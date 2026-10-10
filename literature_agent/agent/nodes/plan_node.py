"""Plan 节点：产生首个必选动作并做冷启动预热。

AI 主导改造后，PDF / TXT / DOCX / DOC 统一先执行 inspect_document：
该工具对非 PDF 毫秒级返回固定布局摘要，随后由 Reflect/Director 决定
第一步直传原件还是提取文本。不再为非 PDF 伪造布局，保证首轮播种逻辑
只有一条入口。

无论哪种格式，都在 Plan 节点内预热 jieba 词典，让首次 detect_structure
/ extract_keywords 毫秒级返回，避免用户感知到 0.5-1s 的"启动卡顿"。
"""
import os

from agent.nodes import emit
from agent.state import TOOL_INSPECT_DOCUMENT
from config import constants as C
from utils.logger import get_logger

logger = get_logger()


def _prewarm_jieba() -> None:
    """jieba 词典首次加载需 0.5-1s，在 Plan 节点里同步预热一次。

    首次解析的 detect_structure / extract_keywords 就直接命中内存缓存；
    预热本身对 TXT 仅几毫秒（词典已热启动），PDF 也只多一次微秒级调用。
    异常保护：预热失败不影响图执行，just ignore。
    """
    try:
        import jieba  # noqa: F401
        jieba.lcut("记账类APP 模块化设计")
    except Exception:
        pass


def plan_node(state: dict) -> dict:
    """初始化动作队列：所有格式统一先体检布局，并预热 jieba。

    Args:
        state: Agent 共享状态。
    Returns:
        状态增量（planned_actions 初始队列、current_step）。
    """
    lit_path = state.get("lit_path", "")
    suffix = os.path.splitext(lit_path)[1].lower()
    model_name = ((state.get("model") or {}).get("name")
                  if state.get("model") else "未配置")
    logger.info("📚 开始解析《%s》（%s 格式，精度 %s/5，模型：%s）",
                os.path.basename(lit_path), suffix or "无后缀",
                state.get("precision", 3), model_name)
    logger.info("  🔍 第一步：让 AI 检查文献原件的排版布局…")

    emit(state, C.AGENT_PCT_START,
         "ReAct 智能体已启动，正在检查原件布局…")
    first_action = {
        "tool": TOOL_INSPECT_DOCUMENT,
        "args": {"file_path": lit_path,
                 "sample_pages": C.AGENT_INSPECT_SAMPLE_PAGES},
        "reason": "先感知原件布局（扫描件/双栏/图表页/表格页），"
                  "再由编排器决定原件直传或文本提取",
    }
    _prewarm_jieba()
    return {"planned_actions": [first_action], "current_step": 0}
