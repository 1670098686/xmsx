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
import shutil
import subprocess
import tempfile

from utils.exceptions import FileInvalidError, LiteratureFileNotFoundError, ParseError
from utils.logger import get_logger

logger = get_logger()


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
                pages_text.append(page.extract_text() or "")
        text = "\n\n".join(pages_text).strip()
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
        text = "\n\n".join(pages_text).strip()
        if not text:
            raise FileInvalidError("PDF 无文本层（可能是扫描件），暂不支持 OCR")
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
            text = raw.decode(encoding).strip()
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
        (text, metadata, file_type) 三元组；file_type 为 PDF/TXT/DOCX。
    Raises:
        FileInvalidError: 后缀不支持。
    """
    from config import constants as C

    suffix = os.path.splitext(file_path)[1].lower()
    if suffix == ".pdf":
        text, meta = extract_pdf_text(file_path)
        return text, meta, C.LIT_TYPE_PDF
    if suffix == ".txt":
        text, meta = extract_txt_text(file_path)
        return text, meta, C.LIT_TYPE_TXT
    if suffix == ".docx":
        text, meta = extract_docx_text(file_path)
        return text, meta, C.LIT_TYPE_DOCX
    if suffix == ".doc":
        text, meta = extract_doc_text(file_path)
        return text, meta, C.LIT_TYPE_DOCX
    raise FileInvalidError(f"不支持的文件格式：{suffix}（仅支持 PDF/TXT/DOCX/DOC）")
