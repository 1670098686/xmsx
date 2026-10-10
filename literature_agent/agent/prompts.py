"""Agent 的全部提示词（单一事实来源）。

本文件集中管理 ReAct 智能体对外调用大模型时使用的全部提示词：
1. DIRECTOR_SYSTEM_PROMPT  —— AI 主导编排器（Director）的系统提示；
2. build_director_user_prompt —— Director 单步决策的用户提示
   （布局/已执行观测/维度覆盖/工具池/剩余步数）；
3. build_ai_analyze_prompt —— AI 整体深度解读指令（读取 resources/prompts
   外置模板，含开场白与 1-5 精度档位，再追加 Agent 的溯源锚点/JSON 契约）；
4. build_map_prompt / build_reduce_prompt —— 超长全文分块提炼与综合指令；
5. build_refine_prompt(_multi) —— Verify 失败后的章节重解读指令；
6. build_vision_ocr_prompt —— 扫描件/图表页的多模态视觉逐字转写指令。

外置模板位于 resources/prompts/（用户可直接编辑），文件缺失时使用本模块
内置兜底文本，行为与模板文件保持一致。

设计约束（与产品规则一致）：
- 每个维度的输出必须是完整中文语句构成的成段内容，严禁关键词罗列；
- 数字、年份、人名、方法名逐字忠实于原文，无依据内容一律留空，严禁编造；
- JSON 值内部引用原文词句时统一使用中文引号“”，英文双引号必须转义，
  锚点换行当输出为合法 JSON 转义 \\n，避免破坏 JSON 结构；
- 参考文献是结构化条目，由本地规则权威提取，AI 不处理该维度。
"""
import json
import os

# 六个内容维度字段（与 state.DIMENSION_KEYS 一致，此处避免交叉导入直接声明）
_DIMENSION_FIELDS = (
    "research_background", "core_view", "research_method",
    "innovation_point", "research_conclusion", "reference_list",
)

# 维度 key → 中文名（规则未提供中文名时的兜底展示，也用于进度文案）
DIMENSION_LABELS = {
    "research_background": "研究背景",
    "core_view": "核心观点",
    "research_method": "研究方法",
    "innovation_point": "创新点",
    "research_conclusion": "研究结论",
    "reference_list": "参考文献",
}

# ================= 1. Director（AI 主导编排器）：系统提示 =================

DIRECTOR_SYSTEM_PROMPT = (
    "你是“文献深度解析智能体”的编排器（Director）。系统已在第一步把文献"
    "原件交给大模型通读；你的职责是在通读结果缺维度、需要表格/图片数据或"
    "通道异常时，从工具池中选择唯一一个下一步动作，或确认收尾。\n"
    "【工具使用纪律】\n"
    "1. 每轮只输出一个动作（或 finalize），只能选用工具池中的工具名，"
    "参数必须与签名一致，禁止臆造工具、参数或字段；\n"
    "2. 通读原件（ai_read_original）、布局体检（inspect_document）、"
    "关键词提取（extract_keywords）由系统自动安排，不会出现在工具池中，"
    "不要试图重复调用；参考文献（reference_list）由本地规则自动提取，"
    "不要为它安排任何动作；\n"
    "3. 只有确有必要才调用工具：某维度的结论依赖具体表格数据时才调用"
    "extract_tables；仅扫描件或页面确为图片、文字未进文本层时才逐页调用"
    "ocr_page；能用已有全文补全的维度直接调用一次 ai_deep_analyze，"
    "不要为同一维度重复解读；\n"
    "4. 已成功执行过的动作不得重复；能一步完成的不要拆解成多步；\n"
    "5. 所有启用维度都已有内容时立即输出 finalize，不要安排多余动作；"
    "剩余步数不足时优先 finalize，把结果交给校验环节；\n"
    "6. 严禁为补全维度而编造内容：任何动作都无法取得依据时选择 finalize，"
    "缺失维度留空由系统处理。\n"
    "【输出协议】\n"
    "只输出一个 JSON 对象，禁止输出 Markdown 代码块标记、注释或任何解释文字。"
    "动作形式：\n"
    '{"action": {"tool": "工具名", "args": {参数键值对}, "reason": "一句中文理由"}}\n'
    '收尾形式：{"finalize": true, "reason": "一句中文理由"}'
)

# ================= 2. Director：用户提示 =================

def build_director_user_prompt(*, layout: dict, enabled_dims: list,
                               missing_dims: list, executed: list,
                               tool_descriptions: list[str],
                               remaining_steps: int, char_count: int,
                               has_abs_text: bool,
                               table_texts_count: int = 0) -> str:
    """组装 Director 单步决策的用户提示。

    Args:
        layout: 原件布局摘要（扫描件/双栏/页数/图表页/表格页）。
        enabled_dims: 规则启用的全部维度 key。
        missing_dims: 当前仍缺 AI 内容的维度 key。
        executed: 已执行动作摘要 [{"step","tool","ok","observation"}]。
        tool_descriptions: 白名单工具池说明。
        remaining_steps: 剩余可用步数。
        char_count: 当前全文字数。
        has_abs_text: 是否已取得可用全文文本。
        table_texts_count: 已结构化提取的表格数量。
    Returns:
        用户提示字符串。
    """
    dim_text = "、".join(
        f"{DIMENSION_LABELS.get(k, k)}({k})" for k in enabled_dims
    ) or "全部六个维度"
    missing_text = "、".join(
        f"{DIMENSION_LABELS.get(k, k)}({k})" for k in missing_dims
    )
    layout_bits = [f"格式 {str(layout.get('format', '')).upper() or '未知'}"]
    if layout.get("is_scanned"):
        layout_bits.append(f"扫描件（共 {layout.get('page_count', 0)} 页）")
    if layout.get("has_two_columns"):
        layout_bits.append("双栏排版")
    figure_pages = layout.get("pages_with_images") or []
    if figure_pages:
        layout_bits.append(
            "图表页：第 " + "、".join(str(p + 1) for p in figure_pages) + " 页")
    table_pages = layout.get("table_pages") or []
    if table_pages:
        layout_bits.append(
            "体检疑似含表格页：第 "
            + "、".join(str(p + 1) for p in table_pages) + " 页")

    if executed:
        executed_lines = "\n".join(
            f"- 第{item.get('step', '?')}步 {item.get('tool', '?')}"
            f"[{'成功' if item.get('ok') else '失败'}]：{item.get('observation', '')}"
            for item in executed[-8:]
        )
    else:
        executed_lines = "- （暂无）"

    return (
        f"原件布局：{'，'.join(layout_bits)}\n"
        f"全文字数：{char_count} 字（{'已有' if has_abs_text else '尚无'}可用全文文本）\n"
        f"已结构化表格数：{table_texts_count}\n"
        f"本次启用维度：{dim_text}\n"
        f"当前仍缺内容的维度：{missing_text}\n"
        f"剩余可用步数：{remaining_steps}\n"
        f"已执行动作与观测（仅显示最近 8 步）：\n{executed_lines}\n\n"
        f"可用工具池：\n- " + "\n- ".join(tool_descriptions) + "\n\n"
        "请严格按系统提示的输出协议，给出唯一的下一步动作，"
        "或在维度已覆盖/无可行动作时输出 finalize。"
    )


# ================= 3. 外置提示词模板加载（resources/prompts/） =================

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROMPT_DIR = os.path.join(_PROJECT_ROOT, "resources", "prompts")
_prompt_cache: dict = {}

# 资源文件缺失时的兜底系统提示词（与 ai_system_prompt.txt 保持一致）
_SYSTEM_PROMPT_FALLBACK = (
    "你是一名严谨、专业的中文学术文献结构化分析助手。用户会向你提供文献的"
    "原始文件（PDF/DOC/DOCX/TXT），或从原件完整提取的全文文本；无论哪种形式，"
    "你都必须逐页、逐节通读全部内容（包括摘要、关键词、引言、方法、实验、"
    "讨论、结论与参考文献，以及表格、图注中的有效信息）后再作答，严禁仅凭"
    "文件名、标题或开头片段臆测。\n"
    "你的输出必须严格遵守以下规则：\n"
    "1. 只输出一个合法的 JSON 对象，不要输出任何解释、前后缀、注释或 "
    "Markdown 代码围栏；\n"
    "2. 忠于原件：所有事实、数据、年份、方法名、结论与数字必须以原件为唯一"
    "依据，严禁编造、补充或脑补原件中不存在的信息；\n"
    "3. 除参考文献与 keywords 外，每个字段都输出由意思完整、句末带句号的"
    "陈述句组成的段落，禁止用短语、词组或编号要点罗列代替；\n"
    "4. 输出语言与原件正文保持一致；原件为中文时一律使用简体中文。"
)

# 开场白兜底（与 ai_openings.json 保持一致）
_OPENING_FALLBACK = {
    "file": "请完整通读我上传的文献原始文件（含封面、摘要、引言、正文各节、"
            "结论与参考文献，逐页阅读不得跳读），依据原件全文完成结构化解析。要求：",
    "text": "以下提供了从文献原件中完整提取的全文文本。请先通读全文"
            "（含摘要、引言、正文各节、结论与参考文献，不得只看开头），"
            "再依据全文内容完成结构化解析。要求：",
}

# 精度档位兜底（与 ai_precision_guides.json 保持一致）
_PRECISION_FALLBACK = {
    "1": "解析精度要求：最快档，每项用 1-2 句完整句子概括最核心内容。",
    "2": "解析精度要求：快速档，每项用 2-4 句完整句子简明陈述。",
    "3": "解析精度要求：均衡档，按各项标注字数用完整句子成段作答。",
    "4": "解析精度要求：精细档，在忠于原件的前提下用完整句子展开，每项 300-400 字。",
    "5": "解析精度要求：最精细档，用完整句子全面覆盖论证细节与数据，每项 400-600 字。",
}

# 整体解析模板缺失时的最小骨架（与 ai_parse_user_prompt.txt 占位符一致）
_PARSE_TEMPLATE_FALLBACK = "【开场白】\n【精度要求】"
# 分块/综合模板缺失时的兜底（与对应 txt 保持一致）
_MAP_TEMPLATE_FALLBACK = (
    "以下是同一篇文献全文切分后的其中一个片段（分阶段阅读的素材之一）。"
    "请只依据本片段内容完成素材摘录，严格只输出一个 JSON 对象。"
)
_REDUCE_TEMPLATE_FALLBACK = (
    "以下是同一篇文献各全文片段经分阶段阅读产生的结构化素材。"
    "请以全部素材为唯一依据，综合生成最终解析结果，严格只输出一个 JSON 对象。"
)


def load_system_prompt() -> str:
    """加载系统提示词（resources/prompts/ai_system_prompt.txt）。"""
    return _load_prompt_text("ai_system_prompt.txt", _SYSTEM_PROMPT_FALLBACK)


def _load_prompt_text(filename: str, fallback: str) -> str:
    """读取提示词文本文件并缓存；文件缺失/损坏时使用内置兜底文本。"""
    if filename not in _prompt_cache:
        path = os.path.join(_PROMPT_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                content = fp.read().strip()
            _prompt_cache[filename] = content or fallback
        except OSError:
            _prompt_cache[filename] = fallback
    return _prompt_cache[filename]


def _load_prompt_json(filename: str) -> dict:
    """读取提示词配套 JSON（开场白/精度档位），失败时返回空字典由调用方兜底。"""
    cache_key = f"json:{filename}"
    if cache_key not in _prompt_cache:
        path = os.path.join(_PROMPT_DIR, filename)
        try:
            with open(path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            _prompt_cache[cache_key] = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _prompt_cache[cache_key] = {}
    return _prompt_cache[cache_key]


def _apply_dimension_switches(template: str, disabled_dims,
                              disabled_text: str) -> str:
    """按停用维度替换提示词模板中的维度字段行。

    Args:
        template: 含 ``- "field": ...`` 字段行的模板文本。
        disabled_dims: 停用维度集合/可迭代对象。
        disabled_text: 维度停用时字段说明的替换文案（不含字段名前缀）。
    Returns:
        替换后的提示词文本。
    """
    disabled = {str(dim).strip() for dim in (disabled_dims or ())}
    lines = []
    for line in template.splitlines():
        replaced = False
        for field in _DIMENSION_FIELDS:
            if field in disabled and line.startswith(f'- "{field}":'):
                lines.append(f'- "{field}": {disabled_text}')
                replaced = True
                break
        if not replaced:
            lines.append(line)
    return "\n".join(lines)


# ================= 4. AI 整体深度解读指令（外置模板 + Agent 校验契约） =================

_JSON_SAFETY = (
    "JSON 安全要求：输出必须是可被 json.loads 直接解析的合法 JSON；"
    "值内部引用原文词句时一律使用中文引号“”，确需英文双引号时必须转义为 \\\"；"
    "段落与锚点之间的换行在 JSON 字符串中写为转义的 \\n，"
    "不要在 JSON 外输出任何文字。"
)

_CITATION_RULE = (
    "原文溯源标注：每个维度的成段正文之后，用转义换行 \\n 另起一行，"
    "以「[原文位置：段落号1, 段落号2]」标注 1-3 个最直接支撑该段结论的原文段落号；"
    "段落号按空行对随后文本分段、从 0 开始计数，只写阿拉伯数字、用英文逗号分隔；"
    "锚点必须真实存在且确实支撑该段内容，找不到支撑段落时不要编造，该维度留空。"
)


def _citation_rule(from_original: bool = False) -> str:
    """溯源锚点说明；原件直传模式下按"在原件中读到的自然段落"编号。"""
    if not from_original:
        return _CITATION_RULE
    return (
        "原文溯源标注：每个维度的成段正文之后，用转义换行 \\n 另起一行，"
        "以「[原文位置：段落号1, 段落号2]」标注 1-3 个最直接支撑该段结论的原文段落号；"
        "段落号按你在文献原件中读到的自然段落（以空行分隔）、从 0 开始计数，"
        "只写阿拉伯数字、用英文逗号分隔；"
        "锚点必须真实存在且确实支撑该段内容，找不到支撑段落时不要编造，该维度留空。"
    )


def _disabled_set(disabled_dims) -> set:
    """把逗号分隔字符串/可迭代对象规范为合法维度 key 集合。"""
    if isinstance(disabled_dims, str):
        disabled_dims = disabled_dims.split(",")
    return {
        str(dim).strip() for dim in (disabled_dims or ())
        if str(dim).strip() in _DIMENSION_FIELDS
    }


def build_ai_analyze_prompt(focus: str = "",
                            from_original: bool = False,
                            precision: int = 3,
                            disabled_dims=None) -> str:
    """组装 ai_deep_analyze / ai_read_original 工具的结构化输出指令。

    指令主体读取用户可编辑的外置模板 resources/prompts/ai_parse_user_prompt.txt
    （开场白随原件/文本通道切换、精度档位 1-5 可调、停用维度字段行替换），
    再追加 Agent 三重校验所需的溯源锚点与 JSON 安全契约。

    Args:
        focus: 解读范围——空串表示全部六个维度；单个维度 key 表示仅解读该
            维度（Refine 重解读）；逗号分隔的多个 key 表示只解读这些维度
            （分维度策略：如参考文献交给本地正则时只解读其余五个维度）。
        from_original: True 表示模型将直接收到文献**原件文件**
            （file_id/file_data 通道），使用原件开场白；False（默认）表示
            随后给出的是本地提取的纯文本。
        precision: 解析精度 1-5，从外置 ai_precision_guides.json 取档位说明。
        disabled_dims: 规则中停用的维度（逗号字符串或可迭代），模板对应
            字段行替换为"填空字符串"。
    Returns:
        指令文本（原件模式由多模态消息附带文件；文本模式由
        chat_with_text 拼接全文在后）。
    """
    disabled = _disabled_set(disabled_dims)
    precision_key = str(max(1, min(5, int(precision))))
    openings = _load_prompt_json("ai_openings.json") or _OPENING_FALLBACK
    opening = openings.get(
        "file" if from_original else "text",
        _OPENING_FALLBACK["file" if from_original else "text"],
    )
    guides = _load_prompt_json("ai_precision_guides.json") or _PRECISION_FALLBACK
    precision_guide = guides.get(
        precision_key, _PRECISION_FALLBACK["3"])

    template = _load_prompt_text(
        "ai_parse_user_prompt.txt", _PARSE_TEMPLATE_FALLBACK)
    lines = []
    for line in template.splitlines():
        stripped = line.strip()
        if stripped == "【开场白】":
            lines.append(opening)
        elif stripped == "【精度要求】":
            lines.append(precision_guide)
        else:
            lines.append(line)
    body = _apply_dimension_switches(
        "\n".join(lines), disabled, '该维度未启用，填空字符串 ""')

    # focus 契约：外置模板默认要求全部维度，这里把解读范围收敛到指定维度
    focus_keys = [
        k.strip() for k in (focus or "").split(",")
        if k.strip() in _DIMENSION_FIELDS
    ]
    scope_line = ""
    if focus_keys:
        labels = "、".join(DIMENSION_LABELS.get(k, k) for k in focus_keys)
        scope_line = (
            f"\n【本次解读范围】只输出以下维度：{labels}；"
            "其余维度（不含 keywords）一律输出空字符串 \"\"。"
        )

    return (
        f"{body}{scope_line}\n"
        f"{_citation_rule(from_original)}\n"
        f"{_JSON_SAFETY}"
    )


def build_map_prompt(disabled_dims=None) -> str:
    """超长全文分块提炼（map 阶段）指令：只摘录片段内出现过的完整句子素材。

    模板：resources/prompts/ai_map_user_prompt.txt。另约定每个原文段落带
    「【段落n】」全局段落号标记，摘录素材必须原样保留该标记，供综合阶段
    生成可被溯源校验验证的原文锚点。

    Args:
        disabled_dims: 停用维度（逗号字符串或可迭代）。
    Returns:
        map 阶段指令文本。
    """
    template = _load_prompt_text(
        "ai_map_user_prompt.txt", _MAP_TEMPLATE_FALLBACK)
    body = _apply_dimension_switches(
        template, _disabled_set(disabled_dims), "该维度未启用，固定给 []")
    marker_rule = (
        "\n段落编号约定：片段中每个原文段落前都标注了【段落n】，"
        "n 是该段落在全文中的全局段落号。摘录每条素材时必须在句首原样保留"
        "其所属段落的【段落n】标记（形如「【段落12】素材原句……」），"
        "无法确定所属段落的素材不要输出。"
    )
    return f"{body}{marker_rule}\n{_JSON_SAFETY}"


def build_reduce_prompt(precision: int = 3, disabled_dims=None,
                        from_original: bool = False) -> str:
    """超长全文综合（reduce 阶段）指令：合并各片段素材产出最终六维度 JSON。

    模板：resources/prompts/ai_reduce_user_prompt.txt。最终输出同样遵守
    Agent 的溯源锚点契约，锚点段落号只能取自素材中的【段落n】标记。

    Args:
        precision: 解析精度 1-5。
        disabled_dims: 停用维度（逗号字符串或可迭代）。
        from_original: 保留参数对称（综合阶段始终基于文本素材）。
    Returns:
        reduce 阶段指令文本。
    """
    _ = precision, from_original
    template = _load_prompt_text(
        "ai_reduce_user_prompt.txt", _REDUCE_TEMPLATE_FALLBACK)
    body = _apply_dimension_switches(
        template, _disabled_set(disabled_dims), '该维度未启用，填空字符串 ""')
    anchor_rule = (
        "\n原文溯源标注：每个维度的成段正文之后，用转义换行 \\n 另起一行，"
        "以「[原文位置：段落号1, 段落号2]」标注 1-3 个最直接支撑该段结论的"
        "原文段落号；段落号只能引用素材条目前缀【段落n】中的 n，"
        "严禁编造素材中未出现的段落号；找不到支撑段落的维度留空。"
    )
    return f"{body}{anchor_rule}\n{_JSON_SAFETY}"


# ================= 4. Verify 失败后的章节重解读指令 =================

def build_refine_prompt(dimension: str) -> str:
    """让 AI 基于随后给出的章节原文重新解读单维度的指令（反幻觉回环）。

    触发场景：该维度上一轮解读未通过三重校验——与原文章节明显不一致，
    或未标注/标注了越界的原文锚点。章节原文不内嵌在指令里，而是经
    chat_with_text 的全文通道发送，与普通解析保持同一条消息拼接路径。

    Args:
        dimension: 需重解读的维度 key。
    Returns:
        指令文本。
    """
    label = DIMENSION_LABELS.get(dimension, dimension)
    return (
        f"你正在对一篇中文学术文献的「{label}」维度进行重新解读。"
        "你上一轮对该维度的解读未通过校验：内容与原文章节不一致，"
        "或原文段落锚点缺失、越界。请严格按以下要求重写：\n"
        "1. 只允许依据随后给出的章节原文作答，"
        "禁止引入原文之外的数据、人名、结论或常识性套话；\n"
        "2. 输出 3-6 句完整中文语句组成的连贯段落，不得只写关键词；\n"
        "3. 数字、百分比、年份、专有名词逐字与原文一致；\n"
        "4. 随后给出的材料中每个原文段落前都带有【段落n】标记，"
        "n 是该段落在全文中的全局段落号；正文之后用转义换行 \\n 另起一行，"
        "以「[原文位置：段落号1, 段落号2]」标注 1-3 个支撑段落，"
        "段落号必须且只能引用材料中实际出现的【段落n】编号，严禁自行从 0 编号；\n"
        "5. 材料中找不到依据时，值输出空字符串 \"\"，严禁编造锚点。\n"
        "只输出一个 JSON 对象，不要输出其他文字：\n"
        "{\"" + dimension + "\": \"你的完整中文段落……\\n[原文位置：12]\"}\n"
        + _JSON_SAFETY
    )


def build_refine_prompt_multi(dimensions: list) -> str:
    """构造多维度合并重解读指令（mixed_section 通道，一次 AI 调用补救多个维度）。

    触发场景：上一轮校验中多个维度同时存在内容缺失、与原文不一致或溯源锚点
    失效。随附材料按维度给出章节原文（本地缺失的维度会附文献全文作为底本），
    模型在一次调用内只重写指定维度，把串行的 N 次补救合并为 1 次。

    Args:
        dimensions: 需要重解读的维度 key 列表（2 个及以上时使用本通道）。
    Returns:
        指令文本；维度全非法时返回空串。
    """
    keys: list[str] = []
    labels: list[str] = []
    for dim in dimensions:
        key = (dim or "").strip()
        if key in DIMENSION_LABELS:
            keys.append(key)
            labels.append(f'"{key}"（{DIMENSION_LABELS[key]}）')
    if not keys:
        return ""
    example = ("{\"" + keys[0] + "\": \"……完整段落……\\n[原文位置：n]\""
               + ("" if len(keys) == 1
                  else ", \"" + keys[-1] + "\": \"……完整段落……\\n[原文位置：n]\"")
               + "}")
    return (
        "你是严谨的中文学术文献分析专家。上一轮解析中，以下维度未通过校验"
        "（内容缺失，或解读与原文不一致、原文段落锚点缺失/越界）：\n"
        f"  {', '.join(labels)}\n"
        "请仅依据随后给出的、带【段落n】全局编号标记的原文材料，重新解读这些"
        "维度，其余维度一律不要输出。要求：\n"
        "1. 每个维度输出 3-6 句完整中文语句组成的连贯段落，不得只写关键词；\n"
        "2. 严格忠于材料：数字、百分比、年份、人名、机构、结论逐字沿用，"
        "材料未提及的信息一律不写；\n"
        "3. 每个维度在正文之后用转义换行 \\n 另起一行，"
        "以「[原文位置：段落号1, 段落号2]」标注 1-3 个支撑段落；"
        "段落号必须且只能引用材料中段落前缀【段落n】里的 n（全文全局段落号），"
        "严禁自行从 0 重新编号；\n"
        "4. 只返回一个 JSON 对象，键仅包含：" + "、".join(keys) + "，不要输出其他文字：\n"
        + example + "\n"
        + _JSON_SAFETY
    )


# ================= 5. 视觉 OCR：扫描件/图表页逐字转写 =================

def build_vision_ocr_prompt(page_index: int = None,
                            page_count: int = None,
                            is_figure: bool = False) -> str:
    """构造多模态视觉 OCR 指令：只逐字转写，不做总结或解读。

    Args:
        page_index: 当前页码（从 0，用于提示模型定位），可为 None。
        page_count: 总页数，可为 None。
        is_figure: True 表示该页以图表/公式为主，提示保留表格行列与图内标注。
    Returns:
        OCR 指令文本。
    """
    page_hint = ""
    if page_index is not None:
        page_hint = (
            f"这是文献第 {page_index + 1}/{page_count} 页。"
            if page_count else f"这是文献第 {page_index + 1} 页。"
        )
    figure_rule = (
        "本页以图表、公式或示意图为主：表格请按行列逐行转写（单元格之间用空格、"
        "换行分隔），坐标轴/图例/流程图中的文字标注也要逐字给出；"
        if is_figure else
        "本页为普通正文页："
    )
    return (
        "你是严谨的文献 OCR 转写引擎。" + page_hint + figure_rule +
        "请忠实、完整地把图片中可见的全部文字转写为纯文本，要求：\n"
        "1. 严格按人工阅读顺序输出：双栏排版先左栏自上而下、再右栏，"
        "不要跨栏串行；段落换行保持原样；\n"
        "2. 中文、英文、数字、标点、上下标、公式符号逐字照录，"
        "不纠错、不翻译、不补充、不省略；\n"
        "3. 页眉页脚的页码、期刊名等噪声原样保留；\n"
        "4. 只输出转写得到的文本本身，不要输出任何解释、说明或 Markdown 标记，"
        "无法辨认的字用□占位。"
    )


# ================= 6. 原件通读中的 function calling 读中工具循环 =================

_FC_TOOL_DISCIPLINE = (
    "你在通读过程中可以调用以下本地中间件工具辅助解析（通过工具调用，不要凭空猜测）：\n"
    "- extract_tables：当某个维度需要引用表格中的精确数据（实验数据、指标对比、"
    "调查统计等），按页码提取表格为 Markdown；\n"
    "- ocr_page：当某页是扫描图像、复杂图表或公式密集页，你从原件中看不清该页"
    "文字时，对该页做视觉 OCR 取字；\n"
    "- extract_text：当你需要可逐段引用的纯文本（用于精确标注原文段落号）时，"
    "提取按段落编号的本地全文；\n"
    "- detect_structure：当你需要本地规则识别的章节切分与参考文献线索作为"
    "交叉验证时调用。\n"
    "工具纪律：\n"
    "1. 工具是辅助取证手段，不是必经步骤：仅凭原件就能完整、准确作答时，"
    "不要调用任何工具，直接输出最终 JSON；\n"
    "2. 同一工具同一参数不要重复调用；页码从 0 开始且不得越界；\n"
    "3. 每次只安排你真正需要的工具，取证完成后必须在随后回复中直接输出"
    "最终结构化 JSON（不要输出工具调用以外的闲聊文字）；\n"
    "4. 参考文献条目、关键词以你在原件中的真实阅读为准，不得因为工具失败"
    "而编造内容。"
)


def _layout_summary(page_count: int, is_scanned: bool,
                    table_pages, figure_pages) -> str:
    """把体检关键发现渲染为一句话摘要（供 FC 首轮提示与 Plan B 取证提示）。"""
    parts = [f"共 {int(page_count)} 页" if page_count else "页数未知"]
    if is_scanned:
        parts.append("疑似扫描件（部分页面可能无文本层）")
    if table_pages:
        pages = "、".join(str(int(p) + 1) for p in table_pages)
        parts.append(f"疑似含表格页：第 {pages} 页")
    if figure_pages:
        pages = "、".join(str(int(p) + 1) for p in figure_pages)
        parts.append(f"疑似含图表页：第 {pages} 页")
    return "；".join(parts)


def build_original_fc_prompt(focus: str = "", precision: int = 3,
                             disabled_dims=None, page_count: int = 0,
                             is_scanned: bool = False,
                             table_pages=(), figure_pages=()) -> str:
    """Plan A：原件与 tools 同框时的首轮通读指令（含读中工具纪律）。

    Args:
        focus: 解读范围（维度 key 逗号串，空串为六维度）。
        precision: 解析精度 1-5。
        disabled_dims: 规则停用维度。
        page_count: 体检得到的总页数。
        is_scanned: 是否疑似扫描件。
        table_pages: 体检疑似表格页索引列表。
        figure_pages: 体检疑似图表页索引列表。
    Returns:
        首轮 user 文本：体检摘要 + 工具纪律 + 结构化输出契约。
    """
    base = build_ai_analyze_prompt(
        focus, from_original=True, precision=precision,
        disabled_dims=disabled_dims)
    layout = _layout_summary(page_count, is_scanned, table_pages, figure_pages)
    return (
        f"【原件体检摘要】{layout}。\n"
        f"{_FC_TOOL_DISCIPLINE}\n"
        "===== 最终输出要求 =====\n"
        f"{base}"
    )


def build_staged_probe_prompt(page_count: int = 0, is_scanned: bool = False,
                              table_pages=(), figure_pages=()) -> str:
    """Plan B 阶段1：仅文本 function calling 取证规划（模型此阶段看不到原件）。

    Args:
        page_count/is_scanned/table_pages/figure_pages: 体检结果。
    Returns:
        取证轮 user 文本：只允许调度工具收集材料，不输出解析报告。
    """
    layout = _layout_summary(page_count, is_scanned, table_pages, figure_pages)
    return (
        "【任务背景】系统即将解析一篇文献原件，但当前模型通道要求先在本地"
        "完成辅助取证、再把原件交给你通读。你现在看不到原件正文，只能依据"
        "以下体检摘要，决定是否需要先调用本地工具收集材料。\n"
        f"【原件体检摘要】{layout}。\n"
        f"{_FC_TOOL_DISCIPLINE}\n"
        "本阶段禁止输出任何解析结论或维度内容：需要取证就发起工具调用；"
        "认为无需取证或取证已完成时，用一句话说明已取得的材料清单即可，"
        "系统随后会把原件与全部取证材料一并交给你做最终解读。"
    )


def build_staged_final_prompt(evidence_text: str, focus: str = "",
                              precision: int = 3,
                              disabled_dims=None) -> str:
    """Plan B 阶段2：原子通读指令（原件 + 阶段1 取证材料汇编）。

    Args:
        evidence_text: 阶段1 各工具执行结果的汇编文本（空串表示无取证）。
        focus/precision/disabled_dims: 同 build_ai_analyze_prompt。
    Returns:
        原子通读 user 文本（不再携带 tools）。
    """
    base = build_ai_analyze_prompt(
        focus, from_original=True, precision=precision,
        disabled_dims=disabled_dims)
    evidence_block = ""
    if (evidence_text or "").strip():
        evidence_block = (
            "===== 读前已完成的本地取证材料（开始） =====\n"
            f"{evidence_text.strip()}\n"
            "===== 读前已完成的本地取证材料（结束） =====\n"
            "请结合上述取证材料与文献原件完成解读；材料仅作事实补充，"
            "一切结论以原件内容为准。\n"
        )
    return f"{evidence_block}{base}"
