"""阶段2 B2-9：文献导入业务 + 文件工具单元测试。"""
import os

import pytest

from business.literature_import import DUP_OVERWRITE, DUP_SKIP, LiteratureImportService
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from tool_layer import file_helper
from utils.exceptions import FileInvalidError

SAMPLE_TEXT = (
    "摘要\n这是一篇关于深度学习的测试文献摘要。\n\n"
    "引言\n近年来人工智能快速发展。\n\n"
    "结论\n深度学习方法有效。\n"
)


@pytest.fixture
def storage(tmp_path):
    """把文献存储基准目录重定向到临时目录。"""
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "lit_store"))
    return str(tmp_path / "lit_store")


def _make_txt(tmp_path, name="sample.txt", content=SAMPLE_TEXT):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return str(path)


def test_import_single_success(mem_conn, storage, tmp_path):
    """正常 TXT 导入：入库 + 复制到受管目录 + 相对路径。"""
    src = _make_txt(tmp_path)
    code, data, msg = LiteratureImportService().import_single(src)
    assert code == C.CODE_SUCCESS, msg
    assert data["id"] > 0
    assert data["literature_type"] == C.LIT_TYPE_TXT
    assert data["is_parsed"] == C.PARSE_NOT_STARTED
    assert data["file_size"] > 0
    assert len(data["file_hash"]) == 64
    # 相对路径不含盘符且文件真实存在
    assert ":" not in data["file_path"]
    abs_path = file_helper.resolve_path(
        data["file_path"], storage, "literature"
    )
    assert os.path.isfile(abs_path)


def test_import_invalid_suffix(mem_conn, storage, tmp_path):
    bad = tmp_path / "bad.bin"
    bad.write_bytes(b"hello")
    code, _data, msg = LiteratureImportService().import_single(str(bad))
    assert code == C.CODE_FILE_INVALID
    assert "格式" in msg


def test_import_file_not_found(mem_conn, storage):
    code, _data, _msg = LiteratureImportService().import_single("Z:/nope/missing.txt")
    assert code == C.CODE_FILE_NOT_FOUND


def test_import_empty_file(mem_conn, storage, tmp_path):
    empty = tmp_path / "empty.txt"
    empty.write_text("", encoding="utf-8")
    code, _data, _msg = LiteratureImportService().import_single(str(empty))
    assert code == C.CODE_FILE_INVALID


def test_duplicate_skip_and_overwrite(mem_conn, storage, tmp_path):
    src = _make_txt(tmp_path)
    service = LiteratureImportService()
    code, first, _ = service.import_single(src, DUP_SKIP)
    assert code == 0

    # 再次导入同文件 → 重复
    code, data, _ = service.import_single(src, DUP_SKIP)
    assert code == C.CODE_DUPLICATE
    assert data["existing"]["id"] == first["id"]

    # 覆盖导入 → 新 id 且库中只剩 1 条
    code, replaced, msg = service.import_single(src, DUP_OVERWRITE)
    assert code == 0, msg
    assert replaced["id"] != first["id"]
    assert LiteratureInfoDao().count_all() == 1


def test_check_duplicate(mem_conn, storage, tmp_path):
    src = _make_txt(tmp_path)
    service = LiteratureImportService()
    assert service.check_duplicate(src) is None
    service.import_single(src)
    hit = service.check_duplicate(src)
    assert hit is not None and hit["file_hash"]


def test_import_batch_mix(mem_conn, storage, tmp_path):
    ok1 = _make_txt(tmp_path, "a.txt")
    ok2 = _make_txt(tmp_path, "b.txt", "另一篇文献的正文内容。")
    bad = tmp_path / "c.bin"
    bad.write_bytes(b"x")
    service = LiteratureImportService()
    service.import_single(ok1)  # 制造重复项

    result = service.import_batch([ok1, ok2, str(bad)])
    assert len(result["success"]) == 1
    assert len(result["duplicate"]) == 1
    assert len(result["failed"]) == 1

    # 进度回调被逐条调用
    progress = []
    service.import_batch([ok2], lambda cur, total, p, c, m: progress.append((cur, total)))
    assert progress == [(1, 1)]


def test_clear_all_records(mem_conn, storage, tmp_path):
    service = LiteratureImportService()
    service.import_single(_make_txt(tmp_path, "a.txt"))
    service.import_single(_make_txt(tmp_path, "b.txt", "第二篇。"))
    code, count, msg = service.clear_all_records()
    assert code == 0 and count == 2 and msg == "清空成功"
    assert LiteratureInfoDao().count_all() == 0


def test_hash_and_path_security(tmp_path):
    """hash 一致性 + 防穿越校验。"""
    f1 = tmp_path / "x.txt"
    f1.write_text("abc", encoding="utf-8")
    assert file_helper.calc_file_hash(str(f1)) == file_helper.calc_file_hash(str(f1))
    with pytest.raises(FileInvalidError):
        file_helper.sanitize_path("../../evil.txt", str(tmp_path))
    # 合法相对路径正常解析
    safe = file_helper.sanitize_path("sub/x.txt", str(tmp_path))
    assert safe.startswith(os.path.realpath(str(tmp_path)))


def test_get_lit_file_path_missing_source(mem_conn, storage, tmp_path):
    src = _make_txt(tmp_path)
    code, data, _ = LiteratureImportService().import_single(src)
    # 删除受管目录中的副本，模拟源文件丢失
    stored = file_helper.resolve_path(data["file_path"], storage, "literature")
    os.remove(stored)
    code, _abs, msg = LiteratureImportService().get_lit_file_path(data["id"])
    assert code == C.CODE_FILE_NOT_FOUND
    assert "移动或删除" in msg
