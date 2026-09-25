"""文本分析工具：jieba 分词、关键词提取、段落切分、文献结构识别。

纯函数工具模块，不访问数据库，不包含业务判断。
"""
import re

from utils.logger import get_logger

logger = get_logger()

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
    """TF-IDF 关键词提取。

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


def detect_structure(text: str) -> dict:
    """识别文献章节结构。

    Args:
        text: 文献全文。
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
        result[dimension] = content

    _fill_fallback_sections(text, result, hits)
    return result


def _fill_fallback_sections(text: str, result: dict, hits: list) -> None:
    """对规则未命中的维度做保守兜底，保证常见文献也能产出结构化内容。

    Args:
        text: 全文。
        result: 结构识别结果（原地补全）。
        hits: 已命中的章节标题列表。
    """
    paragraphs = [p["content"] for p in split_into_paragraphs(text)]
    if not paragraphs:
        return
    hit_dims = {dim for _, dim in hits}

    # 未识别到摘要/背景时，用首段兜底
    if not result["research_background"] and "reference_list" not in hit_dims:
        result["research_background"] = paragraphs[0]

    # 未识别到核心观点时，取摘要之后、结论之前的中部段落拼接到 600 字
    if not result["core_view"]:
        middle = paragraphs[1:-1] if len(paragraphs) > 2 else paragraphs
        result["core_view"] = "\n".join(middle)[:600]

    # 未识别到结论时，用末段兜底（排除参考文献区）
    if not result["research_conclusion"]:
        tail = paragraphs[-1]
        if not re.match(_SECTION_PATTERNS["reference_list"], tail, flags=re.IGNORECASE):
            result["research_conclusion"] = tail
