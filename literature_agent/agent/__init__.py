"""ReAct 文献解析智能体（基于 LangGraph）。

定位：业务层（business）与工具层（tool_layer）之间的**编排层**。
- 由 business/literature_parse.py 调用，本身不直接访问数据库；
- 仅封装 tool_layer 能力（file_parser / text_analysis / ai_client）为工具，
  通过 Plan → Act → Observe → Reflect → Verify 的状态循环自主编排；
- 输入为文献路径与解析规则，输出为六维度结构化报告草稿，持久化仍由业务层完成。
"""
from langgraph.checkpoint.memory import MemorySaver

from config import constants as C
from agent.graph import build_parse_graph
from agent.state import DIMENSION_KEYS

__all__ = [
    "build_parse_graph", "run_parse_agent", "resume_parse_agent",
    "get_agent_state", "DIMENSION_KEYS",
]

# 进程级共享 checkpointer：按 thread_id 保存各解析任务的检查点，
# 使"中断后用同一 thread_id 续跑"跨多次图调用仍然有效
_shared_checkpointer = MemorySaver()


def _build_initial_state(lit_path: str, rule_detail: dict, precision: int,
                         model: dict, lit_id: int, progress_callback,
                         max_steps: int, degradation_callback=None) -> dict:
    """构造图初始状态（集中维护，保证首次运行与续跑入口字段一致）。"""
    return {
        "lit_id": lit_id,
        "lit_path": lit_path,
        "rule_detail": rule_detail or {},
        "precision": precision,
        "model": model,
        "progress_callback": progress_callback,
        "degradation_callback": degradation_callback,
        "planned": False,
        "planned_actions": [],
        "lit_profile": {},
        "tool_history": [],
        "local_drafts": {},
        "section_paragraphs": {},
        "ai_drafts": {},
        "citation_map": {},
        "table_texts": {},
        "director_failures": 0,
        "ai_original_trusted": False,
        "keywords": [],
        "verify_result": {},
        "last_verify_percent": None,
        "refine_attempts": {},
        "current_step": 0,
        "max_steps": max_steps or C.AGENT_MAX_STEPS,
        "warnings": [],
    }


def run_parse_agent(lit_path: str, rule_detail: dict, precision: int = 3,
                    model: dict = None, lit_id: int = 0,
                    progress_callback=None, max_steps: int = None,
                    thread_id: str = None, degradation_callback=None) -> dict:
    """编译并运行一次文献解析 Agent（同步阻塞，适合 QThread worker 内调用）。

    Args:
        lit_path: 文献原件绝对路径。
        rule_detail: 解析规则详情（含 dimensions 开关、weights 权重）。
        precision: 解析精度 1-5。
        model: 当前 AI 模型配置（含已解密 api_key）；None 表示离线本地模式。
        lit_id: 文献 id（仅用于日志/进度标识，不做数据库操作）。
        progress_callback: 可选回调 (percent:int, message:str)。
        max_steps: ReAct 循环最大工具调用步数，防止无限迭代。
        thread_id: 可选检查点线程 id；传入后启用 MemorySaver，
            中断时可用同一 id 调 resume_parse_agent 续跑。
        degradation_callback: 可选回调 (error_message:str)->bool，原件直传
            因协议不支持/超限失败、准备换轨全文文本前征询用户授权；
            返回 False 或未提供回调时不降级，直接转本地基线。
    Returns:
        最终状态 AgentState（final_report 为六维度报告字典）。
    """
    checkpointer = _shared_checkpointer if thread_id else None
    graph = build_parse_graph(checkpointer=checkpointer)
    initial_state = _build_initial_state(
        lit_path, rule_detail, precision, model, lit_id,
        progress_callback, max_steps, degradation_callback,
    )
    config = {"configurable": {"thread_id": thread_id}} if thread_id else None
    return graph.invoke(initial_state, config=config)


def resume_parse_agent(thread_id: str, progress_callback=None) -> dict:
    """从检查点续跑一个中断的解析任务（同进程内有效）。

    Args:
        thread_id: 首次运行时使用的检查点线程 id。
        progress_callback: 可选回调；续跑以该回调替换状态中的旧回调。
    Returns:
        续跑后的最终状态；线程不存在或已结束时返回当前检查点状态。
    Raises:
        KeyError: thread_id 无对应检查点时抛出。
    """
    graph = build_parse_graph(checkpointer=_shared_checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    if progress_callback is not None:
        # 回调对象（常含 UI 引用）不随检查点持久化，续跑时通过状态更新重新绑定
        graph.update_state(config, {"progress_callback": progress_callback})
    # 输入传 None：LangGraph 从最近一个未完成节点继续而非重新开始
    return graph.invoke(None, config=config)


def get_agent_state(thread_id: str) -> dict:
    """读取某检查点线程的当前状态（用于查询进度/是否已结束）。

    Args:
        thread_id: 检查点线程 id。
    Returns:
        当前状态值字典；无线程时返回空字典。
    """
    graph = build_parse_graph(checkpointer=_shared_checkpointer)
    config = {"configurable": {"thread_id": thread_id}}
    snapshot = graph.get_state(config)
    return dict(snapshot.values or {})
