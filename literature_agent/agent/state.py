"""Agent 共享状态与节点/工具名常量。"""
from typing import Annotated, Callable, Optional, TypedDict

# 六个解析维度（与 tool_layer.text_analysis._SECTION_ORDER、
# business.parse_rule_service.DIMENSION_KEYS 保持一致的稳定协议）
DIMENSION_KEYS = (
    "research_background", "core_view", "research_method",
    "innovation_point", "research_conclusion", "reference_list",
)

# ================= 节点名 =================
NODE_PLAN = "plan"
NODE_ACT = "act"
NODE_OBSERVE = "observe"
NODE_REFLECT = "reflect"
NODE_VERIFY = "verify"
NODE_REFINE = "refine"
NODE_FINISH = "finish"

# ================= 条件路由值 =================
ROUTE_ACT = "act"
ROUTE_VERIFY = "verify"
ROUTE_FINISH = "finish"
ROUTE_REFINE = "refine"

# ================= 工具名（LLM 动作 JSON 中的取值） =================
TOOL_INSPECT_DOCUMENT = "inspect_document"
TOOL_OCR_PAGE = "ocr_page"
TOOL_EXTRACT_TEXT = "extract_text"
TOOL_EXTRACT_TABLES = "extract_tables"
TOOL_DETECT_STRUCTURE = "detect_structure"
TOOL_EXTRACT_KEYWORDS = "extract_keywords"
TOOL_AI_DEEP_ANALYZE = "ai_deep_analyze"
# 原件直传版整体解读：模型经 file_id/file_data 通道直接通读文献原件
# （PDF/Word），不经过本地提取文本转写；失败时由 Refine 降级文本通道
TOOL_AI_READ_ORIGINAL = "ai_read_original"

# 页面级布局类型（inspect_document 输出的 estimated_layout 取值）
LAYOUT_SINGLE = "single"          # 普通单栏文本
LAYOUT_DOUBLE = "double"          # 双栏排版
LAYOUT_MIXED = "mixed"            # 抽样页中同时出现单栏与双栏
LAYOUT_IMAGE_ONLY = "image-only"  # 无文本层（扫描/图片页）

# OCR 通道选择（ocr_page 的 mode 取值）
OCR_MODE_AUTO = "auto"            # 有视觉模型走 AI，否则本地 Tesseract
OCR_MODE_LOCAL = "local"          # 强制本地 Tesseract
OCR_MODE_AI = "ai"                # 强制 AI 多模态视觉
OCR_ENGINE_LOCAL = "tesseract"
OCR_ENGINE_AI = "ai_vision"

# 分维度策略：以本地规则为权威、不交给 AI 解读的确定性维度
# （参考文献是结构化条目，本地正则更可靠，AI 复述易编造）
LOCAL_AUTHORITATIVE_DIMS = ("reference_list",)

# AI 补救动作的事实依据通道（ai_deep_analyze 的 basis 取值）
TOOL_BASIS_LOCAL_SECTION = "local_section"  # 单维度重解读：仅喂该维度本地章节原文
TOOL_BASIS_MIXED_SECTION = "mixed_section"  # 多维度合并重解读：按维度拼接章节/全文


def _append_list(left: list, right: list) -> list:
    """LangGraph 列表状态追加 reducer：历史记录只追加不覆盖。"""
    return (left or []) + (right or [])


class AgentState(TypedDict, total=False):
    """ReAct 循环在各节点间流转的共享状态。

    输入字段由 run_parse_agent 初始化；其余字段随循环逐步填充。
    tool_history / warnings 使用追加 reducer，保留完整迭代轨迹。
    """

    # ---------- 输入 ----------
    lit_id: int
    lit_path: str
    rule_detail: dict
    precision: int
    model: Optional[dict]
    progress_callback: Optional[Callable]
    degradation_callback: Optional[Callable]  # 原件→文本降级前的用户授权回调

    # ---------- 规划 ----------
    planned: bool                      # 文本提取后的后续动作是否已规划
    planned_actions: list[dict]        # 待执行动作队列 [{"tool","args","reason"}]
    lit_profile: dict                  # 文献画像（类型/字数/章节密度，驱动自适应策略）

    # ---------- 视觉感知（阶段2） ----------
    inspect_layout: dict               # inspect_document 的原件布局摘要
    ocr_pages: dict                    # OCR 结果 {页码索引: 文本}（扫描件/图表页）
    table_texts: dict                  # 表格结构化结果 {"第N页/Word表N": Markdown}

    # ---------- ReAct 轨迹 ----------
    abs_text: str                      # 文献全文（提取/OCR/表格聚合产出）
    tool_history: Annotated[list, _append_list]  # 每步动作与观测摘要
    current_step: int
    max_steps: int
    # AI 主导编排：Director LLM 连续返回非法决策的次数（达阈值转确定性兜底）
    director_failures: int

    # ---------- 中间产物（两套草稿供 Verify 交叉比对） ----------
    local_drafts: dict                 # 本地规则六维度结果
    section_paragraphs: dict           # 各维度章节覆盖的全局段落号 {dim:[idx,...]}
    ai_drafts: dict                    # AI 深度解读六维度结果
    citation_map: dict                 # AI 每维度标注的原文段落索引 {dim:[idx,...]}
    keywords: list[str]

    # ---------- 校验与产出 ----------
    verify_result: dict
    last_verify_percent: Optional[int]  # 最近一次三重校验的进度锚点（Refine 补救段下限）
    refine_attempts: dict              # "(维度,问题类型)" → 已补救次数（防回环死循环）
    original_read_failed: bool         # 原件直传已失败（进度锚点用，文本降级紧跟 75 段不倒挂）
    ai_original_trusted: bool          # 原件直读成功且维度齐全：跳过一致性/溯源，只查完整性
    ai_unavailable: bool               # AI 通道判定为不可用（网络/鉴权等），Refine 不再排 AI 补救
    ai_error: Optional[str]            # 首个致命 AI 错误原文（随报告回传界面弹窗说明）
    final_report: Optional[dict]
    engine: str                        # agent_ai / agent_local
    warnings: Annotated[list, _append_list]
    error: Optional[str]
