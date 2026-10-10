"""LLM 适配层：复用 tool_layer.ai_client（urllib/OpenAI 兼容协议）。

不引入 langchain-openai：项目所有模型出口统一走用户配置的 base_url，
这里用 JSON 动作协议（而非 function calling）与模型交互，
对不支持 tools 字段的 OpenAI 兼容网关同样可用。
任何失败都返回 None，由 Director 转确定性恢复动作，保证离线可跑。
"""
from agent import prompts
from agent.state import (
    OCR_MODE_AUTO,
    TOOL_AI_DEEP_ANALYZE,
    TOOL_DETECT_STRUCTURE,
    TOOL_EXTRACT_TABLES,
    TOOL_EXTRACT_TEXT,
    TOOL_OCR_PAGE,
)
from config import constants as C
from tool_layer import ai_client
from utils.exceptions import LiteratureAgentError
from utils.logger import get_logger

logger = get_logger()

# Director 可输出的动作工具白名单
_ALLOWED_TOOLS = {
    TOOL_EXTRACT_TEXT,
    TOOL_EXTRACT_TABLES,
    TOOL_OCR_PAGE,
    TOOL_DETECT_STRUCTURE,
    TOOL_AI_DEEP_ANALYZE,
}


def plan_next_action(model: dict, user_prompt: str,
                     page_count: int = 0) -> dict | None:
    """让 Director 模型单步决定下一个工具动作或收尾。

    Args:
        model: 已解密的模型配置。
        user_prompt: 由 prompts.build_director_user_prompt 组装的上下文。
        page_count: PDF 总页数（非 PDF 传 0），用于页码越界校验。
    Returns:
        {"tool","args","reason"} 单个动作，或 {"finalize": True,"reason"}；
        无模型/调用失败/JSON 非法/无有效动作时返回 None。
    """
    if not model or not model.get("base_url") or not model.get("api_key"):
        return None
    try:
        content = ai_client.chat_with_text(
            model["base_url"], model["api_key"], model.get("name", ""),
            "", user_prompt, prompts.DIRECTOR_SYSTEM_PROMPT,
        )
        parsed = ai_client.extract_json_block(content)
    except (LiteratureAgentError, ValueError, KeyError) as exc:
        logger.warning("Director LLM 决策失败，转确定性恢复：%s", exc)
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("finalize"):
        return {"finalize": True,
                "reason": str(parsed.get("reason", ""))[:120]}
    return sanitize_tool_call(parsed.get("action"), page_count)


def sanitize_tool_call(raw_action, page_count: int) -> dict | None:
    """校验模型给出的单动作：工具白名单、参数类型与页码越界。

    同时服务两处：Director 单步 JSON 动作协议，与读中 function calling
    工具调用（tools.py 的 FC 执行器在本地执行前统一过此校验）。

    Args:
        raw_action: 模型给出的动作/工具调用原始值（dict）。
        page_count: PDF 总页数（0 表示非 PDF，页码类动作直接拒绝）。
    Returns:
        合法动作 {"tool","args",...}；非法返回 None。
    """
    if not isinstance(raw_action, dict):
        return None
    tool_name = raw_action.get("tool")
    if tool_name not in _ALLOWED_TOOLS:
        return None
    raw_args = raw_action.get("args")
    if raw_args is None:
        raw_args = {}
    if not isinstance(raw_args, dict):
        return None
    reason = str(raw_action.get("reason", ""))[:120]

    if tool_name == TOOL_EXTRACT_TEXT:
        args: dict = {}
    elif tool_name == TOOL_DETECT_STRUCTURE:
        if "precision" not in raw_args:
            return None
        try:
            precision = int(raw_args["precision"])
        except (TypeError, ValueError):
            return None
        args = {"precision": max(1, min(5, precision))}
    elif tool_name == TOOL_EXTRACT_TABLES:
        try:
            page_index = int(raw_args.get("page_index", -1))
        except (TypeError, ValueError):
            return None
        # 非 PDF（page_count=0）不允许按页抽表；PDF 允许 -1（全部页）
        if page_count > 0:
            if not (page_index == -1 or 0 <= page_index < page_count):
                return None
        elif page_index != -1:
            return None
        try:
            max_tables = int(raw_args.get(
                "max_tables", C.AGENT_TABLE_MAX_DEFAULT))
        except (TypeError, ValueError):
            max_tables = C.AGENT_TABLE_MAX_DEFAULT
        args = {"page_index": page_index,
                "max_tables": max(1, min(C.AGENT_TABLE_MAX_DEFAULT,
                                         max_tables))}
    elif tool_name == TOOL_OCR_PAGE:
        if page_count <= 0:
            return None
        if "page_index" not in raw_args:
            return None
        try:
            page_index = int(raw_args["page_index"])
        except (TypeError, ValueError):
            return None
        if not 0 <= page_index < page_count:
            return None
        mode = str(raw_args.get("mode", OCR_MODE_AUTO))
        if mode not in ("auto", "local", "ai"):
            mode = OCR_MODE_AUTO
        args = {"page_index": page_index, "mode": mode}
    else:  # TOOL_AI_DEEP_ANALYZE
        focus = str(raw_args.get("focus", "") or "").strip()
        if not focus:
            return None
        # 维度合法性由 director.apply_director_guard 进一步收敛到缺失集，
        # 这里只做基础字符约束，防注入超长串
        args = {"focus": focus[:200]}

    return {"tool": tool_name, "args": args, "reason": reason}
