"""AI 模型 HTTP 客户端（OpenAI 兼容协议）。

仅包含与远端模型服务通信的底层能力，不含任何业务判断：
- upload_file：将文献**原始文件**（PDF/DOCX/TXT 二进制原件）上传到模型服务，
  取回 file_id，全程不经过本地文本提取；
- chat_with_file：携带 file_id 发起对话，由模型直接阅读原件生成结果。

协议对齐 OpenAI 兼容网关（如 dashscope compatible-mode/v1、qwen-long）：
- POST {base_url}/files          multipart: purpose=file-extract, file=<原件>
- POST {base_url}/chat/completions  messages.content=[{type:file},{type:text}]

网络出口唯一且受控：仅访问用户在「AI 模型配置」中填写的 base_url。
"""
import base64
import json
import mimetypes
import os
import re
import socket
import tempfile
import threading
import time
import uuid
import urllib.error
import urllib.request

from config import constants as C
from utils.exceptions import AIServiceError
from utils.logger import get_logger

logger = get_logger()

# 文件上传后服务端仍在异步解析时，错误信息中常见的关键字（用于探测重试）
_FILE_BUSY_KEYWORDS = ("processing", "being processed", "parsing",
                       "解析中", "正在解析", "文件正在", "not ready")

# 模型不接受 file_id 类型消息时，各网关常见的报错特征（小写匹配）
# 注：DashScope 对 qwen-max 等模型返回的
# "if content is list. item must be dict and key[type] should in dict"
# 是其对"type 取值不在允许枚举内"的笼统提示，并非消息体真的缺 type 键
_FILE_REJECTED_KEYWORDS = (
    "supported values", "invalid_value", "not support", "unsupported",
    "item must be dict", "should in dict", "content is list",
    "file_id", "file_url", "不支持",
)


def is_file_message_rejected(message: str) -> bool:
    """判断错误信息是否表示"模型不接受 file_id 类型的 content 项"。

    Args:
        message: 模型服务返回的错误原文。
    Returns:
        True 表示属于协议不支持（可降级全文文本），False 为其他错误。
    """
    raw = message or ""
    low = raw.lower()
    file_mentioned = ("file" in low) or ("文件" in raw) or ("附件" in raw)
    return file_mentioned and any(k in low for k in _FILE_REJECTED_KEYWORDS)


# 原件（PDF 内联）体积超限类报错特征：换成全文文本通道仍可解析
_FILE_OVERSIZE_KEYWORDS = (
    "过大", "超限", "超过内联", "上限", "too large", "exceed",
    "request entity too large", "413",
)


def is_file_channel_degradable(message: str) -> bool:
    """原件直传失败时，是否适合自动换轨全文文本通道。

    - 协议不支持（模型不接受 file 类型消息）：文本通道必然可用，应降级；
    - PDF 体积超内联上限：文本通道不受此限制，应降级；
    - 网络超时、鉴权失败、限流、5xx 等：换文本通道大概率同样失败，
      不应降级，直接转本地基线，避免无意义的重试。

    Args:
        message: 模型服务返回的错误原文。
    Returns:
        True 表示可安全降级全文文本；False 表示应终止 AI 尝试。
    """
    if is_file_message_rejected(message):
        return True
    low = (message or "").lower()
    return any(k in low for k in _FILE_OVERSIZE_KEYWORDS)


# function calling 不被网关接受时的报错特征（用于 Plan A 同框 → Plan B
# 分段取证的自动降级；只认协议参数类错误，网络/鉴权/超时不得误判）
_TOOLS_REJECTED_KEYWORDS = (
    "tools", "tool_choice", "tool_calls", "function",
    "unknown parameter", "unexpected field", "unrecognized",
    "invalid parameter", "参数不支持", "不支持的参数", "未知参数",
)
_TOOLS_REJECTED_PROTOCOL_HINTS = ("400", "bad request", "invalid",
                                  "unsupported", "不支持")


def is_tools_unsupported(message: str) -> bool:
    """判断报错是否表示"网关不接受 tools/function calling 字段（或与 file 同框）"。

    必须同时命中一个 tools 协议词与一个 4xx/协议提示，避免把网络、鉴权
    类错误误判成协议降级（那两类错误重试 tools 也不会成功）。

    Args:
        message: 模型服务返回的错误原文。
    Returns:
        True 表示可切换到 Plan B（分段取证 + 原子通读）。
    """
    low = (message or "").lower()
    hit_tool_word = any(word in low for word in _TOOLS_REJECTED_KEYWORDS)
    hit_proto = any(hint in low for hint in _TOOLS_REJECTED_PROTOCOL_HINTS)
    return hit_tool_word and hit_proto


# 全文文本通道"输入超长"类报错特征：缩短输入（如只喂章节原文）后仍可能成功，
# 不应直接判定 AI 通道整体不可用
_TEXT_TOO_LONG_KEYWORDS = (
    "maximum context length", "context_length_exceeded", "context length",
    "maximum tokens", "token length", "input length", "too long",
    "reduce the length", "上下文", "长度", "太长",
)


def is_context_length_error(message: str) -> bool:
    """判断文本通道失败是否由"输入超长"引起（缩短输入后可重试）。

    Args:
        message: 模型服务返回的错误原文。
    Returns:
        True 表示属于超长类错误（章节级短文本补救仍有意义）；
        False 表示网络/鉴权/其他致命错误，应终止 AI 尝试。
    """
    low = (message or "").lower()
    return any(k in low for k in _TEXT_TOO_LONG_KEYWORDS)


def friendly_file_error(message: str) -> str:
    """把"不支持 file_id 原件直传"的晦涩报错转成面向用户的说明。

    Args:
        message: 模型服务返回的错误原文。
    Returns:
        友好说明（附带原始报错供排查）；非此类错误时原样返回。
    """
    if not is_file_message_rejected(message):
        return message
    return f"{C.AI_FILE_ID_UNSUPPORTED_HINT}（服务端原始报错：{message}）"


def normalize_base_url(base_url: str) -> str:
    """规整 base_url：去空白与结尾斜杠。"""
    return (base_url or "").strip().rstrip("/")


def _timeout_message(timeout: int) -> str:
    """统一的模型响应超时提示（与网络断开类错误区分，避免误导用户排查网络）。"""
    return (
        f"模型服务响应超时（已等待约 {int(timeout)} 秒仍未收到完整结果）："
        "可能是服务端排队或长文档生成耗时过久，请稍后重试，"
        "或在 AI 设置中更换响应更快的模型"
    )


def _http_post(url: str, headers: dict, body: bytes, timeout: int,
               on_waiting=None,
               wait_interval: int = C.AI_WAIT_HEARTBEAT_SEC) -> dict:
    """发起 POST 并解析 JSON 响应，统一包装网络/协议异常。

    Args:
        url: 完整请求地址。
        headers: 请求头字典。
        body: 已编码的请求体字节。
        timeout: 超时秒数。
        on_waiting: 可选回调 (elapsed_seconds:int)->None，请求等待期间每
            wait_interval 秒由守护线程调用一次，用于进度条"仍在思考"心跳；
            回调内部异常不影响请求。
        wait_interval: 心跳间隔秒数。
    Returns:
        解析后的 JSON 字典。
    Raises:
        AIServiceError: 网络异常、非 200 状态或响应非 JSON。
    """
    logger.debug("AI 请求开始：POST %s（请求体 %s 字节，超时 %s 秒）",
                 url, len(body or b""), timeout)
    started = time.monotonic()
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    stop_event = threading.Event()
    if on_waiting is not None:
        started_at = time.monotonic()

        def _heartbeat() -> None:
            while not stop_event.wait(max(0.05, float(wait_interval))):
                try:
                    on_waiting(int(time.monotonic() - started_at))
                except Exception:  # 心跳是旁路能力，任何异常都不得影响请求
                    pass

        threading.Thread(target=_heartbeat, daemon=True).start()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        logger.debug("AI 请求失败：HTTP %s，耗时 %.1f 秒",
                     exc.code, time.monotonic() - started, exc_info=True)
        raise AIServiceError(f"模型服务返回错误（{exc.code}）：{detail}") from exc
    except TimeoutError as exc:
        # Python 3.10+ socket.timeout 即 TimeoutError；部分环境 urllib 会把它
        # 包进 URLError（下方再兜底判一次）
        logger.debug("AI 请求超时，耗时 %.1f 秒",
                     time.monotonic() - started, exc_info=True)
        raise AIServiceError(_timeout_message(timeout)) from exc
    except urllib.error.URLError as exc:
        logger.debug("AI 请求网络异常，耗时 %.1f 秒",
                     time.monotonic() - started, exc_info=True)
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            raise AIServiceError(_timeout_message(timeout)) from exc
        raise AIServiceError(f"无法连接模型服务：{exc.reason}") from exc
    finally:
        stop_event.set()
    logger.debug("AI 请求成功：响应 %s 字节，耗时 %.1f 秒",
                 len(raw), time.monotonic() - started)
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AIServiceError("模型服务返回了无法识别的内容") from exc


def upload_file(base_url: str, api_key: str, file_path: str,
                on_waiting=None) -> str:
    """将文献原始文件上传到模型服务，返回 file_id。

    Args:
        base_url: OpenAI 兼容网关根地址。
        api_key: 明文 API 密钥（仅内存传递，不落日志）。
        file_path: 文献原件绝对路径（PDF/DOCX/TXT 等）。
        on_waiting: 可选等待心跳回调（已等待秒数），大文件上传时反馈进度。
    Returns:
        服务端分配的 file_id 字符串。
    Raises:
        AIServiceError: 上传失败或响应缺少文件 id。
    """
    if not os.path.isfile(file_path):
        raise AIServiceError("文献原件不存在，无法上传解析")
    with open(file_path, "rb") as fp:
        file_bytes = fp.read()
    logger.debug("AI 原件上传：%s（%s KB）",
                 os.path.basename(file_path), len(file_bytes) // 1024)

    boundary = f"----litagent{uuid.uuid4().hex}"
    filename = os.path.basename(file_path)
    content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def part(name: str, value: str) -> bytes:
        """构造普通表单字段片段。"""
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n'
        )
        return head.encode("utf-8") + value.encode("utf-8") + b"\r\n"

    body = b""
    body += part("purpose", C.AI_FILE_PURPOSE)
    file_head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode("utf-8")
    body += file_head + file_bytes + b"\r\n"
    body += f"--{boundary}--\r\n".encode("utf-8")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
    }
    data = _http_post(
        f"{normalize_base_url(base_url)}/files",
        headers, body, C.AI_TIMEOUT_UPLOAD,
        on_waiting=on_waiting,
    )
    file_id = data.get("id") or (data.get("data") or {}).get("id")
    if not file_id:
        raise AIServiceError("文件上传成功但未取得文件标识")
    return file_id


def chat_with_file(base_url: str, api_key: str, model: str,
                   file_id: str, prompt: str,
                   system_prompt: str = None,
                   on_waiting=None) -> str:
    """让模型直接阅读已上传的原件并返回文本结果（仅支持文件消息的模型可用）。

    Args:
        base_url: OpenAI 兼容网关根地址。
        api_key: 明文 API 密钥。
        model: 模型名称。
        file_id: upload_file 返回的文件标识。
        prompt: 解析指令（要求结构化 JSON 输出）。
        system_prompt: 可选系统提示词。
        on_waiting: 可选等待心跳回调（已等待秒数）。
    Returns:
        模型输出的文本内容。
    Raises:
        AIServiceError: 调用失败或响应结构异常。
    """
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({
        "role": "user",
        "content": [
            {"type": "file", "file_id": file_id},
            {"type": "text", "text": prompt},
        ],
    })
    return _post_chat_text(base_url, api_key, model, messages,
                           on_waiting=on_waiting)


def chat_with_local_pdf(base_url: str, api_key: str, model: str,
                        file_path: str, prompt: str,
                        system_prompt: str = None,
                        on_waiting=None) -> str:
    """把本地 PDF 原件以 Base64 内联方式发给模型直接阅读（qwen3.8 系列）。

    协议（DashScope OpenAI 兼容模式 PDF 理解）：
    content=[{"type":"file","file":{"file_data":"data:application/pdf;base64,***)",
    "filename": 名称}}, {"type":"text","text": 指令}]。
    文件只随本次请求发送给用户配置的接口，不经过 /files 托管、不需要公网 URL。

    Args:
        base_url: OpenAI 兼容网关根地址。
        api_key: 明文 API 密钥。
        model: 模型名称（须为支持 PDF 理解的 qwen3.8 系列）。
        file_path: 本地 PDF 原件绝对路径。
        prompt: 解析指令（要求结构化 JSON 输出）。
        system_prompt: 可选系统提示词。
        on_waiting: 可选等待心跳回调（已等待秒数）。
    Returns:
        模型输出的文本内容。
    Raises:
        AIServiceError: 文件不存在/非 PDF/超过内联大小上限或调用失败。
    """
    if not os.path.isfile(file_path):
        raise AIServiceError("文献原件不存在，无法发送解析")
    if os.path.splitext(file_path)[1].lower() != ".pdf":
        raise AIServiceError("PDF Base64 内联通道仅支持 .pdf 文件")
    file_size = os.path.getsize(file_path)
    if file_size > C.AI_PDF_INLINE_MAX_BYTES:
        limit_mb = C.AI_PDF_INLINE_MAX_BYTES // (1024 * 1024)
        raise AIServiceError(
            f"PDF 原件过大（约 {file_size // (1024 * 1024)}MB），"
            f"超过内联发送上限 {limit_mb}MB，无法以原件模式解析"
        )
    with open(file_path, "rb") as fp:
        pdf_b64 = base64.b64encode(fp.read()).decode("ascii")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({
        "role": "user",
        "content": [
            {
                "type": "file",
                "file": {
                    "file_data": f"data:application/pdf;base64,{pdf_b64}",
                    "filename": os.path.basename(file_path),
                },
            },
            {"type": "text", "text": prompt},
        ],
    })
    return _post_chat_text(
        base_url, api_key, model, messages,
        timeout=C.AI_TIMEOUT_PDF_CHAT, on_waiting=on_waiting,
    )


def chat_with_vision(base_url: str, api_key: str, model: str,
                     image_bytes: bytes, prompt: str,
                     image_mime: str = "image/png",
                     system_prompt: str = None,
                     on_waiting=None) -> str:
    """把单页渲染图片以多模态消息发给视觉模型做 OCR/图表识别。

    走 OpenAI 兼容视觉协议（image_url + Base64 data URI）：
    content=[{"type":"text","text":指令},
             {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}]。
    图片仅随本次请求发送给用户配置的接口，不落盘不外传第三方。

    Args:
        base_url: OpenAI 兼容网关根地址。
        api_key: 明文 API 密钥（仅内存传递，不落日志）。
        model: 模型名称（须为支持视觉理解的多模态模型）。
        image_bytes: 页面渲染图片字节（PNG/JPEG）。
        prompt: OCR/识别指令。
        image_mime: 图片 MIME 类型，默认 image/png。
        system_prompt: 可选系统提示词。
        on_waiting: 可选等待心跳回调（已等待秒数）。
    Returns:
        模型识别出的页面文本。
    Raises:
        AIServiceError: 图片为空或调用失败。
    """
    if not image_bytes:
        raise AIServiceError("页面图片为空，无法进行视觉识别")
    image_b64 = base64.b64encode(image_bytes).decode("ascii")
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_mime};base64,{image_b64}",
                },
            },
        ],
    })
    return _post_chat_text(
        base_url, api_key, model, messages,
        timeout=C.AI_TIMEOUT_VISION_CHAT, on_waiting=on_waiting,
    )


def chat_with_text(base_url: str, api_key: str, model: str,
                   full_text: str, prompt: str,
                   system_prompt: str = None,
                   max_tokens: int = None,
                   on_waiting=None) -> str:
    """把从原件完整提取的全文作为普通文本消息发给模型解析。

    适用于不支持文件消息但支持长上下文的模型（如 qwen-max）：
    指令在前、全文紧随其后，要求模型通读全文后只输出 JSON。

    Args:
        base_url: OpenAI 兼容网关根地址。
        api_key: 明文 API 密钥。
        model: 模型名称。
        full_text: 从文献原件提取的完整全文。
        prompt: 解析指令（要求结构化 JSON 输出）。
        system_prompt: 可选系统提示词。
        max_tokens: 可选响应 token 上限覆盖。
        on_waiting: 可选等待心跳回调（已等待秒数）。
    Returns:
        模型输出的文本内容。
    Raises:
        AIServiceError: 调用失败或响应结构异常。
    """
    user_content = (
        f"{prompt}\n\n"
        "===== 文献原件完整文本（开始） =====\n"
        f"{full_text}\n"
        "===== 文献原件完整文本（结束） ====="
    )
    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_content})
    return _post_chat_text(base_url, api_key, model, messages, max_tokens,
                           on_waiting=on_waiting)


def _post_chat(base_url: str, api_key: str, model: str,
               messages: list, max_tokens: int = None,
               timeout: int = None, on_waiting=None,
               tools: list = None, tool_choice: str = None) -> dict:
    """统一的 chat/completions 请求，解析 choices[0].message。

    支持 OpenAI 兼容 function calling：传入 tools 时模型可能返回 tool_calls
    而非正文（此时 content 为空串是正常现象，由调用方驱动多轮循环）。

    Args:
        base_url: OpenAI 兼容网关根地址。
        api_key: 明文 API 密钥。
        model: 模型名称。
        messages: OpenAI 消息体。
        max_tokens: 可选响应 token 上限覆盖。
        timeout: 可选请求超时秒数覆盖（默认普通对话超时）。
        on_waiting: 可选等待心跳回调（已等待秒数）。
        tools: 可选 OpenAI 工具 schema 列表（function calling）。
        tool_choice: 可选工具选择策略（"auto"/"required"/None）。
    Returns:
        {"content": str, "tool_calls": list, "finish_reason": str}：
        tool_calls 为模型请求调用的工具列表（无则空列表）。
    Raises:
        AIServiceError: 调用失败或响应结构异常。
    """
    kinds = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            kinds.extend(str(item.get("type", "?")) for item in content)
        elif content:
            kinds.append("text")
    logger.debug(
        "AI 对话调用：模型=%s，消息部件=%s，共 %s 条消息，tools=%s，"
        "max_tokens=%s，超时 %s 秒",
        model, ",".join(kinds) or "text", len(messages),
        len(tools) if tools else 0,
        max_tokens or C.AI_MAX_TOKENS, timeout or C.AI_TIMEOUT_CHAT)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": C.AI_TEMPERATURE,
        "max_tokens": max_tokens or C.AI_MAX_TOKENS,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = _http_post(
        f"{normalize_base_url(base_url)}/chat/completions",
        headers, body, timeout or C.AI_TIMEOUT_CHAT,
        on_waiting=on_waiting,
    )
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        tool_calls = message.get("tool_calls") or []
        finish_reason = str(choice.get("finish_reason") or "").lower()
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise AIServiceError("模型响应结构异常，未包含解析结果") from exc
    return {
        "content": str(content),
        "tool_calls": tool_calls if isinstance(tool_calls, list) else [],
        "finish_reason": finish_reason,
    }


def _post_chat_text(base_url: str, api_key: str, model: str,
                    messages: list, max_tokens: int = None,
                    timeout: int = None, on_waiting=None) -> str:
    """_post_chat 的纯文本包装：校验正文非空/未截断后返回 content 字符串。

    Args:
        同 _post_chat（不含 tools）。
    Returns:
        模型输出文本。
    Raises:
        AIServiceError: 响应正文为空或因长度上限被截断。
    """
    result = _post_chat(base_url, api_key, model, messages, max_tokens,
                        timeout, on_waiting)
    # 推理模型思维链过长会耗尽输出预算：此时 JSON 正文被截断或根本没开始，
    # 继续解析只会得到模糊错误，直接给出可触发降级的明确原因
    if result["finish_reason"] == "length":
        raise AIServiceError(
            "模型输出长度达到上限被截断（结构化结果不完整），"
            "该文献对当前模型过于复杂，建议改用全文文本模式重试"
        )
    if not result["content"].strip():
        raise AIServiceError(
            "模型响应内容为空（可能是推理过程耗尽输出长度上限），"
            "建议改用全文文本模式重试"
        )
    return result["content"]


def chat_round(base_url: str, api_key: str, model: str,
               messages: list, tools: list = None,
               tool_choice: str = None,
               timeout: int = None, on_waiting=None) -> dict:
    """function calling 多轮循环的单轮原语（公开接口，供 agent 层驱动循环）。

    与各 chat_with_* 的区别：不校验正文非空（工具轮 content 为空合法）、
    不做 JSON 提取，调用方自行根据 tool_calls 决定继续循环还是收尾。

    Args:
        base_url: OpenAI 兼容网关根地址。
        api_key: 明文 API 密钥。
        model: 模型名称。
        messages: 含完整多轮历史的消息体（首轮含 file/image 多模态部件）。
        tools: OpenAI 工具 schema 列表；None 表示普通单轮对话。
        tool_choice: 工具选择策略，默认 auto。
        timeout: 可选超时秒数覆盖。
        on_waiting: 可选等待心跳回调。
    Returns:
        {"content": str, "tool_calls": list, "finish_reason": str}
    """
    return _post_chat(base_url, api_key, model, messages,
                      timeout=timeout, on_waiting=on_waiting,
                      tools=tools, tool_choice=tool_choice)


def build_file_content_item(file_id: str = None,
                            file_path: str = None) -> dict:
    """构造 chat/completions 多模态消息中的 file 部件。

    Args:
        file_id: /files 托管通道返回的文件标识（与 file_path 二选一）。
        file_path: 本地文件路径，用于内联通道（仅 PDF，调用方负责类型与
            大小校验及 Base64 编码时机；本函数直接读取并编码）。
    Returns:
        {"type": "file", ...} 消息部件字典。
    Raises:
        AIServiceError: 参数缺失、文件不存在或读取失败。
    """
    if file_id:
        return {"type": "file", "file_id": file_id}
    if file_path:
        if not os.path.isfile(file_path):
            raise AIServiceError("文献原件不存在，无法发送解析")
        with open(file_path, "rb") as fp:
            pdf_b64 = base64.b64encode(fp.read()).decode("ascii")
        return {
            "type": "file",
            "file": {
                "file_data": f"data:application/pdf;base64,{pdf_b64}",
                "filename": os.path.basename(file_path),
            },
        }
    raise AIServiceError("构造 file 消息部件需要 file_id 或 file_path")


# ================= 原件通道分类 =================
# 通道一 file_id：qwen-long / Kimi 等长文档模型，先 POST /files 托管取 file_id，
#               再以 {"type":"file","file_id": xxx} 引用；
# 通道二 file_data：qwen3.8 系列（max/flash/27b）PDF 理解，本地 PDF 直接
#               Base64 内联 {"type":"file","file":{"file_data":..,"filename":..}}，
#               无需 /files、无需公网 URL（文件只发送给用户配置的接口）。
FILE_ID_CHANNEL_KEYWORDS = ("qwen-long", "moonshot", "kimi")
FILE_DATA_CHANNEL_KEYWORDS = ("qwen3.8",)

# 原件通道标识
FILE_CHANNEL_ID = "file_id"
FILE_CHANNEL_DATA = "file_data"
# 启发式无通道（模型名未命中任何已知原件模型）
FILE_CHANNEL_NONE = ""
# 持久化配置中的通道取值（与 config.constants 同值）：
# auto=自动识别；text_only=用户显式选择仅全文文本
FILE_CHANNEL_AUTO = C.AI_FILE_CHANNEL_AUTO
FILE_CHANNEL_TEXT_ONLY = C.AI_FILE_CHANNEL_NONE


def file_channel_mode(model_name: str, base_url: str = "") -> str:
    """按模型名/网关关键字判断原件直传通道类型（启发式，最终以真实探测为准）。

    Args:
        model_name: 模型名称。
        base_url: 接口根地址。
    Returns:
        ``file_id``（/files 托管+file_id）、``file_data``（PDF Base64 内联）
        或 ``""``（不支持原件直传，走全文文本通道）。
    """
    haystack = f"{model_name or ''} {base_url or ''}".lower()
    if any(keyword in haystack for keyword in FILE_ID_CHANNEL_KEYWORDS):
        return FILE_CHANNEL_ID
    if any(keyword in haystack for keyword in FILE_DATA_CHANNEL_KEYWORDS):
        return FILE_CHANNEL_DATA
    return FILE_CHANNEL_NONE


def model_supports_file(model_name: str, base_url: str = "") -> bool:
    """是否具备任一原件直传通道（file_id 或 PDF Base64 内联）。

    仅用于界面提示与路由预判；最终以 probe_file_parsing 的真实探测为准。
    """
    return file_channel_mode(model_name, base_url) != FILE_CHANNEL_NONE


def resolve_file_channel(model: dict) -> str:
    """展开当前模型持久化的原件通道配置，返回实际生效通道。

    - 配置为 auto（或缺失）：按模型名/网关关键字启发式判定为
      file_id / file_data / ""（无原件通道，走全文文本）；
    - 显式配置 file_id / file_data：原样返回；
    - 显式配置 none（仅全文文本）：返回 ""（FILE_CHANNEL_NONE）。

    Args:
        model: 模型配置字典（含 name/base_url/file_channel）。
    Returns:
        FILE_CHANNEL_ID / FILE_CHANNEL_DATA / FILE_CHANNEL_NONE（""）。
    """
    if not model:
        return FILE_CHANNEL_NONE
    configured = model.get("file_channel") or FILE_CHANNEL_AUTO
    if configured == FILE_CHANNEL_AUTO:
        return file_channel_mode(
            model.get("name") or "", model.get("base_url") or "")
    if configured in (FILE_CHANNEL_ID, FILE_CHANNEL_DATA):
        return configured
    return FILE_CHANNEL_NONE


# ================= 配置校验探测 =================

def probe_chat(base_url: str, api_key: str, model: str) -> str:
    """发送一句最小探测对话，验证地址/密钥可用且模型能正常返回文本。

    探测内容不包含任何用户文献数据，只要求模型回一句固定短话。

    Returns:
        模型返回的文本内容（非空即视为通过）。
    Raises:
        AIServiceError: 网络、鉴权、协议或空回复失败，message 面向用户。
    """
    messages = [{
        "role": "user",
        "content": "连接测试：请只回复“连接正常”四个字。",
    }]
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": C.AI_PROBE_MAX_TOKENS,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = _http_post(
        f"{normalize_base_url(base_url)}/chat/completions",
        headers, body, C.AI_PROBE_TIMEOUT,
    )
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AIServiceError("模型响应结构异常，未包含回复内容") from exc
    if not str(content or "").strip():
        raise AIServiceError("模型已响应但回复内容为空，请确认模型名称是否正确")
    return str(content).strip()


# 探测 PDF 正文中的唯一暗号：模型只有真正读出了文件内容，
# 才可能在回复中原样给出该暗号；若模型读不了文件，通常只会回复
# “无法读取/不支持附件”等说明文本，借此区分“假成功”。
_PROBE_PAGE_TEXT = "AI configuration test document. Probe token: ZXQ-4827."
_PROBE_TOKEN_PARTS = ("zxq", "4827")


def _reply_contains_probe_token(reply: str) -> bool:
    """判断模型回复是否原样包含探测 PDF 中的唯一暗号。

    Args:
        reply: 模型回复文本。
    Returns:
        True 表示回复同时包含暗号的字母段与数字段（归一化后比较，
        容忍大小写、空格、连字符、标点差异）。
    """
    normalized = re.sub(r"[^a-z0-9]", "", str(reply or "").lower())
    return all(part in normalized for part in _PROBE_TOKEN_PARTS)


def _build_probe_pdf_bytes() -> bytes:
    """构造一页包含固定英文语句与唯一暗号的最小合法 PDF（含正确 xref）。"""
    page_text = _PROBE_PAGE_TEXT
    stream = (
        "BT /F1 12 Tf 72 720 Td "
        f"({page_text}) Tj ET"
    ).encode("ascii")
    objects = []
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>")
    objects.append(
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
    )
    objects.append(
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
        + stream + b"\nendstream"
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj_body in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{index} 0 obj\n".encode("ascii"))
        pdf.extend(obj_body)
        pdf.extend(b"\nendobj\n")
    xref_pos = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        b"trailer\n<< /Size " + str(len(objects) + 1).encode("ascii")
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_pos).encode("ascii") + b"\n%%EOF\n"
    )
    return bytes(pdf)


def _probe_with_channel(base_url: str, api_key: str, model: str,
                        tmp_path: str, channel: str) -> str:
    """用指定通道对已生成的临时 PDF 发起一次文件对话探测（含忙等重试）。

    Args:
        tmp_path: 内置微型探测 PDF 的本地路径。
        channel: FILE_CHANNEL_ID（/files+file_id）或 FILE_CHANNEL_DATA（内联）。
    Returns:
        模型针对探测 PDF 的非空回复。
    Raises:
        AIServiceError: 通道不可用或始终无有效回复（消息已做友好化）。
    """
    prompt = (
        "这是一个连接测试 PDF。请阅读文件后，"
        "原样回复文件正文中的完整英文语句（其中包含 Probe token 暗号），"
        "不要添加解释或翻译。"
    )
    if channel == FILE_CHANNEL_DATA:
        call = lambda: chat_with_local_pdf(
            base_url, api_key, model, tmp_path, prompt,
        )
    else:
        file_id = upload_file(base_url, api_key, tmp_path)
        messages = [{
            "role": "user",
            "content": [
                {"type": "file", "file_id": file_id},
                {"type": "text", "text": prompt},
            ],
        }]
        call = lambda: _post_chat_text(
            base_url, api_key, model, messages, C.AI_PROBE_MAX_TOKENS,
        )

    last_error = None
    last_reply = ""
    for _attempt in range(C.AI_PROBE_MAX_RETRIES):
        try:
            content = call()
        except AIServiceError as exc:
            last_error = exc
            low = exc.message.lower()
            if any(keyword in low for keyword in _FILE_BUSY_KEYWORDS):
                time.sleep(C.AI_PROBE_RETRY_INTERVAL)
                continue
            raise AIServiceError(friendly_file_error(exc.message)) from exc
        reply_text = str(content or "").strip()
        if reply_text:
            # 仅有非空回复不算通过：读不了文件的模型也会回复“无法读取
            # 附件”等说明文字；必须原样给出文件内唯一暗号才算真能直读。
            if _reply_contains_probe_token(reply_text):
                return reply_text
            last_reply = reply_text
        time.sleep(C.AI_PROBE_RETRY_INTERVAL)
    if last_reply:
        raise AIServiceError(
            "模型有回复但未能读出测试文件中的暗号内容，"
            "该模型/通道可能并不支持直接阅读 PDF 原件，建议改用全文文本模式"
        )
    raise AIServiceError(
        "文件已发送但文件对话始终无有效回复"
        + (f"：{last_error.message}" if last_error else "")
    )


def probe_file_parsing(base_url: str, api_key: str, model: str,
                       mode: str = FILE_CHANNEL_AUTO):
    """探测模型能否直读 PDF 原件（内置微型 PDF，与用户文献无关）。

    Args:
        mode: 通道策略：
            - FILE_CHANNEL_AUTO：名称命中已知通道则只测该通道；
              未知模型依次实测 file_data → file_id（兼容其他 PDF 模型）；
            - FILE_CHANNEL_FILE_DATA / FILE_CHANNEL_ID：只测指定通道；
            - FILE_CHANNEL_NONE：直接判定不可用，不发任何请求。
    Returns:
        (reply_text, used_channel)：模型非空回复与实际可用的通道标识。
    Raises:
        AIServiceError: 全部候选通道均不可用，message 面向用户。
    """
    if mode == FILE_CHANNEL_TEXT_ONLY:
        raise AIServiceError(C.AI_FILE_ID_UNSUPPORTED_HINT)
    if mode == FILE_CHANNEL_AUTO:
        guessed = file_channel_mode(model, base_url)
        candidates = (guessed,) if guessed != FILE_CHANNEL_NONE else (
            FILE_CHANNEL_DATA, FILE_CHANNEL_ID,
        )
    else:
        candidates = (mode,)

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="ai_probe_", suffix=".pdf", delete=False
        ) as tmp:
            tmp.write(_build_probe_pdf_bytes())
            tmp_path = tmp.name

        last_error = None
        for channel in candidates:
            try:
                reply = _probe_with_channel(
                    base_url, api_key, model, tmp_path, channel,
                )
                return reply, channel
            except AIServiceError as exc:
                last_error = exc
                logger.warning("PDF 通道 %s 探测失败：%s", channel, exc.message)
                continue
        raise last_error or AIServiceError(C.AI_FILE_ID_UNSUPPORTED_HINT)
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError as exc:
                logger.warning("探测 PDF 临时文件删除失败：%s | %s", tmp_path, exc)


def _repair_embedded_quotes(text: str) -> str:
    """修复 JSON 字符串值内部未转义的英文双引号（大模型常见输出错误）。

    扫描时区分“结构性引号”与“正文引号”：处于字符串内部时，若一个双引号
    之后（跳过空白）紧跟 `,`、`:`、`}`、`]`，它是字段/字符串的结束引号；
    否则视为正文引号，转义为 ``\\"``。例如
    ``作者以"FLPE账本"为载体`` 中的两个引号都会被正确转义。

    Args:
        text: 待修复的 JSON 文本。
    Returns:
        修复后的 JSON 文本（合法输入原样等价）。
    """
    result = []
    in_string = False
    escaped = False
    length = len(text)
    for index, char in enumerate(text):
        if not in_string:
            result.append(char)
            if char == '"':
                in_string = True
            continue
        if escaped:
            result.append(char)
            escaped = False
            continue
        if char == "\\":
            result.append(char)
            escaped = True
            continue
        if char == '"':
            probe = index + 1
            while probe < length and text[probe] in " \t\r\n":
                probe += 1
            if probe >= length or text[probe] in ",:}]":
                # 结束引号：其后必然是结构符
                in_string = False
                result.append(char)
            else:
                # 正文内部的裸双引号：转义保留
                result.append('\\"')
        else:
            result.append(char)
    return "".join(result)


def _loads_lenient_json(text: str):
    """尽量宽松地把模型输出解析为 JSON，容忍常见不规范写法。

    依次尝试：标准解析（允许字符串内含裸控制字符）→ 去尾随逗号 +
    修复未转义正文引号后再次解析。

    Returns:
        解析后的 Python 对象。
    Raises:
        ValueError: 所有容错手段均失败。
    """
    try:
        return json.loads(text, strict=False)
    except ValueError:
        repaired = re.sub(r",(\s*[}\]])", r"\1", text)
        repaired = _repair_embedded_quotes(repaired)
        return json.loads(repaired, strict=False)


def extract_json_block(content: str) -> dict:
    """从模型输出中提取 JSON 对象（容忍代码围栏与前后说明文字）。

    Args:
        content: 模型原始文本。
    Returns:
        解析后的字典。
    Raises:
        AIServiceError: 未找到合法 JSON。
    """
    text = (content or "").strip()
    # 部分兼容网关会把推理模型的思维链（<think>...</think>）拼进正文，
    # 思维链中可能出现花括号，干扰 JSON 定位，先整体剔除
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = text.strip()
    if "```" in text:
        # 取代码围栏内的片段（```json ... ``` 或裸 ``` ... ```）
        fragments = text.split("```")
        candidates = [f.strip() for f in fragments if "{" in f]
        text = candidates[0] if candidates else text
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise AIServiceError("模型未按要求返回结构化结果")
    block = text[start:end + 1]
    try:
        result = _loads_lenient_json(block)
    except (TypeError, ValueError) as exc:
        # 保留现场，便于后续定位具体模型的输出问题（只记摘要，不影响用户）
        logger.warning(
            "模型 JSON 解析失败，原始回复前 800 字：%s",
            block[:800],
        )
        raise AIServiceError("模型返回的结构化结果无法解析") from exc
    if not isinstance(result, dict):
        raise AIServiceError("模型返回的结构化结果格式不正确")
    return result
