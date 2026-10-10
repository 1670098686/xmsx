"""文献文件文本提取工具（零业务逻辑）。

- PDF：pdfplumber 优先，PyPDF2 兜底；
- TXT：多编码自动探测；
- DOCX：python-docx 提取段落与元数据；
- DOC（旧版 OLE2 二进制）：调用本机 Word/WPS（COM）或 LibreOffice 转换为
  临时 .docx 后提取，全程本地完成、无网络上传。

底层 IO/解析异常统一包装为 utils.exceptions 中的自定义异常向上抛出。
"""
import base64
import os
import re
import shutil
import subprocess
import tempfile
import time

from utils.exceptions import FileInvalidError, LiteratureFileNotFoundError, ParseError
from utils.logger import get_logger

logger = get_logger()

# CJK 统一表意文字与常见中文标点
_CJK_CHAR = "\u4e00-\u9fff\u3400-\u4dbf"
_CJK_PUNC = "，。；：、？！）】》〉」』…—·～"
_CJK_OPEN_PUNC = "（【《〈「『"
# 双栏版面中央栏间距的最小宽度（pt），小于该值视为单栏
_COLUMN_GUTTER_MIN_PT = 15.0
# 同一行词块的纵向聚类容差（pt）
_LINE_TOP_TOLERANCE_PT = 3.5
# 栏间距占据率判定：一个 x 位置至少被多少个词块覆盖才算"有正文"
_GUTTER_SUPPORT_MIN = 3
# PDF 水平切带数量（每页自上而下分别检测栏间距，兼容同页不同版面）
_PAGE_BAND_COUNT = 3
# pdfplumber 无法映射的字形占位，形如 (cid:12)
_UNMAPPED_GLYPH_RE = re.compile(r"\(cid:\d+\)")
# 独立成行的页码，如 "— 67 —"、"- 68 -"
_PAGE_NUMBER_RE = re.compile(r"^[\s—–\-－·.．]*\d{1,4}[\s—–\-－·.．]*$")
# 期刊"上接第 X 页"标记：其后为另一篇文章续文，应截断
_CONTINUED_FROM_RE = re.compile(r"[（(]\s*上接第\s*\d+\s*页\s*[）)]")
# 正文配对行密度判定参数：120pt 窗口内至少 4 对，且与下一配对行间距 <30pt
_BODY_PAIR_WINDOW_PT = 120.0
_BODY_PAIR_MIN_COUNT = 4
_BODY_PAIR_MAX_GAP_PT = 30.0
# 段落重建：相邻物理行 top 差超过中位行距的倍数即视为段前空白
_PARA_GAP_RATIO = 1.55
# 段落重建：段前空白还须至少比中位行距多出的 pt 数（抗行距抖动）
_PARA_GAP_EXTRA_PT = 4.0
# 段落重建：行首 x0 相对栏内正文基准缩进超过"中位行距*该系数"视为首行缩进
_PARA_INDENT_RATIO = 0.8
# 标题样式行的最大长度（超过则按普通正文行处理，避免误断）
_HEADING_LINE_MAX_LEN = 30
# 编辑/责编碎片（页脚，如"（责编：若佳）"）；双栏排版中常与正文末句
# 挤在同一物理行，故按碎片删除而非整行丢弃，末句正文得以保留
_EDITOR_FRAGMENT_RE = re.compile(r"\s*[（(]?\s*责\s*编\s*[:：][^）)]*[）)]?")
# 标题样式行：编号前缀（一、 / （二） / 1. / 2.3、 / 第X章）开头
_HEADING_LINE_RE = re.compile(
    r"^\s*(?:"
    r"[一二三四五六七八九十]+\s*[、.．]"
    r"|[（(]\s*[一二三四五六七八九十0-9]+\s*[）)]"
    r"|\d+(?:\.\d+)*\s*[、.．]"
    r"|第\s*[一二三四五六七八九十0-9]+\s*[章节]"
    r")"
)
# 页眉/页脚去重时排除的参考文献条目行
_REF_ITEM_PREFIX_RE = re.compile(r"^\s*\[\s*\d+\s*\]")
# 参与页眉/页脚去重的行长度上限
_RUNNING_HEADER_MAX_LEN = 22
# 单词字符（含中文）
_ALNUM_RE = re.compile(rf"[{_CJK_CHAR}A-Za-z0-9]")
# 判定符号乱码行的最小长度
_GARBAGE_MIN_LEN = 6


def _is_cjk_char(char: str) -> bool:
    """判断单个字符是否为 CJK 汉字。"""
    return bool(char) and "\u4e00" <= char <= "\u9fff"


def strip_cjk_spaces(text: str) -> str:
    """清理 PDF 抽取时被错误插入到中文文字之间的空格/制表符。

    仅删除两侧均为中文汉字或中文标点的空白，保留英文单词之间、
    中英文交界处的必要空格。

    Args:
        text: 原始抽取文本。
    Returns:
        清理后的文本。
    """
    if not text:
        return ""
    # 中文/中文标点 之间的空白（多轮处理以覆盖"字 字 字"连续情形）
    inner = re.compile(
        rf"([{_CJK_CHAR}{_CJK_PUNC}{_CJK_OPEN_PUNC}])[ \t\u00A0]+"
        rf"(?=[{_CJK_CHAR}{_CJK_PUNC}{_CJK_OPEN_PUNC}])"
    )
    previous = None
    while previous != text:
        previous = text
        text = inner.sub(r"\1", text)
    return text


def _is_symbol_garbage_line(line: str) -> bool:
    """判断是否为表格图片/符号字体产生的乱码行。

    三类特征：符号数超过文字数两倍；或整行无中文、无空格且较长
    （期刊内嵌表格图片的字形层通常是连续乱码字母串，正常英文
    参考文献行都含空格分词）；或无中文无空格的极短碎片行
    （表格字形层常碎成 "PQ"、"nfo"、")*+"、"!234/0" 这类
    2-6 字片段，正常正文不会以符号开头且无空格独立成行）。
    """
    stripped = line.strip()
    alnum_count = len(_ALNUM_RE.findall(stripped))
    symbol_count = len(stripped) - alnum_count
    if stripped and symbol_count > alnum_count * 2:
        return True
    has_cjk = any(_is_cjk_char(char) for char in stripped)
    if not has_cjk and " " not in stripped:
        if alnum_count >= _GARBAGE_MIN_LEN:
            return True
        # 极短字母/数字/符号碎片（2-5 字）或以符号开头的碎片
        if 2 <= len(stripped) < _GARBAGE_MIN_LEN:
            return True
        if stripped and not stripped[0].isalnum():
            return True
    return False


def clean_extracted_text(text: str) -> str:
    """清理 PDF 抽取噪声：未映射字形 (cid:n)、独立页码、责编/页眉行与乱码行。

    保留物理空行：段落重建产生的空行是溯源锚点分段的唯一依据，不得压缩；
    连续多个空行统一折叠为一个，页间分隔保持一个空行。

    Args:
        text: 已完成中文空格整理的抽取文本。
    Returns:
        清理后的文本。
    """
    if not text:
        return ""
    # 期刊"（上接第 X 页）"之后是他文续文，整体截断避免污染主文献
    continued = _CONTINUED_FROM_RE.search(text)
    if continued:
        text = text[:continued.start()]
    text = _UNMAPPED_GLYPH_RE.sub("", text)
    raw_lines = text.splitlines()
    # 页眉/页脚（期刊名、栏目名）在每页重复出现：短行出现 >=2 次即剔除；
    # 参考文献条目等可能合法重复的行不参与判定
    header_counts = {}
    for line in raw_lines:
        stripped = line.strip()
        if (0 < len(stripped) <= _RUNNING_HEADER_MAX_LEN
                and not _REF_ITEM_PREFIX_RE.match(stripped)):
            header_counts[stripped] = header_counts.get(stripped, 0) + 1
    running_headers = {line for line, count in header_counts.items() if count >= 2}

    kept = []
    for line in raw_lines:
        stripped = line.strip()
        if not stripped:
            # 保留空行但折叠连续空行（段落/页边界）
            if kept and kept[-1] != "":
                kept.append("")
            continue
        if _PAGE_NUMBER_RE.match(stripped):
            continue
        # 责编碎片常粘连在正文末句之后：只删碎片，保留同行的正文部分
        line = _EDITOR_FRAGMENT_RE.sub("", line)
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in running_headers:
            continue
        if _is_symbol_garbage_line(stripped):
            continue
        kept.append(line.rstrip())
    while kept and kept[-1] == "":
        kept.pop()
    return "\n".join(kept)


def find_column_gutter(words: list, page_width: float,
                       y_range: tuple = None,
                       support: int = _GUTTER_SUPPORT_MIN) -> tuple:
    """在页面中央带检测左右栏之间的垂直空白间隔（水平占据率法）。

    逐 pt 统计中央带内词块矩形对每个 x 位置的覆盖次数，只有覆盖次数
    达到 support 的位置才视为正文，从而过滤通栏标题、页码、旋转图形
    (cid) 字形等偶发跨栏噪声；空白带两侧都必须存在正文，避免把页面
    右侧留白误判为栏间距。

    Args:
        words: pdfplumber extract_words 得到的词块列表（含 x0/x1/top）。
        page_width: 页面宽度（pt）。
        y_range: 可选 (top_lo, top_hi)，仅统计该纵向范围内的词块；
            None 表示不限制纵向范围。
        support: x 位置被至少多少个词块覆盖才算有正文。
    Returns:
        (gutter_left, gutter_right)；判定为单栏时返回 None。
    """
    band_lo, band_hi = page_width * 0.30, page_width * 0.70
    coverage = {}
    for word in words:
        if y_range is not None:
            top = word.get("top", 0.0)
            if top < y_range[0] or top > y_range[1]:
                continue
        x0, x1 = word.get("x0", 0.0), word.get("x1", 0.0)
        x0, x1 = max(x0, band_lo), min(x1, band_hi)
        if x1 <= x0:
            continue
        for x in range(int(x0), min(int(x1) + 1, int(band_hi) + 1)):
            coverage[x] = coverage.get(x, 0) + 1
    if not coverage:
        return None

    start_x, end_x = int(band_lo), int(band_hi)
    occupied = {x for x in range(start_x, end_x + 1)
                if coverage.get(x, 0) >= support}
    if not occupied:
        return None

    # 扫描连续的"低占据"区间，挑两侧都有正文、宽度最大的一个
    best_gap = None
    x = start_x
    while x <= end_x:
        if x in occupied:
            x += 1
            continue
        gap_start = x
        while x <= end_x and x not in occupied:
            x += 1
        gap_end = x
        left_ok = any(px in occupied for px in range(start_x, gap_start))
        right_ok = any(px in occupied for px in range(gap_end, end_x + 1))
        width = gap_end - gap_start
        if left_ok and right_ok and width >= _COLUMN_GUTTER_MIN_PT:
            if best_gap is None or width > (best_gap[1] - best_gap[0]):
                best_gap = (float(gap_start), float(gap_end))
    return best_gap


def group_words_into_lines(words: list) -> list:
    """把词块按纵向位置聚成行，行内词块按 x0 从左到右排序。

    行 top 用行内均值更新，可吸收同一行文字的微小纵向抖动。

    Args:
        words: pdfplumber 词块列表。
    Returns:
        每行一个词块列表，整体按 top 升序。
    """
    ordered = sorted(words, key=lambda w: (w.get("top", 0), w.get("x0", 0)))
    lines = []
    current = []
    line_top = None
    for word in ordered:
        top = word.get("top", 0)
        if line_top is None or abs(top - line_top) <= _LINE_TOP_TOLERANCE_PT:
            current.append(word)
            if line_top is None:
                line_top = top
            else:
                line_top = (line_top * (len(current) - 1) + top) / len(current)
        else:
            lines.append(current)
            current = [word]
            line_top = top
    if current:
        lines.append(current)
    return [sorted(line, key=lambda w: w.get("x0", 0)) for line in lines]


def reconstruct_paragraphs(typed_lines: list) -> str:
    """把物理行序列按排版信号重建为"空行分隔段落"的文本。

    PDF 文本层只保留物理行，不保留段落结构；而 Agent 溯源校验按空行
    切分全局段落，若整篇被压成一段，任何锚点在数学上都无法通过覆盖
    校验。这里用三类排版信号恢复段落边界：
    - 段前空白：本行与上一行的纵向间距显著大于中位行距；
    - 首行缩进：本行首词 x0 明显大于栏内正文行左边距（中文正文段首
      缩进两字，约 21pt）；
    - 标题样式行：编号前缀的短独占行（如"（二）模块化设计"）。

    Args:
        typed_lines: [(top, x0, text), ...]，按阅读顺序排好的物理行。
    Returns:
        段内以单换行连接、段间以空行（\\n\\n）分隔的文本。
    """
    rows = [(float(top), float(x0), str(text).strip())
            for top, x0, text in typed_lines if str(text).strip()]
    if not rows:
        return ""
    gaps = [rows[i][0] - rows[i - 1][0]
            for i in range(1, len(rows)) if rows[i][0] > rows[i - 1][0]]
    median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
    x0_values = sorted(row[1] for row in rows)
    x0_base = x0_values[len(x0_values) // 2] if x0_values else 0.0
    gap_threshold = max(
        median_gap * _PARA_GAP_RATIO, median_gap + _PARA_GAP_EXTRA_PT)
    indent_threshold = max(median_gap * _PARA_INDENT_RATIO, 6.0)

    def _starts_paragraph(index: int) -> bool:
        top, x0, text = rows[index]
        if index == 0:
            return False
        prev_top = rows[index - 1][0]
        if top - prev_top >= gap_threshold:
            return True
        if x0 - x0_base >= indent_threshold:
            return True
        if (len(text) <= _HEADING_LINE_MAX_LEN
                and _HEADING_LINE_RE.match(text)
                and not re.search(r"[。！？!?]", text)):
            return True
        return False

    blocks = []
    current = [rows[0][2]]
    for index in range(1, len(rows)):
        if _starts_paragraph(index):
            blocks.append("\n".join(current))
            current = [rows[index][2]]
        else:
            current.append(rows[index][2])
    blocks.append("\n".join(current))
    return "\n\n".join(blocks)


def words_to_lines(words: list) -> str:
    """把词块按"先上后下、同行从左到右"拼成保留段落结构的文本。

    物理行之间以单换行连接；检测到段前空白/首行缩进/标题行时插入空行，
    输出的空行分段与人类视觉段落一致，供下游章节识别与溯源锚点使用。

    Args:
        words: pdfplumber 词块列表。
    Returns:
        拼合后的多段文本。
    """
    if not words:
        return ""
    typed_lines = []
    for line in group_words_into_lines(words):
        text = _join_line_words(line)
        if text:
            typed_lines.append((
                sum(w.get("top", 0.0) for w in line) / len(line),
                min(w.get("x0", 0.0) for w in line),
                text,
            ))
    return reconstruct_paragraphs(typed_lines)


def _join_line_words(line_words: list) -> str:
    """拼接同一行内的词块，中文之间不插空格。"""
    parts = []
    previous = None
    for word in sorted(line_words, key=lambda w: w.get("x0", 0)):
        text = (word.get("text") or "").strip()
        if not text:
            continue
        if previous is not None:
            gap = word.get("x0", 0) - previous.get("x1", 0)
            prev_char = (previous.get("text") or "")[-1:]
            next_char = text[:1]
            if _is_cjk_char(prev_char) and (
                _is_cjk_char(next_char) or next_char in _CJK_PUNC
            ):
                separator = ""
            elif gap <= 2.0:
                separator = ""
            else:
                separator = " "
            parts.append(separator)
        parts.append(text)
        previous = word
    return "".join(parts)


def _line_top(line_words: list) -> float:
    """取一行词块的平均 top。"""
    return sum(w.get("top", 0.0) for w in line_words) / max(len(line_words), 1)


def _partition_column_lines(words: list, divider: float) -> tuple:
    """按栏分隔线把词块拆成 (左栏行, 右栏行, 通栏行)。

    词块矩形跨越分隔线两侧（标题、页眉、页码、旋转图形字形等）归入
    通栏行；其余词块按中心位置归入左/右栏，并各自聚行。每个元素为
    (top, 文本)。
    """
    cross = _COLUMN_GUTTER_MIN_PT / 2
    left_words, right_words, span_words = [], [], []
    for word in words:
        x0, x1 = word.get("x0", 0.0), word.get("x1", 0.0)
        if x0 <= divider - cross and x1 >= divider + cross:
            span_words.append(word)
        elif (x0 + x1) / 2 < divider:
            left_words.append(word)
        else:
            right_words.append(word)

    def _to_lines(group):
        typed = []
        for line in group_words_into_lines(group):
            text = _join_line_words(line)
            if text:
                typed.append((_line_top(line),
                              min(w.get("x0", 0.0) for w in line), text))
        return typed

    return (
        _to_lines(left_words),
        _to_lines(right_words),
        _to_lines(span_words),
    )


def _merge_column_lines(left_lines: list, right_lines: list,
                        span_lines: list) -> str:
    """按纵向位置合并通栏行与左右两栏文本。

    通栏行（标题/页眉等）之上的左右栏内容先按左栏、右栏输出，再输出
    通栏行；末尾剩余内容整段输出左栏再输出右栏，保证双栏阅读顺序。
    """
    left_lines = sorted(left_lines, key=lambda item: item[0])
    right_lines = sorted(right_lines, key=lambda item: item[0])
    span_lines = sorted(span_lines, key=lambda item: item[0])
    parts = []
    li = ri = 0
    for ftop, _fx0, ftext in span_lines:
        boundary = ftop + _LINE_TOP_TOLERANCE_PT
        l_buf = []
        while li < len(left_lines) and left_lines[li][0] <= boundary:
            l_buf.append(left_lines[li])
            li += 1
        r_buf = []
        while ri < len(right_lines) and right_lines[ri][0] <= boundary:
            r_buf.append(right_lines[ri])
            ri += 1
        # 每栏内部按排版信号独立重建段落，再按左栏→右栏还原阅读顺序
        if l_buf:
            parts.append(reconstruct_paragraphs(l_buf))
        if r_buf:
            parts.append(reconstruct_paragraphs(r_buf))
        if ftext:
            parts.append(ftext)
    rest_left = [row for row in left_lines[li:] if row[2]]
    rest_right = [row for row in right_lines[ri:] if row[2]]
    if rest_left:
        parts.append(reconstruct_paragraphs(rest_left))
    if rest_right:
        parts.append(reconstruct_paragraphs(rest_right))
    return "\n\n".join(part for part in parts if part)


def _find_body_top(left_lines: list, right_lines: list):
    """找出双栏正文区起点的 top。

    页眉、通栏大标题、作者行偶尔也会在两侧同时出现词块，因此不能
    只取首个"同行配对行"：要求候选行之后 120pt 内至少还有 4 个
    配对行，且与下一个配对行间距不超过 30pt（正文行密集连续），
    从而排除孤立的标题/页眉配对。

    Returns:
        正文区起始 top；无法判定（如单栏内容）时返回 None。
    """
    r_tops = sorted(top for top, _x0, _text in right_lines)
    paired = []
    for top, _x0, _text in sorted(left_lines, key=lambda item: item[0]):
        if any(abs(top - other) <= _LINE_TOP_TOLERANCE_PT for other in r_tops):
            paired.append(top)
    for index, top in enumerate(paired):
        if index + 1 < len(paired) and \
                paired[index + 1] - top > _BODY_PAIR_MAX_GAP_PT:
            continue
        nearby = sum(
            1 for other in paired
            if top <= other <= top + _BODY_PAIR_WINDOW_PT
        )
        if nearby >= _BODY_PAIR_MIN_COUNT:
            return top
    return None


def _reconstruct_columns(words: list, divider: float) -> str:
    """双栏词块还原：页眉标题区单栏输出，正文按左栏→右栏输出。"""
    left, right, _span = _partition_column_lines(words, divider)
    body_top = _find_body_top(left, right)
    if body_top is not None:
        cutoff = body_top - _LINE_TOP_TOLERANCE_PT
        head_words = [w for w in words if w.get("top", 0.0) < cutoff]
        body_words = [w for w in words if w.get("top", 0.0) >= cutoff]
    else:
        head_words, body_words = [], words
    parts = []
    head_text = words_to_lines(head_words)
    if head_text:
        parts.append(head_text)
    left2, right2, span2 = _partition_column_lines(body_words, divider)
    body_text = _merge_column_lines(left2, right2, span2)
    if body_text:
        parts.append(body_text)
    # 页眉标题区与正文区是天然的段落边界，用空行分隔
    return "\n\n".join(parts)


def _extract_page_band_text(words: list, page_width: float,
                            y_range: tuple) -> str:
    """提取单个水平带内文本；带内检测到双栏则按左栏→右栏还原。"""
    band_words = [
        w for w in words
        if y_range[0] <= w.get("top", 0.0) <= y_range[1]
    ]
    if not band_words:
        return ""
    gutter = find_column_gutter(words, page_width, y_range=y_range)
    if not gutter:
        return words_to_lines(band_words)
    divider = (gutter[0] + gutter[1]) / 2
    left, right, _span = _partition_column_lines(band_words, divider)
    if not left or not right:
        return words_to_lines(band_words)
    return _reconstruct_columns(band_words, divider)


def _extract_pdf_page_text(page) -> str:
    """提取单页文本，双栏版面按左栏→右栏还原阅读顺序。

    先在 3 个水平带内分别检测栏间距：若各带分隔线一致，按整页统一
    分栏（保持整栏阅读连贯）；否则逐带独立还原，兼容同一页下半部
    切换版面（如期刊"上接"他文）的情况。
    """
    try:
        words = page.extract_words(
            x_tolerance=2, y_tolerance=3, keep_blank_chars=False,
        )
    except Exception:
        words = []
    if not words:
        return page.extract_text() or ""

    width = float(page.width)
    height = float(page.height or 0.0) or max(
        (w.get("bottom", 0.0) for w in words), default=0.0
    )
    if height <= 0:
        return words_to_lines(words)

    band_ranges = [
        (height * index / _PAGE_BAND_COUNT,
         height * (index + 1) / _PAGE_BAND_COUNT + (1.0 if index == _PAGE_BAND_COUNT - 1 else 0.0))
        for index in range(_PAGE_BAND_COUNT)
    ]
    gutters = [
        find_column_gutter(words, width, y_range=band_range)
        for band_range in band_ranges
    ]
    valid = [gutter for gutter in gutters if gutter]
    dividers = [(gutter[0] + gutter[1]) / 2 for gutter in valid]
    # 各带分隔线一致（至少 2 带检出且偏差不超过 10pt）：整页统一分栏
    if len(valid) >= 2 and max(dividers) - min(dividers) <= 10.0:
        divider = sorted(dividers)[len(dividers) // 2]
        left, right, _span = _partition_column_lines(words, divider)
        if left and right:
            return _reconstruct_columns(words, divider)
        return page.extract_text() or ""

    # 版面不一致：逐带还原，带间空行分隔
    band_texts = [
        _extract_page_band_text(words, width, band_range)
        for band_range in band_ranges
    ]
    merged = "\n\n".join(text for text in band_texts if text)
    return merged or (page.extract_text() or "")


def _check_readable(file_path: str) -> None:
    """文件存在性与可读性前置校验。

    Args:
        file_path: 文件绝对路径。
    Raises:
        LiteratureFileNotFoundError: 文件不存在或不是普通文件。
        PermissionDeniedError: 无读取权限。
    """
    from utils.exceptions import PermissionDeniedError

    if not file_path or not os.path.exists(file_path):
        raise LiteratureFileNotFoundError(f"文件不存在：{file_path}")
    if not os.path.isfile(file_path):
        raise FileInvalidError(f"不是有效文件：{file_path}")
    if not os.access(file_path, os.R_OK):
        raise PermissionDeniedError(f"无读取权限：{file_path}")


def extract_pdf_text(file_path: str) -> tuple:
    """提取 PDF 全文与元数据。

    Args:
        file_path: PDF 文件绝对路径。
    Returns:
        (text, metadata)：metadata 含 title/author/page_count。
    Raises:
        FileInvalidError: 文件损坏或加密无文本层。
        ParseError: 两个引擎均提取失败。
    """
    _check_readable(file_path)

    # ---- 引擎 1：pdfplumber ----
    try:
        import pdfplumber

        pages_text = []
        meta = {"title": "", "author": "", "page_count": 0}
        with pdfplumber.open(file_path) as pdf:
            meta["page_count"] = len(pdf.pages)
            raw_meta = pdf.metadata or {}
            meta["title"] = (raw_meta.get("Title") or "").strip()
            meta["author"] = (raw_meta.get("Author") or "").strip()
            for page in pdf.pages:
                # 双栏版面按左栏→右栏还原阅读顺序，避免跨栏串行
                pages_text.append(_extract_pdf_page_text(page))
        text = clean_extracted_text(
            strip_cjk_spaces("\n\n".join(pages_text))
        ).strip()
        if text:
            return text, meta
        logger.warning("pdfplumber 未提取到文本，尝试 PyPDF2：%s", file_path)
    except Exception as exc:  # pdfplumber 自身异常不致命，走兜底引擎
        logger.warning("pdfplumber 解析失败，尝试 PyPDF2：%s | %s", file_path, exc)

    # ---- 引擎 2：PyPDF2 兜底 ----
    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(file_path)
        if getattr(reader, "is_encrypted", False):
            raise FileInvalidError("PDF 已加密，请解密后再导入")
        pages_text = []
        for page in reader.pages:
            pages_text.append(page.extract_text() or "")
        info = reader.metadata or {}
        meta = {
            "title": (getattr(info, "title", "") or "").strip() if info else "",
            "author": (getattr(info, "author", "") or "").strip() if info else "",
            "page_count": len(reader.pages),
        }
        text = clean_extracted_text(
            strip_cjk_spaces("\n\n".join(pages_text))
        ).strip()
        if not text:
            raise FileInvalidError("PDF 无文本层（可能是纯扫描件），无法提取文字")
        return text, meta
    except FileInvalidError:
        raise
    except Exception as exc:
        raise ParseError(f"PDF 解析失败：{exc}") from exc


_TXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin-1")


def extract_txt_text(file_path: str) -> tuple:
    """提取 TXT 全文，自动探测中文编码。

    Args:
        file_path: TXT 文件绝对路径。
    Returns:
        (text, metadata)：TXT 无文献元数据，返回空字段字典。
    Raises:
        FileInvalidError: 空文件或全部编码均无法解码。
    """
    _check_readable(file_path)
    last_error = None
    with open(file_path, "rb") as fp:
        raw = fp.read()
    if not raw.strip():
        raise FileInvalidError("文本文件内容为空")
    for encoding in _TXT_ENCODINGS:
        try:
            # 统一换行符：Windows 记事本等保存的 CRLF/CR 文献若原样保留，
            # Agent 溯源校验按空行分段时会把整篇误判为一段，导致锚点全部越界
            text = raw.decode(encoding).replace("\r\n", "\n").replace("\r", "\n")
            text = text.strip()
            if text:
                meta = {
                    "title": os.path.splitext(os.path.basename(file_path))[0],
                    "author": "",
                    "page_count": 0,
                    "encoding": encoding,
                }
                return text, meta
        except UnicodeDecodeError as exc:
            last_error = exc
    raise FileInvalidError(f"文本编码无法识别：{last_error}")


def extract_docx_text(file_path: str) -> tuple:
    """提取 DOCX 全文与元数据。

    Args:
        file_path: DOCX 文件绝对路径（旧版二进制 .doc 不支持）。
    Returns:
        (text, metadata)：metadata 含 title/author/paragraph_count。
    Raises:
        FileInvalidError: 旧版 .doc 或文档损坏。
    """
    _check_readable(file_path)
    try:
        import docx
    except ImportError as exc:
        raise ParseError("缺少 python-docx 依赖，无法解析 Word 文档") from exc

    try:
        document = docx.Document(file_path)
    except Exception as exc:
        # python-docx 只能读取 OOXML(.docx)，旧版 .doc 会在此失败
        if file_path.lower().endswith(".doc"):
            raise FileInvalidError(
                "旧版 .doc 不能按 DOCX 解析，请通过 auto_extract/extract_doc_text 转换后导入"
            ) from exc
        raise FileInvalidError(f"Word 文档损坏或无法打开：{exc}") from exc

    paragraphs = [p.text.strip() for p in document.paragraphs if p.text.strip()]
    text = "\n".join(paragraphs).strip()
    if not text:
        raise FileInvalidError("Word 文档内容为空")

    core = getattr(document, "core_properties", None)
    meta = {
        "title": "",
        "author": "",
        "paragraph_count": len(paragraphs),
    }
    if core is not None:
        meta["title"] = (getattr(core, "title", "") or "").strip()
        meta["author"] = (getattr(core, "author", "") or "").strip()
    return text, meta


# ================= 表格结构化提取（PDF / DOCX → Markdown） =================
# 常规正文提取（extract_words/extract_text）会把表格单元格文字按物理位置
# 拆散或串列，丢失行列关系。这里单独用各库的表格识别能力把表格还原为
# Markdown 行列文本，供 AI 在文本通道综合时读到准确的表格数据。

def _table_rows_to_markdown(rows: list) -> str:
    """把二维单元格数组转换为 GitHub 风格 Markdown 表格。

    Args:
        rows: 表格行的二维列表（每个元素为一行的单元格字符串）。
    Returns:
        Markdown 表格文本；无任何有效单元格时返回空串。
    """
    cleaned: list[list[str]] = []
    for row in rows or []:
        cells = [
            str(cell or "").replace("\n", " ").replace("|", "\\|").strip()
            for cell in row
        ]
        if any(cells):  # 整行全空的单元格行丢弃
            cleaned.append(cells)
    if not cleaned:
        return ""
    col_count = max(len(row) for row in cleaned)
    normalized = [row + [""] * (col_count - len(row)) for row in cleaned]
    lines = [
        "| " + " | ".join(normalized[0]) + " |",
        "| " + " | ".join(["---"] * col_count) + " |",
    ]
    for row in normalized[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _extract_pdf_tables(file_path: str, page_index: int,
                        max_tables: int) -> tuple:
    """用 pdfplumber 识别并提取 PDF 中的表格为 Markdown。

    Args:
        file_path: PDF 文件绝对路径。
        page_index: 指定页码索引（从 0 开始）；负数表示提取全部页。
        max_tables: 返回表格数量上限。
    Returns:
        (tables, table_pages)：tables 为
        [{"page":int,"index":int,"markdown":str},...]，
        table_pages 为含表格的页码索引列表。
    """
    import pdfplumber

    tables: list[dict] = []
    table_pages: list[int] = []
    with pdfplumber.open(file_path) as pdf:
        total = len(pdf.pages)
        if int(page_index) >= 0:
            indices = [int(page_index)] if 0 <= int(page_index) < total else []
        else:
            indices = range(total)
        for pidx in indices:
            page = pdf.pages[pidx]
            try:
                found = page.find_tables()
            except Exception:  # 单页表格识别失败不影响其他页
                found = []
            page_has_table = False
            for table_index, table in enumerate(found):
                try:
                    rows = table.extract()
                except Exception:
                    continue
                markdown = _table_rows_to_markdown(rows)
                if not markdown:
                    continue
                tables.append({
                    "page": int(pidx), "index": int(table_index),
                    "markdown": markdown,
                })
                page_has_table = True
                if len(tables) >= max_tables:
                    break
            if page_has_table:
                table_pages.append(int(pidx))
            if len(tables) >= max_tables:
                break
    return tables, table_pages


def _extract_docx_tables(file_path: str, max_tables: int) -> list:
    """用 python-docx 提取 Word 文档中的全部表格为 Markdown。

    Args:
        file_path: DOCX 文件绝对路径。
        max_tables: 返回表格数量上限。
    Returns:
        [{"page":"docx","index":int,"markdown":str},...]
    """
    import docx

    document = docx.Document(file_path)
    tables: list[dict] = []
    for table_index, table in enumerate(document.tables):
        if len(tables) >= max_tables:
            break
        rows = [
            [cell.text for cell in row.cells]
            for row in getattr(table, "rows", [])
        ]
        markdown = _table_rows_to_markdown(rows)
        if markdown:
            tables.append({
                "page": "docx", "index": int(table_index),
                "markdown": markdown,
            })
    return tables


def extract_tables_markdown(file_path: str, page_index: int = -1,
                            max_tables: int = 20) -> dict:
    """按文件类型提取文献中的表格，统一转成 Markdown 文本。

    Args:
        file_path: 文献原件绝对路径（PDF/DOCX/TXT/DOC）。
        page_index: PDF 指定页码索引（从 0 开始）；负数表示全部页；
            对 DOCX/TXT 无效。
        max_tables: 返回表格数量上限。
    Returns:
        {"tables":[{"page":int|"docx","index":int,"markdown":str}],
         "table_pages":[int]}；TXT/旧版 DOC 无表格返回空结构。
    Raises:
        FileInvalidError: 文件不存在/不可读。
        ParseError: PDF/DOCX 表格提取依赖缺失或解析失败。
    """
    _check_readable(file_path)
    limit = max(1, int(max_tables))
    suffix = os.path.splitext(file_path)[1].lower()
    if suffix == ".pdf":
        tables, table_pages = _extract_pdf_tables(
            file_path, int(page_index), limit)
        return {"tables": tables, "table_pages": table_pages}
    if suffix == ".docx":
        return {"tables": _extract_docx_tables(file_path, limit),
                "table_pages": []}
    # TXT 无表格结构；旧版 .doc 需先转 .docx（文本提取链路另有转换），
    # 表格直抽不支持，返回空结构而非报错
    return {"tables": [], "table_pages": []}


def _ps_literal(value: str) -> str:
    """把路径转成 PowerShell 单引号字符串字面量（单引号双写转义）。

    Args:
        value: 原始字符串（文件绝对路径）。
    Returns:
        str: 可直接嵌入 PowerShell 脚本的字面量。
    """
    return "'" + value.replace("'", "''") + "'"


# PowerShell 转换脚本：创建 Office COM 实例，只读打开文档，按指定格式另存。
# 占位符：prog=ProgID，src/dst=单引号路径字面量，fmt=SaveAs 格式枚举。
_PS_CONVERT_TEMPLATE = """
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$src = {src}
$dst = {dst}
$app = $null
$doc = $null
try {{
    $app = New-Object -ComObject '{prog}'
    try {{ $app.Visible = $false }} catch {{}}
    try {{ $app.DisplayAlerts = 0 }} catch {{}}
    $doc = $app.Documents.Open($src, $false, $true)
    $doc.SaveAs([ref]$dst, {fmt})
}} finally {{
    if ($doc) {{ try {{ $doc.Close($false) }} catch {{}} }}
    if ($app) {{ try {{ $app.Quit() }} catch {{}} }}
}}
exit 0
"""


def _run_office_com_convert(src_path: str, dst_path: str, prog_id: str,
                            word_format: int) -> subprocess.CompletedProcess:
    """执行 PowerShell Office COM 另存脚本。

    Args:
        src_path: 源文档绝对路径。
        dst_path: 目标文件绝对路径。
        prog_id: Office COM ProgID。
        word_format: Word SaveAs 格式枚举（如 16=docx、17=pdf）。
    Returns:
        subprocess.CompletedProcess：子进程结果（不检查返回码，由调用方按产物判定）。
    Raises:
        FileInvalidError: PowerShell 无法启动或转换超时。
    """
    from config import constants as C

    script = _PS_CONVERT_TEMPLATE.format(
        prog=prog_id,
        src=_ps_literal(os.path.abspath(src_path)),
        dst=_ps_literal(os.path.abspath(dst_path)),
        fmt=word_format,
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        return subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-EncodedCommand",
                encoded,
            ],
            capture_output=True,
            timeout=C.DOC_CONVERT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise FileInvalidError(
            f"{prog_id} 转换超时（超过 {C.DOC_CONVERT_TIMEOUT_SEC} 秒）"
        ) from exc
    except OSError as exc:
        raise FileInvalidError(f"{prog_id} 无法启动（{exc}）") from exc


def _convert_via_office_com(src_path: str, out_dir: str, prog_id: str,
                            target_ext: str, word_format: int,
                            log_label: str) -> str:
    """用本机 Office（Word/WPS）COM 把文档另存为指定格式。

    Args:
        src_path: 源文档绝对路径。
        out_dir: 转换输出目录。
        prog_id: Office COM ProgID（如 Word.Application、KWps.Application）。
        target_ext: 目标后缀（如 ".docx"、".pdf"）。
        word_format: Word SaveAs 格式枚举。
        log_label: 日志中使用的格式名称。
    Returns:
        str: 生成文件的绝对路径。
    Raises:
        FileInvalidError: COM 不可用、打开失败、转换超时或未生成文件。
    """
    dst_name = os.path.splitext(os.path.basename(src_path))[0] + target_ext
    dst_path = os.path.join(out_dir, dst_name)
    proc = _run_office_com_convert(src_path, dst_path, prog_id, word_format)
    if os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
        logger.info("COM(%s) 转换%s成功：%s", prog_id, log_label, dst_name)
        return dst_path
    detail = ""
    if proc.stderr:
        detail = proc.stderr.decode("gbk", errors="replace").strip()
        detail = ("：" + detail[-200:]) if detail else ""
    raise FileInvalidError(f"{prog_id} 无法打开或转换该文档{detail}")


def _convert_doc_via_com(src_path: str, out_dir: str, prog_id: str) -> str:
    """用本机 Office（Word/WPS）COM 自动化把 .doc 另存为 .docx。

    Args:
        src_path: 旧版 .doc 文件绝对路径。
        out_dir: 转换输出目录（调用方负责清理）。
        prog_id: Office COM ProgID（如 Word.Application、KWps.Application）。
    Returns:
        str: 生成的 .docx 绝对路径。
    Raises:
        FileInvalidError: COM 不可用、打开失败、转换超时或未生成文件。
    """
    from config import constants as C

    return _convert_via_office_com(
        src_path, out_dir, prog_id,
        target_ext=".docx", word_format=C.DOC_WORD_FORMAT_DOCX, log_label="DOCX",
    )


def _find_soffice() -> str:
    """查找本机 LibreOffice/OpenOffice 可执行文件。

    Returns:
        str: soffice 绝对路径；未找到返回空字符串。
    """
    from config import constants as C

    for name in C.DOC_SOFFICE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    for candidate in C.DOC_SOFFICE_FALLBACK_PATHS:
        if os.path.isfile(candidate):
            return candidate
    return ""


def _convert_via_soffice(src_path: str, out_dir: str, soffice_bin: str,
                         target_ext: str, soffice_format: str) -> str:
    """用 LibreOffice headless 把文档转换为指定格式。

    Args:
        src_path: 源文档绝对路径。
        out_dir: 转换输出目录。
        soffice_bin: soffice 可执行文件绝对路径。
        target_ext: 目标后缀（如 ".docx"、".pdf"）。
        soffice_format: --convert-to 的格式名（如 docx、pdf）。
    Returns:
        str: 生成文件的绝对路径。
    Raises:
        FileInvalidError: 转换超时、进程失败或未生成文件。
    """
    from config import constants as C

    dst_name = os.path.splitext(os.path.basename(src_path))[0] + target_ext
    dst_path = os.path.join(out_dir, dst_name)
    try:
        proc = subprocess.run(
            [
                soffice_bin,
                "--headless",
                "--invisible",
                "--nodefault",
                "--nolockcheck",
                "--convert-to",
                soffice_format,
                "--outdir",
                out_dir,
                src_path,
            ],
            capture_output=True,
            timeout=C.DOC_CONVERT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as exc:
        raise FileInvalidError(
            f"LibreOffice 转换超时（超过 {C.DOC_CONVERT_TIMEOUT_SEC} 秒）"
        ) from exc
    except OSError as exc:
        raise FileInvalidError(f"LibreOffice 无法启动（{exc}）") from exc

    if os.path.isfile(dst_path) and os.path.getsize(dst_path) > 0:
        logger.info("LibreOffice 转换%s成功：%s", target_ext.upper(), dst_name)
        return dst_path
    detail = ""
    if proc.stderr:
        detail = proc.stderr.decode("gbk", errors="replace").strip()
        detail = ("：" + detail[-200:]) if detail else ""
    raise FileInvalidError(f"LibreOffice 无法打开或转换该文档{detail}")


def _convert_doc_via_soffice(src_path: str, out_dir: str, soffice_bin: str) -> str:
    """用 LibreOffice headless 把 .doc 转换为 .docx。

    Args:
        src_path: 旧版 .doc 文件绝对路径。
        out_dir: 转换输出目录（调用方负责清理）。
        soffice_bin: soffice 可执行文件绝对路径。
    Returns:
        str: 生成的 .docx 绝对路径。
    Raises:
        FileInvalidError: 转换超时、进程失败或未生成文件。
    """
    return _convert_via_soffice(src_path, out_dir, soffice_bin, ".docx", "docx")


def _convert_doc_to_docx(src_path: str, out_dir: str) -> str:
    """依次尝试 Word/WPS COM、LibreOffice 后端把 .doc 转为 .docx。

    Args:
        src_path: 旧版 .doc 文件绝对路径。
        out_dir: 转换输出目录（调用方负责清理）。
    Returns:
        str: 生成的 .docx 绝对路径。
    Raises:
        FileInvalidError: 所有后端均不可用或转换失败。
    """
    from config import constants as C

    errors = []
    for prog_id in C.DOC_COM_PROGIDS:
        try:
            return _convert_doc_via_com(src_path, out_dir, prog_id)
        except FileInvalidError as exc:
            logger.warning("DOC 转换后端 %s 失败：%s", prog_id, exc.message)
            errors.append(exc.message)

    soffice_bin = _find_soffice()
    if soffice_bin:
        try:
            return _convert_doc_via_soffice(src_path, out_dir, soffice_bin)
        except FileInvalidError as exc:
            logger.warning("DOC 转换后端 LibreOffice 失败：%s", exc.message)
            errors.append(exc.message)
    else:
        errors.append("未检测到 LibreOffice")

    raise FileInvalidError(
        "旧版 .doc 文档转换失败：本机未检测到可用的 Microsoft Word/WPS/LibreOffice，"
        "或文档已损坏、被加密。请安装上述任一软件，或将文件另存为 .docx 后导入"
        + ("（后端详情：" + "；".join(errors) + "）" if errors else "")
    )


def convert_document_to_pdf(src_path: str, out_dir: str) -> str:
    """把 Word 文档（.doc/.docx）转换为 PDF，用于原版式渲染与批注。

    依次尝试 Word/WPS COM、LibreOffice 后端；全程本地处理、无网络上传。

    Args:
        src_path: Word 文档绝对路径。
        out_dir: PDF 输出目录（由调用方管理，可做缓存复用）。
    Returns:
        str: 生成的 PDF 绝对路径。
    Raises:
        FileInvalidError: 后缀不支持、所有后端不可用或转换失败。
    """
    from config import constants as C

    _check_readable(src_path)
    suffix = os.path.splitext(src_path)[1].lower()
    if suffix not in C.WORD_RENDER_SUFFIX:
        raise FileInvalidError(f"仅支持 .doc/.docx 转 PDF，收到：{suffix}")
    os.makedirs(out_dir, exist_ok=True)

    errors = []
    for prog_id in C.DOC_COM_PROGIDS:
        try:
            return _convert_via_office_com(
                src_path, out_dir, prog_id,
                target_ext=".pdf", word_format=C.DOC_WORD_FORMAT_PDF, log_label="PDF",
            )
        except FileInvalidError as exc:
            logger.warning("PDF 转换后端 %s 失败：%s", prog_id, exc.message)
            errors.append(exc.message)

    soffice_bin = _find_soffice()
    if soffice_bin:
        try:
            return _convert_via_soffice(
                src_path, out_dir, soffice_bin, ".pdf", "pdf")
        except FileInvalidError as exc:
            logger.warning("PDF 转换后端 LibreOffice 失败：%s", exc.message)
            errors.append(exc.message)
    else:
        errors.append("未检测到 LibreOffice")

    raise FileInvalidError(
        "Word 文档转 PDF 失败：本机未检测到可用的 Microsoft Word/WPS/LibreOffice，"
        "或文档已损坏、被加密。请安装上述任一软件以查看原版式"
        + ("（后端详情：" + "；".join(errors) + "）" if errors else "")
    )


def extract_doc_text(file_path: str) -> tuple:
    """提取旧版 .doc 全文与元数据。

    处理流程：
    1. 读取文件头魔数：ZIP 头说明实为改名的 OOXML，直接按 DOCX 提取；
    2. 其余情况（OLE2/RTF 等）调用本机 Office 转换为临时 .docx；
    3. 复用 extract_docx_text 提取，临时文件用后即删，全程本地处理。

    Args:
        file_path: .doc 文件绝对路径。
    Returns:
        (text, metadata)：metadata 含 title/author/paragraph_count。
    Raises:
        FileInvalidError: 文件过小、无可用转换后端或转换失败。
    """
    from config import constants as C

    _check_readable(file_path)
    with open(file_path, "rb") as fp:
        magic = fp.read(len(C.ZIP_MAGIC))
    if len(magic) < len(C.ZIP_MAGIC):
        raise FileInvalidError("Word 文档内容为空或已损坏")
    if magic == C.ZIP_MAGIC:
        logger.info(".doc 文件实际为 OOXML 包，直接按 DOCX 解析：%s", file_path)
        return extract_docx_text(file_path)

    tmp_dir = tempfile.mkdtemp(prefix="la_doc_convert_")
    try:
        docx_path = _convert_doc_to_docx(file_path, tmp_dir)
        return extract_docx_text(docx_path)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def auto_extract(file_path: str) -> tuple:
    """按后缀自动分发到对应提取器。

    Args:
        file_path: 文献文件绝对路径。
    Returns:
        (text, metadata, file_type) 三元组；file_type 为 PDF/TXT/DOCX/DOC（按后缀区分）。
    Raises:
        FileInvalidError: 后缀不支持。
    """
    from config import constants as C

    suffix = os.path.splitext(file_path)[1].lower()
    extractors = {
        ".pdf": (extract_pdf_text, C.LIT_TYPE_PDF),
        ".txt": (extract_txt_text, C.LIT_TYPE_TXT),
        ".docx": (extract_docx_text, C.LIT_TYPE_DOCX),
        ".doc": (extract_doc_text, C.LIT_TYPE_DOC),
    }
    if suffix not in extractors:
        raise FileInvalidError(
            f"不支持的文件格式：{suffix}（仅支持 PDF/TXT/DOCX/DOC）")
    extractor, file_type = extractors[suffix]
    started = time.monotonic()
    text, meta = extractor(file_path)
    logger.debug("全文提取完成：%s → %s，%s 字，耗时 %.2f 秒",
                 os.path.basename(file_path), file_type,
                 len(text or ""), time.monotonic() - started)
    return text, meta, file_type
