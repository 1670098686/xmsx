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
    result = {key: "" for key in _SECTION_ORDER}
    if not text or not text.strip():
        return result

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

    hits.sort(key=lambda item: item[0])
    for order, (line_idx, dimension) in enumerate(hits):
        end_line = hits[order + 1][0] if order + 1 < len(hits) else len(lines)
        section_lines = lines[line_idx + 1:end_line]
        content = "\n".join(section_lines).strip()
        result[dimension] = normalize_section_text(dimension, content)

    # 快速档（1-2）：仅保留标题精确命中的结构，不做猜测性补全
    if precision >= 3:
        core_cap = 1200 if precision >= 5 else (900 if precision == 4 else 600)
        _fill_fallback_sections(text, result, hits, core_cap)
    return result


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


def _fill_fallback_sections(text: str, result: dict, hits: list,
                            core_cap: int = 600) -> None:
    """对规则未命中的维度做保守兜底，保证常见文献也能产出成段语句。

    Args:
        text: 全文。
        result: 结构识别结果（原地补全）。
        hits: 已命中的章节标题列表。
        core_cap: 核心观点兜底内容的最大保留字数（随解析精度变化）。
    """
    paragraphs = [p["content"] for p in split_into_paragraphs(text)]
    if not paragraphs:
        return
    hit_dims = {dim for _, dim in hits}

    # 未识别到摘要/背景时，用首段（完整句子）兜底
    if not result["research_background"] and "reference_list" not in hit_dims:
        result["research_background"] = paragraphs[0]

    # 未识别到核心观点时，取摘要之后、结论之前的中部段落，
    # 先拼回完整语句再按句界截断（随解析精度调整保留长度）
    if not result["core_view"]:
        middle = paragraphs[1:-1] if len(paragraphs) > 2 else paragraphs
        merged = _merge_wrapped_paragraphs("\n".join(middle))
        result["core_view"] = truncate_at_sentence_boundary(merged, core_cap)

    # 未识别到结论时，用末段兜底（排除参考文献区标题）
    if not result["research_conclusion"]:
        tail = paragraphs[-1]
        if not re.match(_SECTION_PATTERNS["reference_list"], tail, flags=re.IGNORECASE):
            result["research_conclusion"] = tail
