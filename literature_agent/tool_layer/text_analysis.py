"""文本分析工具：jieba 分词、关键词提取、段落切分、文献结构识别、章节语句归一化。

纯函数工具模块，不访问数据库，不包含业务判断。
结构化维度产出的一律是原文中完整、成段的语句；
关键词仅作为报告的附加信息单独提取，不参与任何维度内容。
"""
import re

from utils.logger import get_logger

logger = get_logger()

# 句末标点（中英文；不含英文句点 "."，避免参考文献缩写 "Y." 被误判为句末）
_SENTENCE_ENDINGS = "。！？!?”’"
# 参考文献/编号条目前导标记：命中时条目必须独立成行
_LIST_ITEM_PATTERN = re.compile(
    r"^\s*(?:\[\s*\d+\s*\]|\(\s*\d+\s*\)|（\s*\d+\s*）|\d+\s*[\.、)]|[▪•·\-*]\s+)"
)

# 结构维度 → 章节标题正则（中英文，允许编号前缀，如 "1 引言"、"三、研究方法"）
_SECTION_PATTERNS = {
    "research_background": (
        r"^\s*(?:[0-9一二三四五六七八九十]+[\.、\s]*)?"
        r"(?:摘\s*要|内容提要|引\s*言|绪\s*论|研究背景|前\s*言|"
        r"abstract|introduction|background)\s*[:：]?\s*$"
    ),
    "core_view": (
        r"^\s*(?:[0-9一二三四五六七八九十]+[\.、\s]*)?"
        r"(?:核心观点|主要观点|核心内容|主要内容|观点综述|内容综述)\s*[:：]?\s*$"
    ),
    "research_method": (
        r"^\s*(?:[0-9一二三四五六七八九十]+[\.、\s]*)?"
        r"(?:研究方法|研究设计|材料与方法|方法与材料|方\s*法|实验方法|实验设计|"
        r"methods?(?:\s+and\s+materials)?|materials\s+and\s+methods)\s*[:：]?\s*$"
    ),
    "innovation_point": (
        r"^\s*(?:[0-9一二三四五六七八九十]+[\.、\s]*)?"
        r"(?:创新点|研究创新|主要创新|创新之处|innovations?)\s*[:：]?\s*$"
    ),
    "research_conclusion": (
        r"^\s*(?:[0-9一二三四五六七八九十]+[\.、\s]*)?"
        r"(?:研究结论|结\s*论|结\s*语|总\s*结|展望|研究展望|"
        r"conclusions?|summary)\s*[:：]?\s*$"
    ),
    "reference_list": (
        r"^\s*(?:[0-9一二三四五六七八九十]+[\.、\s]*)?"
        r"(?:参\s*考\s*文\s*献|references|bibliography)\s*[:：]?\s*$"
    ),
}

# 章节识别顺序（参考文献必须最先截断后续内容）
_SECTION_ORDER = (
    "research_background",
    "core_view",
    "research_method",
    "innovation_point",
    "research_conclusion",
    "reference_list",
)


def segment_text(text: str) -> list:
    """jieba 中文分词。

    Args:
        text: 原始文本。
    Returns:
        词语列表（已过滤空白与纯标点）。
    """
    import jieba

    words = [w.strip() for w in jieba.lcut(text or "")]
    return [w for w in words if w and not re.fullmatch(r"[\s\W_]+", w, flags=re.UNICODE)]


def extract_keywords(text: str, top_n: int = 20) -> list:
    """TF-IDF 关键词提取（报告附加信息，不属于任何解析维度内容）。

    Args:
        text: 原始文本。
        top_n: 返回关键词数量上限。
    Returns:
        [(关键词, 权重), ...]，按权重降序。
    """
    if not text or not text.strip():
        return []
    try:
        import jieba.analyse

        return jieba.analyse.extract_tags(
            text, topK=max(1, int(top_n)), withWeight=True
        )
    except Exception as exc:
        logger.warning("jieba 关键词提取失败，降级为词频统计：%s", exc)
        return _fallback_keywords(text, top_n)


def _fallback_keywords(text: str, top_n: int) -> list:
    """jieba.analyse 不可用时的简易词频降级方案。"""
    from collections import Counter

    words = [w for w in segment_text(text) if len(w) >= 2]
    counter = Counter(words)
    total = sum(counter.values()) or 1
    return [(word, count / total) for word, count in counter.most_common(top_n)]


def split_into_paragraphs(text: str) -> list:
    """按换行/空行切分原文段落。

    Args:
        text: 原始全文。
    Returns:
        [{"pos": "0", "content": "段落文本"}, ...]，pos 为段落序号字符串，
        用于 literature_note.paragraph_pos 锚定。
    """
    if not text:
        return []
    paragraphs = []
    for raw in text.splitlines():
        content = raw.strip()
        if content:
            paragraphs.append({"pos": str(len(paragraphs)), "content": content})
    return paragraphs


def _global_paragraph_spans(lines: list) -> list:
    """计算全局段落（空行分隔的连续非空行块）及其行跨度。

    切分规则与 agent.verifiers._split_paragraphs 完全同构
    （split("\\n\\n") 后过滤空白段），保证这里给出的段落号就是
    AI 锚点[原文位置：n]校验时使用的全局段落号。

    Args:
        lines: text.splitlines() 得到的行列表。
    Returns:
        [(start_line, end_line, paragraph_text), ...]，段落号即列表下标。
    """
    spans = []
    index = 0
    total = len(lines)
    while index < total:
        while index < total and not lines[index].strip():
            index += 1
        if index >= total:
            break
        start = index
        buf = []
        while index < total and lines[index].strip():
            buf.append(lines[index])
            index += 1
        spans.append((start, index - 1, "\n".join(buf).strip()))
    return spans


def detect_structure(text: str, precision: int = 3) -> dict:
    """识别文献章节结构。

    Args:
        text: 文献全文。
        precision: 解析精度 1-5（规则模板中配置）。
            1-2 为快速档：仅按章节标题严格切分，不做段落猜测兜底；
            3 为均衡档：未命中的维度用保守启发式补全；
            4-5 为精细档：核心观点等兜底内容保留更长（900/1200 字），
            语义提取更全面，耗时也更长。
    Returns:
        {
            "research_background": str, "core_view": str,
            "research_method": str, "innovation_point": str,
            "research_conclusion": str, "reference_list": str,
        }
        未识别到的维度为空字符串。
    """
    structure, _anchors = detect_structure_with_anchors(text, precision)
    return structure


def detect_structure_with_anchors(text: str, precision: int = 3) -> tuple:
    """识别章节结构，并给出每个维度章节覆盖的全局段落号。

    段落号与 verifiers._split_paragraphs 对 abs_text 的空行切分同构，
    供 Agent Refine 补救时渲染【段落n】全局锚点标记，避免"章节内局部
    编号 vs 全文全局编号"错位导致溯源校验必败。

    Args:
        text: 文献全文。
        precision: 解析精度 1-5。
    Returns:
        (structure, section_paragraphs)：
        structure 为六维度章节原文字典；
        section_paragraphs 为 {维度key: [全局段落号, ...]}，
        未识别到内容的维度给空列表。
    """
    result = {key: "" for key in _SECTION_ORDER}
    section_paragraphs = {key: [] for key in _SECTION_ORDER}
    if not text or not text.strip():
        return result, section_paragraphs

    try:
        precision = max(1, min(5, int(precision)))
    except (TypeError, ValueError):
        precision = 3

    lines = text.splitlines()
    # 收集所有命中的章节标题：(行号, 维度)
    hits = []
    seen = set()
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or len(stripped) > 30:
            continue
        for dimension in _SECTION_ORDER:
            if dimension in seen:
                continue
            if re.match(_SECTION_PATTERNS[dimension], stripped, flags=re.IGNORECASE):
                hits.append((idx, dimension))
                seen.add(dimension)
                break

    # 每个标题命中维度的正文行区间（标题行的下一非空行到下个标题行）
    hit_ranges = {}
    hits.sort(key=lambda item: item[0])
    for order, (line_idx, dimension) in enumerate(hits):
        end_line = hits[order + 1][0] if order + 1 < len(hits) else len(lines)
        section_lines = lines[line_idx + 1:end_line]
        content = "\n".join(section_lines).strip()
        result[dimension] = normalize_section_text(dimension, content)
        hit_ranges[dimension] = (line_idx + 1, end_line)

    # 快速档（1-2）：仅保留标题精确命中的结构，不做猜测性补全
    fill_sources = {}
    if precision >= 3:
        core_cap = 1200 if precision >= 5 else (900 if precision == 4 else 600)
        fill_sources = _fill_fallback_sections(text, result, hits, core_cap)

    section_paragraphs = _map_sections_to_global_paragraphs(
        lines, result, hit_ranges, fill_sources)
    return result, section_paragraphs


def _map_sections_to_global_paragraphs(lines: list, result: dict,
                                       hit_ranges: dict,
                                       fill_sources: dict) -> dict:
    """把每个维度的章节文本映射回全局段落号列表。

    候选行来源两部分并集：标题命中维度的正文行区间、兜底补全实际取材的行。
    再用"去空白后行原文包含在最终章节文本中"做逐行确认，剔除被句界截断
    丢弃的尾段与仅标题行成块的噪声，保证映射到的段落确实支撑该维度内容。

    Args:
        lines: 全文按行切分的结果。
        result: 六维度章节文本（兜底补全已完成）。
        hit_ranges: {维度: (起始行, 结束行)}。
        fill_sources: {维度: [兜底取材行号, ...]}。
    Returns:
        {维度key: [全局段落号, ...]}，按段落号升序。
    """
    spans = _global_paragraph_spans(lines)
    section_map = {}
    for dimension in _SECTION_ORDER:
        content = result.get(dimension) or ""
        if not content.strip():
            section_map[dimension] = []
            continue
        content_flat = re.sub(r"\s+", "", content)
        range_start, range_end = hit_ranges.get(dimension, (-1, -1))
        source_lines = set(fill_sources.get(dimension) or [])
        indices = []
        for para_idx, (start, end, _text) in enumerate(spans):
            line_range = range(start, end + 1)
            in_hit_range = (
                range_start >= 0
                and any(range_start <= ln < range_end for ln in line_range)
            )
            if not in_hit_range and not any(
                    ln in source_lines for ln in line_range):
                continue
            # 逐行确认：块内至少一行的原文（去空白）出现在最终章节文本中
            confirmed = any(
                re.sub(r"\s+", "", lines[ln].strip()) in content_flat
                for ln in line_range if lines[ln].strip()
            )
            if confirmed:
                indices.append(para_idx)
        section_map[dimension] = indices
    return section_map



def normalize_section_text(dimension: str, content: str) -> str:
    """把章节原文整理为成段的完整语句（解析各维度统一出口）。

    - 普通维度：将 PDF/Word 提取时被硬换行拆散的同一句话拼回完整段落，
      已在句末结束的行、编号条目行保留独立换行；
    - 参考文献：逐条独立成行，仅去除空白行。

    Args:
        dimension: 结构维度 key（reference_list 走条目规则，其余走段落规则）。
        content: 章节原始文本。
    Returns:
        归一化后的成段/逐条文本；入参为空时返回空字符串。
    """
    if not content or not content.strip():
        return ""
    if dimension == "reference_list":
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        return "\n".join(lines)
    return _merge_wrapped_paragraphs(content)


def _merge_wrapped_paragraphs(content: str) -> str:
    """合并被换行拆散的语句，还原"一段段完整语句"的结构。

    Args:
        content: 章节原文（可能含 PDF/Word 提取产生的硬换行）。
    Returns:
        段落文本：段内语句完整拼接，段落与编号条目之间保留换行。
    """
    paragraphs = []
    for block in re.split(r"\n\s*\n", content):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        merged = lines[0]
        for line in lines[1:]:
            if _starts_new_line(merged, line):
                merged += "\n" + line
            elif _is_ascii_word_char(merged[-1]) and _is_ascii_word_char(line[0]):
                merged += " " + line
            else:
                merged += line
        paragraphs.append(merged)
    return "\n".join(paragraphs)


def _starts_new_line(previous: str, current: str) -> bool:
    """判断当前行是否应另起一行（上一行句意完整，或当前行是编号条目）。"""
    if previous[-1] in _SENTENCE_ENDINGS or previous.endswith(("：", ":")):
        return True
    if _LIST_ITEM_PATTERN.match(current):
        return True
    return False


def _is_ascii_word_char(char: str) -> bool:
    """判断字符是否为 ASCII 字母/数字（用于决定拼接换行时是否补空格）。"""
    return bool(re.fullmatch(r"[A-Za-z0-9]", char or ""))


def truncate_at_sentence_boundary(text: str, max_chars: int) -> str:
    """超长文本只在完整句子边界截断，避免留下半句话或短语碎片。

    Args:
        text: 已归一化的段落文本。
        max_chars: 保留字数上限。
    Returns:
        截断后的文本；长度未超限时原样返回。
    """
    if not text or len(text) <= max_chars:
        return text
    window = text[:max_chars]
    cut = max(window.rfind(mark) for mark in _SENTENCE_ENDINGS + "\n")
    # 至少保留一半预算内的最后一个完整句子，找不到句界时才硬截
    if cut >= max_chars // 2:
        return window[:cut + 1].strip()
    return window.strip()


# 期刊元信息/脚注行：既不是正文也不是结论，兜底取材一律跳过
_META_LINE_RE = re.compile(
    r"^\s*(?:《[^》]+》|摘\s*要\s*[:：]?|关键词\s*[:：]|中图分类号|"
    r"文献标识码|文章编号|基金项目|作者简介|作者单位|收稿日期|"
    r"责\s*编\s*[:：]|●)")
_FOOTNOTE_LINE_RE = re.compile(
    r"(?:基金项目|作者简介|作者单位|收稿日期|责\s*编\s*[:：])")
# 研究方法章节的内容指示词（命中行可作方法维度兜底取材）
_METHOD_HINT_RE = re.compile(
    r"研究?方法|采用.{0,12}(?:方法|案例|分析)|案例(?:分析|研究)|实证(?:研究|分析)|"
    r"实验|问卷|访谈|数据(?:采集|来源)|分析框架|设计实践|研究设计")
# 创新点章节的内容指示词（不收"模块化设计"这类标题常用词，避免误选论文标题）
_INNOVATION_HINT_RE = re.compile(
    r"创新|新颖|首次|突破点|设计思路|本文提出|提出了?一种")
# 行内粘连的结语标题（期刊排版常把"四、结语"挤进参考文献后的行里）
_INLINE_CONCLUSION_RE = re.compile(
    r"(?:[一二三四五六七八九十]+\s*[、.．]|[（(]\s*[一二三四五六七八九十]+\s*[）)])"
    r"\s*(?:结\s*语|结\s*论|总\s*结)")
# 摘要块：起始前缀与终止标记（关键词/中图分类号等）
_ABSTRACT_PREFIX_RE = re.compile(r"^\s*摘\s*要\s*[:：]?\s*")
_ABSTRACT_STOP_RE = re.compile(
    r"^\s*(?:关\s*键\s*词\s*[:：]?|中图分类号|文献标识码|文章编号|Abstract\b)",
    re.IGNORECASE)
# 行内脚注碎片：双栏拼接时"作者简介/基金项目"常与正文同行
_FOOTNOTE_INLINE_RE = re.compile(
    r"\s*(?:[（(\[]\s*)?(?:作者简介|基金项目|作者单位|收稿日期)"
    r"|[，,；;]?\s*主要研究方向\s*[:：]")
# 正文碎片尾部粘连的作者职称（右栏作者简介跨行残留）。注意：栏间往往
# 没有标点分隔，单位名词与正文无法可靠区分，故只剥职称词本身，保留前文，
# 宁可残留"财务处"三字噪声也不误删正文
_FOOTNOTE_ORG_TITLE_RE = re.compile(
    r"(?:副教授|副研究员|讲师|教授|研究员|博士|硕士)")
# 参考文献条目/期刊续行/图表行（文末噪声区回扫时跳过）
_REF_ITEM_LINE_RE = re.compile(r"^\s*\[\s*\d+\s*\]")
_REF_CONT_LINE_RE = re.compile(
    r"^\s*(?:\[[A-Z]{1,4}\]\.?"           # [J]. / [M] 等
            r"|.*?(?:19|20)\d{2}\s*[，,（(]"  # 含出版年的引用行
            r"|.*?\d{1,4}\s*[-–]\s*\d{1,4})"  # 含页码区间
)
_FIGURE_LINE_RE = re.compile(r"^\s*[图表]\s*\d*[\s.．、]")
_REF_HEADING_RE = re.compile(r"^\s*参\s*考\s*文\s*献\s*[:：]?\s*$")
# 标题样式行（无任何标点的短行，如论文标题、图中标签；允许含空格。
# 阈值 22 字：期刊正文换行行宽普遍 24 字以上，避免误杀正文续行）
_TITLE_LIKE_RE = re.compile(r"^[^。！？，,；;：:、…]{4,22}$")
# 编号前缀标题行（一、 / （二） / 1. / 第X章 开头）
_HEADING_PREFIX_RE = re.compile(
    r"^\s*(?:"
    r"[一二三四五六七八九十]+\s*[、.．]"
    r"|[（(]\s*[一二三四五六七八九十0-9]+\s*[）)]"
    r"|\d+(?:\.\d+)*\s*[、.．]"
    r"|第\s*[一二三四五六七八九十0-9]+\s*[章节]"
    r")")


def _fill_fallback_sections(text: str, result: dict, hits: list,
                            core_cap: int = 600) -> dict:
    """对规则未命中的维度做保守兜底，保证常见文献也能产出成段语句。

    Args:
        text: 全文。
        result: 结构识别结果（原地补全）。
        hits: 已命中的章节标题列表。
        core_cap: 核心观点兜底内容的最大保留字数（随解析精度变化）。
    Returns:
        {维度key: [兜底取材行号, ...]}，供章节→全局段落映射使用；
        未实际兜底的维度不出现在字典中。
    """
    # 非空行（行号, 内容），兜底取材均来自这些行
    content_lines = [(idx, line.strip())
                     for idx, line in enumerate(text.splitlines()) if line.strip()]
    if not content_lines:
        return {}
    sources = {}

    # 参考文献标题之后的内容（含可能粘连的结语）不参与背景/方法/创新取材
    ref_hit = next((idx for idx, dim in hits if dim == "reference_list"), None)
    body_lines = [
        (idx, content) for idx, content in content_lines
        if ref_hit is None or idx < ref_hit
    ]
    if not body_lines:
        body_lines = content_lines

    def _record(dim: str, line_indices: list) -> None:
        """登记兜底维度实际引用的非空行号。"""
        if line_indices:
            sources[dim] = line_indices

    def _is_meta(content: str) -> bool:
        """是否为期刊元信息/栏目行（不作为正文取材）。"""
        return bool(_META_LINE_RE.match(content))

    def _take_span(start_pos: int, line_count: int,
                   cap: int, candidates: list) -> tuple:
        """从候选行 start_pos 起取连续 line_count 行拼回成段并截断；
        起始行之后遇到编号标题行/噪声行即停止，避免串入下一章节。

        Returns:
            (成段文本, 实际取材行号列表)；内容为空时返回 ("", [])。
        """
        picked = []
        prev_idx = None
        crossed_gap = False
        # 取材起于编号标题行时，允许跨过一个空行接续其后正文（标题常独立成段）
        heading_start = bool(
            _HEADING_PREFIX_RE.match(candidates[start_pos][1]))
        for offset, item in enumerate(
                candidates[start_pos:start_pos + line_count]):
            idx, content = item
            gap = prev_idx is not None and idx != prev_idx + 1
            if offset > 0:
                if gap:
                    if heading_start and not crossed_gap:
                        crossed_gap = True
                    else:
                        break
                if (_is_noise_line(content)
                        or (_HEADING_PREFIX_RE.match(content)
                            and len(content) <= 30)):
                    break
            picked.append(item)
            prev_idx = idx
        indices = [idx for idx, _c in picked]
        merged = _merge_wrapped_paragraphs(
            "\n".join(content for _i, content in picked))
        return truncate_at_sentence_boundary(merged, cap), indices

    def _strip_footnote(content: str) -> str:
        """剥除双栏拼接残留的同行脚注碎片，只留正文前缀。

        如"…所以在细分财务处讲师，主要研究方向：…]"→"…所以在细分"；
        栏间常无标点，单位+职称按机构后缀词整体识别剥除。
        """
        head = _FOOTNOTE_INLINE_RE.split(content)[0]
        title_matches = list(_FOOTNOTE_ORG_TITLE_RE.finditer(head))
        if title_matches:
            head = head[:title_matches[-1].start()]
        return head.strip(" []（）()　\t，,；;、")

    def _is_noise_line(content: str) -> bool:
        """元信息/脚注/参考文献条目或续行/图表题注/乱码行。"""
        if _META_LINE_RE.match(content) or _FOOTNOTE_LINE_RE.search(content):
            return True
        if (_REF_ITEM_LINE_RE.match(content) or _REF_HEADING_RE.match(content)
                or _REF_CONT_LINE_RE.match(content)
                or _FIGURE_LINE_RE.match(content)):
            return True
        # 基金项目跨行续片（无"["但含编号/以括号残片结尾）
        if "编号" in content and ("：" in content or ":" in content):
            return True
        return _is_symbol_garbage_like(content)

    def _abstract_block(candidates: list, cap: int) -> tuple:
        """取材摘要块：自"摘要："行起至"关键词/中图分类号"等标记前。

        Returns:
            (成段文本, 取材行号列表)；无摘要块时返回 ("", [])。
        """
        start = next((pos for pos, (_i, c) in enumerate(candidates)
                      if _ABSTRACT_PREFIX_RE.match(c)), None)
        if start is None:
            return "", []
        block, used = [], []
        for idx, content in candidates[start:start + 14]:
            matched = _ABSTRACT_PREFIX_RE.match(content)
            line_text = content[matched.end():] if matched else content
            if not matched and (
                    _ABSTRACT_STOP_RE.match(content)
                    or _META_LINE_RE.match(content)):
                break
            if line_text.strip():
                block.append(line_text.strip())
                used.append(idx)
        merged = _merge_wrapped_paragraphs("\n".join(block))
        return truncate_at_sentence_boundary(merged, cap), used

    def _body_paragraph_before_tail(marker_idx: int) -> tuple:
        """从行内结语标题向前穿过参考文献/脚注/图表噪声区，取紧邻正文段。

        双栏期刊该段是被阅读顺序提前的结语后半段（右栏顶部）。
        按原始行（含空行）回扫：噪声区内空行忽略，一旦进入正文段，
        空行/编号标题行即视为段落边界。

        Returns:
            (按阅读顺序的正文行列表, 行号列表)。
        """
        raw_lines = text.splitlines()
        collected = []
        skipping_tail = True
        for ln in range(marker_idx - 1, -1, -1):
            content = raw_lines[ln].strip()
            if not content:
                if skipping_tail:
                    continue
                break
            if skipping_tail:
                if _is_noise_line(content):
                    continue
                # 噪声区内无标点短标签（图中文字、栏目标题等）
                if len(content) <= 15 and not re.search(
                        r"[。！？，,；;：:、（）()\d]", content):
                    continue
                skipping_tail = False
            if _is_noise_line(content) or len(content) < 10:
                break
            if _TITLE_LIKE_RE.match(content):
                break
            if _HEADING_PREFIX_RE.match(content) and len(content) <= 30:
                break
            collected.append((ln, content))
            if len(collected) >= 12:
                break
        collected.reverse()
        return ([c for _i, c in collected],
                [i for i, _c in collected])

    def _conclusion_fallback(cap: int) -> tuple:
        """结论兜底：优先行内粘连"结语"标题后的正文（剥脚注），
        并拼接标题之前紧邻的正文段；无标题时反向跳过噪声行取文末正文。
        """
        marker_pos, marker_idx = next(
            ((pos, idx) for pos, (idx, c) in enumerate(content_lines)
             if _INLINE_CONCLUSION_RE.search(c)),
            (None, None))
        if marker_pos is not None:
            part_a, idx_a, skips = [], [], 0
            for idx, content in content_lines[marker_pos + 1:marker_pos + 13]:
                head = _strip_footnote(content)
                if len(head) < 4 or not re.search(r"[\u4e00-\u9fff]", head):
                    skips += 1
                    if skips > 4:
                        break
                    continue
                if _is_noise_line(head):
                    break
                part_a.append(head)
                idx_a.append(idx)
                if sum(len(x) for x in part_a) >= cap:
                    break
            part_b, idx_b = _body_paragraph_before_tail(marker_idx)
            ordered = part_a + part_b
            if ordered:
                merged = truncate_at_sentence_boundary(
                    _merge_wrapped_paragraphs("\n".join(ordered)), cap)
                if merged:
                    return merged, idx_a + idx_b
        for idx, content in reversed(content_lines):
            if _is_noise_line(content):
                continue
            head = _strip_footnote(content)
            if len(head) < 20 or not re.search(r"[\u4e00-\u9fff]", head):
                continue
            return truncate_at_sentence_boundary(head, cap), [idx]
        return "", []

    # 1) 背景：首选摘要块；无摘要标记时取首个含句末标点的正文跨段，
    #    跳过文首无标点的标题样式行（论文标题/作者行）
    if not result["research_background"]:
        filled, used = _abstract_block(content_lines, core_cap)
        if not filled:
            for pos, (_i, _c) in enumerate(body_lines):
                if _is_noise_line(body_lines[pos][1]):
                    continue
                if pos < 6 and _TITLE_LIKE_RE.match(body_lines[pos][1]):
                    continue
                filled, used = _take_span(pos, 4, core_cap, body_lines)
                if filled and len(filled) >= 20 and re.search(r"[。！？]", filled):
                    break
                filled, used = "", []
        if filled:
            result["research_background"] = filled
            _record("research_background", used)

    # 2) 核心观点：摘要之后、结论之前的中部内容，剔除噪声与标题样式行
    if not result["core_view"]:
        middle = body_lines[1:-1] if len(body_lines) > 2 else body_lines
        clean_middle = [
            (idx, c) for idx, c in middle
            if not _is_noise_line(c) and not _TITLE_LIKE_RE.match(c)
        ]
        if clean_middle:
            result["core_view"] = truncate_at_sentence_boundary(
                _merge_wrapped_paragraphs(
                    "\n".join(c for _i, c in clean_middle)),
                core_cap,
            )
            _record("core_view", [idx for idx, _c in clean_middle])

    # 3) 研究方法：正文中首个方法指示词命中的成段内容；编号标题行
    #    （如"三、…设计实践"）即使不足 20 字也从该标题起取材
    if not result["research_method"]:
        hit_pos = next(
            (pos for pos, (_i, content) in enumerate(body_lines)
             if not _is_noise_line(content) and _METHOD_HINT_RE.search(content)
             and (len(content) >= 20
                  or _HEADING_PREFIX_RE.match(content))),
            None)
        if hit_pos is not None:
            filled, used = _take_span(hit_pos, 4, core_cap, body_lines)
            if filled:
                result["research_method"] = filled
                _record("research_method", used)

    # 4) 创新点：正文中首个创新指示词命中的成段内容；
    #    指示词收紧后无命中即留空，不臆造、不误取标题
    if not result["innovation_point"]:
        hit_pos = next(
            (pos for pos, (_i, content) in enumerate(body_lines)
             if not _is_noise_line(content) and _INNOVATION_HINT_RE.search(content)
             and len(content) >= 20),
            None)
        if hit_pos is not None:
            filled, used = _take_span(hit_pos, 3, core_cap, body_lines)
            if filled:
                result["innovation_point"] = filled
                _record("innovation_point", used)

    # 5) 结论：行内粘连标题场景剥脚注并拼回双栏前后两段；
    #    常规场景反向跳过作者简介/基金项目/责编等噪声行
    if not result["research_conclusion"]:
        filled, used = _conclusion_fallback(core_cap)
        if filled:
            result["research_conclusion"] = filled
            _record("research_conclusion", used)
    return sources


def _is_symbol_garbage_like(content: str) -> bool:
    """行内兜底取材用的轻量乱码判定：不含任何中文且不含空格的短碎片。"""
    if " " in content:
        return False
    return not re.search(r"[\u4e00-\u9fff]", content) and len(content) < 6
