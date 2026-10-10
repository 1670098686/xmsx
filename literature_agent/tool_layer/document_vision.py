"""原件视觉感知工具：PDF 布局探测、页面渲染与本地 OCR（阶段2）。

纯工具层模块，不访问数据库、不含业务判断：
- inspect_document_layout：抽样前 N 页用词块坐标判断单栏/双栏/扫描/图表页，
  只做坐标统计不做整页 OCR，单篇毫秒级；
- render_page_png：把指定页按 DPI 渲染为 PNG，供本地 Tesseract 或 AI 视觉识别；
- ocr_image_local：Tesseract 本地 OCR（依赖可选，缺失时返回明确错误，
  由上层降级为 AI 视觉或直接提示用户）。

PyMuPDF 随项目 .vendor 目录分发；非 PDF（TXT/DOCX/DOC）不做视觉探测，
直接返回"普通单栏文本"布局，由上层走标准文本提取链路。
"""
import io
import os
import sys

from config import constants as C
from utils.exceptions import FileInvalidError, LiteratureAgentError
from utils.logger import get_logger

logger = get_logger()

# 补项目内置 vendor 目录（PyMuPDF 免全局安装，与 ui/widgets/pdf_viewer.py 同策略）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_VENDOR_DIR = os.path.join(_PROJECT_ROOT, ".vendor")
if os.path.isdir(_VENDOR_DIR) and _VENDOR_DIR not in sys.path:
    sys.path.insert(0, _VENDOR_DIR)

try:
    import pymupdf  # PyMuPDF
    _PYMUPDF_AVAILABLE = True
except ImportError:  # pragma: no cover - 依赖缺失时由上层提示降级
    pymupdf = None
    _PYMUPDF_AVAILABLE = False

# 非 PDF 格式无需视觉探测，统一视为普通文本原件
_NON_PDF_FORMATS = {".txt": "txt", ".docx": "docx", ".doc": "doc"}
_PREVIEW_CHARS = 200

# 本地 OCR 可用性检测结果缓存（get_tesseract_version 会拉起子系统进程，
# 多页 OCR 时不应每页重复探测）
_local_ocr_cache: dict = {"checked": False, "available": False}


def is_pymupdf_available() -> bool:
    """PyMuPDF 是否可导入（布局探测与页面渲染的前置依赖）。"""
    return _PYMUPDF_AVAILABLE


def is_local_ocr_available() -> bool:
    """本地 Tesseract OCR 是否可用（pytesseract 包 + 系统 tesseract 程序齐备）。

    Returns:
        True 表示可做本地 OCR；结果在进程内缓存，避免重复拉起探测进程。
    """
    if _local_ocr_cache["checked"]:
        return _local_ocr_cache["available"]
    _local_ocr_cache["checked"] = True
    try:
        import pytesseract  # noqa: F401
        from PIL import Image  # noqa: F401
        pytesseract.get_tesseract_version()
    except Exception as exc:  # 包缺失/系统未装 tesseract/语言包问题都视为不可用
        logger.info("本地 OCR 不可用，扫描件将尝试 AI 视觉通道：%s", exc)
        _local_ocr_cache["available"] = False
        return False
    _local_ocr_cache["available"] = True
    return True


def inspect_document_layout(file_path: str,
                            sample_pages: int = C.AGENT_INSPECT_SAMPLE_PAGES) -> dict:
    """抽样检查原件布局，返回页面级布局摘要（不做整页 OCR）。

    Args:
        file_path: 文献原件绝对路径。
        sample_pages: 抽样页数上限（取前 N 页）。
    Returns:
        {"page_count","format","is_scanned","has_two_columns",
         "column_gutter_estimate","pages_with_images","page_samples"}；
        非 PDF 文件返回固定的普通文本布局（page_count=0、不抽样）。
    Raises:
        FileInvalidError: 文件不存在/后缀不支持/PDF 加密无法打开。
        LiteratureAgentError: PyMuPDF 不可用或 PDF 解析失败。
    """
    if not os.path.isfile(file_path):
        raise FileInvalidError("文献原件不存在，无法检查布局")
    suffix = os.path.splitext(file_path)[1].lower()
    if suffix in _NON_PDF_FORMATS:
        return {
            "page_count": 0,
            "format": _NON_PDF_FORMATS[suffix],
            "is_scanned": False,
            "has_two_columns": False,
            "column_gutter_estimate": 0.0,
            "pages_with_images": [],
            "table_pages": [],
            "page_samples": [],
        }
    if suffix != ".pdf":
        raise FileInvalidError(f"不支持的文件格式：{suffix}")
    if not _PYMUPDF_AVAILABLE:
        raise LiteratureAgentError(
            "缺少 PDF 布局检查组件 PyMuPDF，无法感知原件版式")

    try:
        doc = pymupdf.open(file_path)
    except Exception as exc:
        raise LiteratureAgentError(f"PDF 原件无法打开：{exc}") from exc

    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise FileInvalidError("PDF 已加密，无法检查版式与解析内容")
        page_count = doc.page_count
        sample_n = max(1, min(int(sample_pages), page_count)) if page_count else 0

        samples: list[dict] = []
        image_only_pages: list[int] = []
        figure_pages: list[int] = []
        table_pages: list[int] = []
        double_gutters: list[float] = []
        for index in range(sample_n):
            page = doc[index]
            words = page.get_text("words") or []
            text = (page.get_text("text") or "").strip()
            image_ratio = _image_area_ratio(page)
            has_text_layer = len(text) >= C.AGENT_LAYOUT_PAGE_TEXT_MIN_CHARS
            is_double, gutter = (
                _detect_double_column(words, page.rect.width)
                if has_text_layer else (False, 0.0)
            )
            has_large_image = image_ratio >= C.AGENT_LAYOUT_FIGURE_AREA_RATIO
            # 表格探测（毫秒级、仅抽样页）：find_tables 失败/不可用按无表格处理，
            # 仅给 Director 提示"这些页可能需要结构化抽表"，不做真正提取
            has_table = False
            if has_text_layer:
                try:
                    # 注意：此处 page 是 PyMuPDF 页对象（非 pdfplumber），
                    # find_tables() 返回 TableFinder 对象，bool() 恒为 True，
                    # 必须判其 .tables 列表是否非空，否则每页都会误报含表格
                    has_table = bool(page.find_tables().tables)
                except Exception:
                    has_table = False

            if not has_text_layer:
                layout = "image-only"
                image_only_pages.append(index)
                # 无文本层页（整页扫描图/矢量图）本身就需要 OCR
                if index not in figure_pages:
                    figure_pages.append(index)
            else:
                layout = "double" if is_double else "single"
                if has_large_image:
                    figure_pages.append(index)
            if has_table:
                table_pages.append(index)
            if is_double:
                double_gutters.append(gutter)

            samples.append({
                "page_index": index,
                "has_text_layer": has_text_layer,
                "has_image": has_large_image or not has_text_layer,
                "estimated_layout": layout,
                "text_preview": text[:_PREVIEW_CHARS],
            })

        scanned = (
            bool(sample_n)
            and len(image_only_pages) / sample_n >= C.AGENT_LAYOUT_SCANNED_RATIO
        )
        return {
            "page_count": page_count,
            "format": "pdf",
            "is_scanned": scanned,
            # 扫描页没有词块不可能判双栏；双栏标记只服务文本 PDF 的提取提示
            "has_two_columns": bool(double_gutters) and not scanned,
            "column_gutter_estimate": (
                round(sum(double_gutters) / len(double_gutters), 3)
                if double_gutters else 0.0
            ),
            # 扫描件整篇走 OCR，不再单列图表页
            "pages_with_images": [] if scanned else figure_pages,
            # 抽样页中检测到表格线框的页码（提示 Director 可结构化抽表）
            "table_pages": table_pages,
            "page_samples": samples,
        }
    finally:
        doc.close()


def render_page_png(file_path: str, page_index: int,
                    dpi: int = C.AGENT_OCR_DPI) -> bytes:
    """把 PDF 指定页渲染为 PNG 字节（OCR 的统一输入）。

    Args:
        file_path: PDF 原件绝对路径。
        page_index: 页码索引（从 0 开始）。
        dpi: 渲染分辨率，默认 300（兼顾中文识别率与速度）。
    Returns:
        PNG 图片字节。
    Raises:
        FileInvalidError: 文件不存在/页码越界。
        LiteratureAgentError: PyMuPDF 不可用或渲染失败。
    """
    if not os.path.isfile(file_path):
        raise FileInvalidError("文献原件不存在，无法渲染页面")
    if os.path.splitext(file_path)[1].lower() != ".pdf":
        raise FileInvalidError("仅支持渲染 PDF 页面，TXT/DOCX 无需 OCR")
    if not _PYMUPDF_AVAILABLE:
        raise LiteratureAgentError("缺少 PyMuPDF 组件，无法渲染 PDF 页面")
    try:
        doc = pymupdf.open(file_path)
    except Exception as exc:
        raise LiteratureAgentError(f"PDF 原件无法打开：{exc}") from exc
    try:
        if doc.needs_pass and not doc.authenticate(""):
            raise FileInvalidError("PDF 已加密，无法识别页面内容")
        if page_index < 0 or page_index >= doc.page_count:
            raise FileInvalidError(
                f"页码 {page_index} 越界（全文共 {doc.page_count} 页）")
        zoom = max(72, int(dpi)) / 72.0
        matrix = pymupdf.Matrix(zoom, zoom)
        pixmap = doc[page_index].get_pixmap(matrix=matrix, alpha=False)
        return pixmap.tobytes("png")
    finally:
        doc.close()


def ocr_image_local(image_bytes: bytes,
                    lang: str = None) -> tuple[str, float]:
    """用本地 Tesseract 对页面图片做 OCR。

    Args:
        image_bytes: PNG 图片字节。
        lang: Tesseract 语言包，默认 chi_sim+eng。
    Returns:
        (识别文本, 平均置信度 0-1)；置信度无法计算时返回 0.0。
    Raises:
        LiteratureAgentError: pytesseract/Tesseract 未安装或识别过程失败。
    """
    if not is_local_ocr_available():
        raise LiteratureAgentError(
            "本地 OCR 不可用：请安装 Tesseract（含中文语言包 chi_sim）"
            "与 pytesseract，或在 AI 模型配置中启用视觉解析")
    use_lang = lang or C.AGENT_OCR_LANG
    try:
        import pytesseract
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes))
        data = pytesseract.image_to_data(
            image, lang=use_lang, config="--psm 3",
            output_type=pytesseract.Output.DICT,
        )
        confidence_values = [
            int(value) for value in data.get("conf", [])
            if str(value).lstrip("-").isdigit() and int(value) >= 0
        ]
        confidence = (
            round(sum(confidence_values) / len(confidence_values) / 100.0, 3)
            if confidence_values else 0.0
        )
        text = pytesseract.image_to_string(
            image, lang=use_lang, config="--psm 3")
        return (text or "").strip(), confidence
    except LiteratureAgentError:
        raise
    except Exception as exc:
        raise LiteratureAgentError(f"本地 OCR 识别失败：{exc}") from exc


def _image_area_ratio(page) -> float:
    """统计页面内嵌图片覆盖面积占页面总面积的比例（多图面积累加，上限 1.0）。

    Args:
        page: PyMuPDF 页面对象。
    Returns:
        覆盖比例 0.0-1.0。
    """
    page_area = max(1.0, page.rect.width * page.rect.height)
    total = 0.0
    for image_info in page.get_images(full=True) or []:
        xref = image_info[0]
        for rect in page.get_image_rects(xref) or []:
            total += max(0.0, rect.width * rect.height) / page_area
    return min(1.0, total)


def _detect_double_column(words: list, page_width: float) -> tuple:
    """根据词块水平分布判断页面是否双栏排版（适配中文按行成词）。

    判据：页面中央带（30%-70% 宽）内存在一条足够宽的低词块覆盖中缝，
    且中缝左右两侧都有不少于阈值的词块与字符数。

    Args:
        words: PyMuPDF get_text("words") 词块列表。
        page_width: 页面宽度（pt）。
    Returns:
        (是否双栏, 中缝位置比例 0-1)；非双栏时第二项为 0.0。
    """
    band_lo, band_hi = int(page_width * 0.30), int(page_width * 0.70)
    coverage: dict[int, int] = {}
    for word in words:
        x0, x1 = max(int(word[0]), band_lo), min(int(word[2]), band_hi)
        if x1 <= x0:
            continue
        for x in range(x0, x1 + 1):
            coverage[x] = coverage.get(x, 0) + 1

    best_gap = 0
    gap_center = -1
    run = 0
    run_start = band_lo
    for x in range(band_lo, band_hi + 1):
        if coverage.get(x, 0) <= 1:
            if run == 0:
                run_start = x
            run += 1
            if run > best_gap:
                best_gap = run
                gap_center = run_start + run // 2
        else:
            run = 0

    if best_gap < C.AGENT_LAYOUT_GUTTER_MIN_PT or gap_center < 0:
        return False, 0.0
    edge_lo, edge_hi = gap_center - best_gap / 2, gap_center + best_gap / 2
    left_words = [w for w in words if (w[0] + w[2]) / 2 < edge_lo]
    right_words = [w for w in words if (w[0] + w[2]) / 2 > edge_hi]
    left_chars = sum(len(w[4]) for w in left_words)
    right_chars = sum(len(w[4]) for w in right_words)
    if (len(left_words) < C.AGENT_LAYOUT_COLUMN_SIDE_WORDS
            or len(right_words) < C.AGENT_LAYOUT_COLUMN_SIDE_WORDS
            or left_chars < C.AGENT_LAYOUT_COLUMN_SIDE_CHARS
            or right_chars < C.AGENT_LAYOUT_COLUMN_SIDE_CHARS):
        return False, 0.0
    return True, round(gap_center / page_width, 3)
