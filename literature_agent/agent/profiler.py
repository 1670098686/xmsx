"""文献画像：在规划前用纯本地、零模型成本的方式判断文献类型与结构密度。

画像结果驱动自适应规划：
- short_paper 短文献：精简动作，关键词少提，循环步数收紧；
- survey 综述类：本地结构打底权重更高，AI 只在本地结果之上解读；
- experimental 实验类：章节通常密集，校验失败时允许更大的精度提升步长；
- standard 常规文献：标准动作序列。

判据全部基于字数、章节标题行密度、标题/开头关键词，不调用任何模型。
"""
import re

from config import constants as C

# 常见中文章节标题行：一、 / 1. / 1.2 / 第3章 / 第3节，以及常见英文章节名
_HEADING_PATTERN = re.compile(
    r"^\s*(?:"
    r"(?:第?\s*[0-9一二三四五六七八九十]+\s*[章节、.．])"  # 一、 / 第3章 / 3.
    r"|(?:[0-9]+(?:\.[0-9]+)*\s+\S)"                        # 1.2 方法
    r"|(?:摘要|引言|前言|绪论|背景|方法|实验|结论|讨论|参考文献|相关工作)"
    r"|(?:abstract|introduction|related work|method|experiment|conclusion|reference)"
    r")",
    re.IGNORECASE,
)

# 类型判定只看标题/开头，避免被正文中的普通用词干扰
_KIND_LOOKAHEAD_CHARS = 600


def profile_text(abs_text: str) -> dict:
    """生成文献画像。

    Args:
        abs_text: 文献全文。
    Returns:
        {"char_count","heading_count","dense_headings","kind","kind_label",
         "keyword_top_n"}
    """
    text = abs_text or ""
    char_count = len(text)
    heading_count = sum(
        1 for line in text.splitlines()
        if line.strip() and _HEADING_PATTERN.match(line)
    )
    kind, label = _classify_kind(text, char_count)
    return {
        "char_count": char_count,
        "heading_count": heading_count,
        "dense_headings": heading_count >= C.AGENT_HEADING_DENSE_COUNT,
        "kind": kind,
        "kind_label": label,
        "keyword_top_n": (
            C.AGENT_SHORT_KEYWORD_TOP_N if kind == C.LIT_KIND_SHORT
            else C.PARSE_KEYWORD_TOP_N
        ),
    }


def _classify_kind(text: str, char_count: int) -> tuple:
    """按优先级判定文献类型：短文献 > 综述 > 实验 > 常规。

    Args:
        text: 全文。
        char_count: 全文字数。
    Returns:
        (kind 常量, 中文标签)
    """
    if char_count < C.AGENT_SHORT_TEXT_CHARS:
        return C.LIT_KIND_SHORT, "短篇文献"
    head = text[:_KIND_LOOKAHEAD_CHARS].lower()
    if any(word.lower() in head for word in C.AGENT_SURVEY_KEYWORDS):
        return C.LIT_KIND_SURVEY, "综述类文献"
    if any(word.lower() in head for word in C.AGENT_EXPERIMENT_KEYWORDS):
        return C.LIT_KIND_EXPERIMENTAL, "实验类文献"
    return C.LIT_KIND_STANDARD, "常规文献"


def describe_profile(profile: dict) -> str:
    """把画像压成一句给 LLM 规划器看的中文描述。"""
    return (
        f"类型={profile.get('kind_label')}，"
        f"字数={profile.get('char_count')}，"
        f"章节标题数={profile.get('heading_count')}"
        + ("（章节密集）" if profile.get("dense_headings") else "")
    )
