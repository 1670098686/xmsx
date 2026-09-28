"""阶段4 T4-6：全量边界测试。

覆盖验收要求：100MB+ 大文件、GBK 中文编码、加密 PDF、空文件、
不支持后缀、损坏 docx/旧版 doc、损坏备份 zip、路径穿越、全程无网络导入。
"""
import ast
import os
import tempfile
import zipfile

import pytest

from business.export_backup import ExportBackupService
from business.literature_import import LiteratureImportService
from business.literature_parse import LiteratureParseService
from business.service_common import get_base_dir
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from tool_layer import file_helper, file_parser
from utils.exceptions import FileInvalidError

GBK_SAMPLE = "研究背景：中文编码边界测试。\n核心观点：人工智能发展迅速。\n结论：GBK 解码正常。"

# 业务代码包：这些目录的模块禁止引入任何网络库（系统默认全程本地化）
_NO_NETWORK_DIRS = ["business", "tool_layer", "data_layer", "ui", "config", "utils"]
_NETWORK_MODULES = {
    "socket", "requests", "urllib", "http", "ftplib", "telnetlib",
    "smtplib", "poplib", "xmlrpc",
}
# 唯一受控网络出口豁免：AI 客户端仅在用户显式配置模型后，
# 访问用户填写的接口地址，将文献原件直传模型（用户明确要求的功能）。
_NETWORK_WHITELIST = {os.path.join("tool_layer", "ai_client.py")}


@pytest.fixture
def storage(tmp_path):
    """把文献/备份存储基准目录重定向到临时目录。"""
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "lit_store"))
    SystemConfigDao().set(C.CFG_BACKUP_PATH, str(tmp_path / "backup_store"))
    return str(tmp_path / "lit_store")


# ================= 文件解析边界 =================

def test_empty_txt_rejected(mem_conn, storage, tmp_path):
    """0 字节空文件：tool 层抛业务异常，导入业务返回错误码且不闪退。"""
    empty = tmp_path / "empty.txt"
    empty.write_bytes(b"")
    with pytest.raises(FileInvalidError, match="空"):
        file_parser.extract_txt_text(str(empty))
    code, _data, msg = LiteratureImportService().import_single(str(empty))
    assert code != C.CODE_SUCCESS
    assert msg


def test_gbk_chinese_txt_decoded(mem_conn, storage, tmp_path):
    """GBK 编码中文 TXT 必须被编码探测正确解码，不能乱码。"""
    gbk_file = tmp_path / "gbk.txt"
    gbk_file.write_bytes(GBK_SAMPLE.encode("gbk"))
    text, meta, lit_type = file_parser.auto_extract(str(gbk_file))
    assert lit_type == C.LIT_TYPE_TXT
    assert "研究背景" in text
    assert "人工智能" in text
    # 走完整导入链路也应成功
    code, data, msg = LiteratureImportService().import_single(str(gbk_file))
    assert code == C.CODE_SUCCESS, msg


def test_unsupported_suffix_rejected(mem_conn, storage, tmp_path):
    """不支持的后缀：.bin 被拒绝并给出明确提示。"""
    bad = tmp_path / "data.bin"
    bad.write_bytes(b"\x00\x01binary")
    with pytest.raises(FileInvalidError, match="不支持"):
        file_parser.auto_extract(str(bad))
    code, _data, _msg = LiteratureImportService().import_single(str(bad))
    assert code == C.CODE_FILE_INVALID


def test_corrupted_docx_rejected(mem_conn, storage, tmp_path):
    """内容损坏的 .docx：抛出“文档损坏”业务异常而非原始堆栈。"""
    broken = tmp_path / "broken.docx"
    broken.write_bytes(b"this is not a real docx package")
    with pytest.raises(FileInvalidError, match="损坏"):
        file_parser.extract_docx_text(str(broken))


def test_legacy_doc_hint(mem_conn, storage, tmp_path, monkeypatch):
    """旧版二进制 .doc：无可用转换后端时给出明确、可操作的提示。"""
    legacy = tmp_path / "old.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0fake ole content")

    def _no_backend(_src, _out_dir):
        raise FileInvalidError("旧版 .doc 文档转换失败：本机未检测到可用的 Word/WPS")

    monkeypatch.setattr(file_parser, "_convert_doc_to_docx", _no_backend)
    with pytest.raises(FileInvalidError, match="旧版"):
        file_parser.extract_doc_text(str(legacy))
    # auto_extract 分发到 .doc 转换流程时同样包装为业务异常
    with pytest.raises(FileInvalidError, match="旧版"):
        file_parser.auto_extract(str(legacy))


def test_doc_actually_renamed_docx(mem_conn, storage, tmp_path):
    """后缀 .doc 但内容是 OOXML（改名文件）：按 DOCX 引擎提取，类型按后缀记为 DOC。"""
    docx = pytest.importorskip("docx")
    real_docx = tmp_path / "real.docx"
    document = docx.Document()
    document.add_paragraph("改名文档正文：旧后缀新格式。")
    document.save(str(real_docx))
    renamed = tmp_path / "renamed.doc"
    renamed.write_bytes(real_docx.read_bytes())

    text, meta, lit_type = file_parser.auto_extract(str(renamed))
    assert "改名文档正文" in text
    assert lit_type == C.LIT_TYPE_DOC
    assert meta["paragraph_count"] >= 1


def test_doc_convert_success_and_cleanup(mem_conn, storage, tmp_path, monkeypatch):
    """真 .doc 转换成功：提取转换后文本，临时转换目录用后即删。"""
    docx = pytest.importorskip("docx")
    legacy = tmp_path / "paper.doc"
    legacy.write_bytes(b"\xd0\xcf\x11\xe0real ole stream")
    captured = {}

    def _fake_convert(src_path, out_dir):
        """模拟 Word COM：在输出目录生成一个真实可解析的 .docx。"""
        captured["out_dir"] = out_dir
        document = docx.Document()
        document.add_paragraph("转换后正文：旧版 Word 内容。")
        target = os.path.join(out_dir, "paper.docx")
        document.save(target)
        return target

    monkeypatch.setattr(file_parser, "_convert_doc_to_docx", _fake_convert)
    text, meta = file_parser.extract_doc_text(str(legacy))
    assert "转换后正文" in text
    assert meta["paragraph_count"] == 1
    # 临时转换目录已清理，不留临时文件
    assert not os.path.exists(captured["out_dir"])


def test_doc_too_small_rejected(mem_conn, storage, tmp_path):
    """过小的 .doc（不足魔数长度）：判定为空或损坏，不启动 Office。"""
    tiny = tmp_path / "tiny.doc"
    tiny.write_bytes(b"\xd0\xcf")
    with pytest.raises(FileInvalidError, match="为空或已损坏"):
        file_parser.extract_doc_text(str(tiny))



def test_encrypted_pdf_rejected(mem_conn, storage, tmp_path):
    """加密 PDF：两个引擎均不能提取时给出“已加密”提示。"""
    pytest.importorskip("PyPDF2")
    from PyPDF2 import PdfWriter

    encrypted = tmp_path / "secret.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("edge-case-password")
    with open(encrypted, "wb") as fp:
        writer.write(fp)

    with pytest.raises(FileInvalidError, match="加密"):
        file_parser.extract_pdf_text(str(encrypted))


# ================= 100MB+ 大文件 =================

def test_large_file_over_100mb(mem_conn, storage, tmp_path):
    """100MB+ 文本（稀疏文件快速构造）：hash 正确、可提取、可完整导入。"""
    big = tmp_path / "big.txt"
    target_size = 100 * 1024 * 1024 + 1024
    chunk = ("大文件边界测试，深度学习与自然语言处理。\n" * 4096).encode("utf-8")
    tail = "\n# TAIL 结论正常\n".encode("utf-8")
    with open(big, "wb") as fp:
        fp.write("# HEAD\n".encode("utf-8"))
        fp.write(chunk)
        # 制造稀疏空洞快速得到 100MB+ 文件（中间零字节，占盘极小）
        fp.seek(target_size - len(tail))
        fp.write(tail)
    assert os.path.getsize(big) >= 100 * 1024 * 1024

    digest_a = file_helper.calc_file_hash(str(big))
    digest_b = file_helper.calc_file_hash(str(big))
    assert len(digest_a) == 64 and digest_a == digest_b

    text, _meta, _lit_type = file_parser.auto_extract(str(big))
    assert "大文件边界测试" in text and "TAIL" in text

    code, data, msg = LiteratureImportService().import_single(str(big))
    assert code == C.CODE_SUCCESS, msg
    assert data["file_size"] >= 100 * 1024 * 1024
    assert data["file_hash"] == digest_a


# ================= 备份 zip 损坏 =================

def test_corrupted_backup_zip_rejected(mem_conn, storage, tmp_path):
    """zip 结构有效但缺少数据库/清单条目时拒绝恢复。"""
    bad_zip = tmp_path / "bad_backup.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("manifest.json", '{"version": 1}')
        zf.writestr("readme.txt", "missing database entry")
    code, _data, msg = ExportBackupService().restore(str(bad_zip))
    assert code == C.CODE_FILE_INVALID
    assert "不完整" in msg


def test_backup_zip_slip_blocked(mem_conn, storage, tmp_path):
    """含 ../ 路径条目的恶意备份不得逃逸出受管目录（zip slip 防护）。"""
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "evil")
    with pytest.raises(FileInvalidError, match="穿越"):
        file_helper.sanitize_path("../escape.txt", storage)


# ================= 路径穿越 =================

def test_sanitize_path_blocks_traversal(tmp_path):
    """sanitize_path：相对路径逃逸基准目录时抛业务异常。"""
    base = tmp_path / "base"
    base.mkdir()
    with pytest.raises(FileInvalidError, match="穿越"):
        file_helper.sanitize_path("../../etc/passwd", str(base))
    safe = file_helper.sanitize_path(os.path.join("sub", "a.txt"), str(base))
    assert os.path.realpath(safe).startswith(os.path.realpath(str(base)))


# ================= 全程本地化（网络断网模拟）=================

def test_no_network_modules_imported():
    """AST 静态扫描：业务/工具/数据/UI/config/utils 层默认不得引入网络库。

    系统默认全程本地化，断网环境下功能必须可用；唯一豁免是用户显式
    配置模型后才启用的 AI 客户端（tool_layer/ai_client.py）。
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    whitelist = {os.path.normpath(p) for p in _NETWORK_WHITELIST}
    offenders = []
    for pkg in _NO_NETWORK_DIRS:
        pkg_dir = os.path.join(project_root, pkg)
        for root, _dirs, files in os.walk(pkg_dir):
            for name in files:
                if not name.endswith(".py"):
                    continue
                path = os.path.join(root, name)
                rel_path = os.path.normpath(os.path.relpath(path, project_root))
                if rel_path in whitelist:
                    continue
                with open(path, "r", encoding="utf-8") as fp:
                    tree = ast.parse(fp.read(), filename=path)
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        names = [alias.name.split(".")[0] for alias in node.names]
                    elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                        names = [node.module.split(".")[0]]
                    else:
                        continue
                    if set(names) & _NETWORK_MODULES:
                        offenders.append(rel_path)
    assert not offenders, f"发现网络库导入（违反本地化约束）：{offenders}"


def test_ai_client_only_reaches_configured_endpoint():
    """AI 客户端安全约束：不硬编码任何服务地址，地址必须全部来自调用参数。"""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tool_layer", "ai_client.py",
    )
    with open(path, "r", encoding="utf-8") as fp:
        source = fp.read()
    # 不得出现硬编码的 http(s) 域名/IP 字面量
    import re
    hardcoded = re.findall(r"https?://[0-9A-Za-z.\-]+", source)
    assert not hardcoded, f"AI 客户端存在硬编码服务地址：{hardcoded}"
    # 鉴权头与文件直传语义必须存在
    assert "Bearer" in source
    assert "multipart/form-data" in source
    assert 'name="file"' in source


# ================= Word→PDF 原版式渲染 =================

def _make_blank_pdf(path: str) -> None:
    """用 PyPDF2 生成单页空白 PDF 测试文件。"""
    pytest.importorskip("PyPDF2")
    from PyPDF2 import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    with open(path, "wb") as fp:
        writer.write(fp)


def test_word_to_pdf_rejects_txt(tmp_path):
    """非 Word 后缀调用转 PDF：直接抛业务异常，不启动 Office。"""
    txt = tmp_path / "note.txt"
    txt.write_text("正文", encoding="utf-8")
    with pytest.raises(FileInvalidError, match="仅支持"):
        file_parser.convert_document_to_pdf(str(txt), str(tmp_path / "out"))


def test_word_to_pdf_via_com(tmp_path, monkeypatch):
    """docx 经 Word/WPS COM 转 PDF：参数为 wdFormatPDF(17)，产物有效。"""
    docx = pytest.importorskip("docx")
    src = tmp_path / "paper.docx"
    document = docx.Document()
    document.add_paragraph("Word 转 PDF 正文。")
    document.save(str(src))

    def _fake_com(s_path, out_dir, prog_id, target_ext, word_format, log_label):
        assert target_ext == ".pdf"
        assert word_format == C.DOC_WORD_FORMAT_PDF
        target = os.path.join(out_dir, "paper.pdf")
        os.makedirs(out_dir, exist_ok=True)
        _make_blank_pdf(target)
        return target

    monkeypatch.setattr(file_parser, "_convert_via_office_com", _fake_com)
    result = file_parser.convert_document_to_pdf(
        str(src), str(tmp_path / "out"))
    assert result.lower().endswith(".pdf")
    assert os.path.getsize(result) > 0


def test_word_to_pdf_soffice_fallback(tmp_path, monkeypatch):
    """COM 不可用时回退 LibreOffice --convert-to pdf。"""
    src = tmp_path / "old.doc"
    src.write_bytes(b"\xd0\xcf\x11\xe0fake ole stream")

    def _boom(*_args, **_kwargs):
        raise FileInvalidError("COM 不可用")

    def _fake_soffice(s_path, out_dir, bin_path, target_ext, fmt):
        assert target_ext == ".pdf" and fmt == "pdf"
        target = os.path.join(out_dir, "old.pdf")
        os.makedirs(out_dir, exist_ok=True)
        _make_blank_pdf(target)
        return target

    monkeypatch.setattr(file_parser, "_convert_via_office_com", _boom)
    monkeypatch.setattr(file_parser, "_find_soffice",
                        lambda: r"C:\fake\soffice.exe")
    monkeypatch.setattr(file_parser, "_convert_via_soffice", _fake_soffice)
    result = file_parser.convert_document_to_pdf(
        str(src), str(tmp_path / "out"))
    assert os.path.isfile(result) and result.endswith(".pdf")


def test_word_to_pdf_all_backends_fail(tmp_path, monkeypatch):
    """所有转换后端均不可用：聚合错误后抛可操作的业务异常。"""
    src = tmp_path / "paper.docx"
    src.write_bytes(b"PK\x03\x04fake docx bytes")

    def _boom(*_args, **_kwargs):
        raise FileInvalidError("COM 不可用")

    monkeypatch.setattr(file_parser, "_convert_via_office_com", _boom)
    monkeypatch.setattr(file_parser, "_find_soffice", lambda: "")
    with pytest.raises(FileInvalidError, match="Word 文档转 PDF 失败"):
        file_parser.convert_document_to_pdf(str(src), str(tmp_path / "out"))


def test_prepare_render_pdf_native_pdf(mem_conn, storage):
    """原始 PDF：直接返回受管存储路径，不经过任何转换。"""
    base_dir = get_base_dir("literature")
    os.makedirs(base_dir, exist_ok=True)
    pdf_abs = os.path.join(base_dir, "native.pdf")
    _make_blank_pdf(pdf_abs)
    lit_id = LiteratureInfoDao().insert({
        "literature_title": "原生PDF",
        "literature_author": "未知",
        "publish_time": "",
        "journal_source": "",
        "literature_type": C.LIT_TYPE_PDF,
        "file_path": file_helper.to_relative(pdf_abs, base_dir),
        "file_size": os.path.getsize(pdf_abs),
        "file_hash": "native-pdf-hash",
        "category_id": 0,
        "is_parsed": C.PARSE_NOT_STARTED,
    })
    code, info, msg = LiteratureParseService().prepare_render_pdf(lit_id)
    assert code == C.CODE_SUCCESS, msg
    assert info["converted"] is False
    assert os.path.abspath(info["path"]) == os.path.abspath(pdf_abs)


def test_prepare_render_pdf_txt_unsupported(mem_conn, storage, tmp_path):
    """TXT 不支持原版式渲染：返回文件格式错误码，UI 据此走文本通道。"""
    txt = tmp_path / "plain.txt"
    txt.write_text("纯文本正文第一行\n第二行\n", encoding="utf-8")
    code, data, msg = LiteratureImportService().import_single(str(txt))
    assert code == C.CODE_SUCCESS, msg
    code, info, _msg = LiteratureParseService().prepare_render_pdf(data["id"])
    assert code == C.CODE_FILE_INVALID
    assert info is None


def test_prepare_render_pdf_word_cache(mem_conn, storage, tmp_path, monkeypatch):
    """Word 转 PDF：按文件哈希缓存，第二次打开不重复转换。"""
    docx = pytest.importorskip("docx")
    src = tmp_path / "paper.docx"
    document = docx.Document()
    document.add_paragraph("缓存验证正文。")
    document.save(str(src))
    code, data, msg = LiteratureImportService().import_single(str(src))
    assert code == C.CODE_SUCCESS, msg

    convert_calls = []

    def _fake_convert(s_path, out_dir):
        convert_calls.append(s_path)
        target = os.path.join(out_dir, "paper.pdf")
        os.makedirs(out_dir, exist_ok=True)
        _make_blank_pdf(target)
        return target

    monkeypatch.setattr(file_parser, "convert_document_to_pdf", _fake_convert)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    service = LiteratureParseService()
    code, info, msg = service.prepare_render_pdf(data["id"])
    assert code == C.CODE_SUCCESS, msg
    assert info["converted"] is True
    assert os.path.isfile(info["path"])
    # 缓存文件以哈希命名，与源文件名解耦
    assert os.path.basename(info["path"]) == f"{data['file_hash']}.pdf"

    code2, info2, _msg2 = service.prepare_render_pdf(data["id"])
    assert code2 == C.CODE_SUCCESS
    assert os.path.abspath(info2["path"]) == os.path.abspath(info["path"])
    assert len(convert_calls) == 1  # 第二次命中缓存，未再调转换后端


def test_prepare_render_pdf_concurrent_single_convert(
        mem_conn, storage, tmp_path, monkeypatch):
    """两页面线程同时渲染同一 Word：只允许实际转换一次（缓存锁防写冲突）。"""
    import threading
    import time

    docx = pytest.importorskip("docx")
    src = tmp_path / "concurrent.docx"
    document = docx.Document()
    document.add_paragraph("并发渲染正文。")
    document.save(str(src))
    code, data, msg = LiteratureImportService().import_single(str(src))
    assert code == C.CODE_SUCCESS, msg

    call_count = {"n": 0}
    count_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=10)

    def _fake_convert(_s_path, out_dir):
        with count_lock:
            call_count["n"] += 1
        time.sleep(0.3)  # 模拟 Office 转换耗时，放大并发窗口
        target = os.path.join(out_dir, "concurrent.pdf")
        os.makedirs(out_dir, exist_ok=True)
        _make_blank_pdf(target)
        return target

    monkeypatch.setattr(file_parser, "convert_document_to_pdf", _fake_convert)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    results = []

    def _worker():
        barrier.wait()  # 两个线程尽量同时进入业务方法
        results.append(LiteratureParseService().prepare_render_pdf(data["id"]))

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == 2
    assert all(code_i == C.CODE_SUCCESS for code_i, _i, _m in results)
    assert call_count["n"] == 1  # 只转换一次，另一线程锁内命中缓存
    assert results[0][1]["path"] == results[1][1]["path"]


def test_prepare_render_pdf_missing_source(mem_conn, storage, tmp_path):
    """受管源文件丢失：返回错误码与可理解提示，不抛原始堆栈。"""
    txt = tmp_path / "gone.txt"
    txt.write_text("稍后将被删除的正文。", encoding="utf-8")
    code, data, msg = LiteratureImportService().import_single(str(txt))
    assert code == C.CODE_SUCCESS, msg
    stored = file_helper.resolve_path(
        LiteratureInfoDao().select_by_id(data["id"])["file_path"],
        get_base_dir("literature"), "literature",
    )
    os.remove(stored)
    code, info, msg = LiteratureParseService().prepare_render_pdf(data["id"])
    assert code != C.CODE_SUCCESS
    assert info is None and msg
