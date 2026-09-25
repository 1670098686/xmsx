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
import json
import mimetypes
import os
import uuid
import urllib.error
import urllib.request

from config import constants as C
from utils.exceptions import AIServiceError


def normalize_base_url(base_url: str) -> str:
    """规整 base_url：去空白与结尾斜杠。"""
    return (base_url or "").strip().rstrip("/")


def _http_post(url: str, headers: dict, body: bytes, timeout: int) -> dict:
    """发起 POST 并解析 JSON 响应，统一包装网络/协议异常。

    Args:
        url: 完整请求地址。
        headers: 请求头字典。
        body: 已编码的请求体字节。
        timeout: 超时秒数。
    Returns:
        解析后的 JSON 字典。
    Raises:
        AIServiceError: 网络异常、非 200 状态或响应非 JSON。
    """
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise AIServiceError(f"模型服务返回错误（{exc.code}）：{detail}") from exc
    except urllib.error.URLError as exc:
        raise AIServiceError(f"无法连接模型服务：{exc.reason}") from exc
    except TimeoutError as exc:
        raise AIServiceError("连接模型服务超时，请检查网络或稍后重试") from exc
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise AIServiceError("模型服务返回了无法识别的内容") from exc


def upload_file(base_url: str, api_key: str, file_path: str) -> str:
    """将文献原始文件上传到模型服务，返回 file_id。

    Args:
        base_url: OpenAI 兼容网关根地址。
        api_key: 明文 API 密钥（仅内存传递，不落日志）。
        file_path: 文献原件绝对路径（PDF/DOCX/TXT 等）。
    Returns:
        服务端分配的 file_id 字符串。
    Raises:
        AIServiceError: 上传失败或响应缺少文件 id。
    """
    if not os.path.isfile(file_path):
        raise AIServiceError("文献原件不存在，无法上传解析")
    with open(file_path, "rb") as fp:
        file_bytes = fp.read()

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
    )
    file_id = data.get("id") or (data.get("data") or {}).get("id")
    if not file_id:
        raise AIServiceError("文件上传成功但未取得文件标识")
    return file_id


def chat_with_file(base_url: str, api_key: str, model: str,
                   file_id: str, prompt: str,
                   system_prompt: str = None) -> str:
    """让模型直接阅读已上传的原件并返回文本结果。

    Args:
        base_url: OpenAI 兼容网关根地址。
        api_key: 明文 API 密钥。
        model: 模型名称。
        file_id: upload_file 返回的文件标识。
        prompt: 解析指令（要求结构化 JSON 输出）。
        system_prompt: 可选系统提示词。
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
    payload = {
        "model": model,
        "messages": messages,
        "temperature": C.AI_TEMPERATURE,
        "max_tokens": C.AI_MAX_TOKENS,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    data = _http_post(
        f"{normalize_base_url(base_url)}/chat/completions",
        headers, body, C.AI_TIMEOUT_CHAT,
    )
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise AIServiceError("模型响应结构异常，未包含解析结果") from exc


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
    try:
        result = json.loads(text[start:end + 1])
    except (TypeError, ValueError) as exc:
        raise AIServiceError("模型返回的结构化结果无法解析") from exc
    if not isinstance(result, dict):
        raise AIServiceError("模型返回的结构化结果格式不正确")
    return result
