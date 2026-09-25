"""阶段3：系统配置业务测试（存储路径迁移、AI 模型密钥加密）。"""
import json

import pytest

from business.system_service import SystemService
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from business.literature_import import LiteratureImportService


@pytest.fixture
def storage(tmp_path):
    SystemConfigDao().set(C.CFG_LITERATURE_PATH, str(tmp_path / "lit_store"))
    return str(tmp_path / "lit_store")


def test_storage_path_migrate_keeps_relative_path(mem_conn, storage, tmp_path):
    """迁移后库内相对路径不变，文件落在新目录，配置已更新。"""
    src_file = tmp_path / "src_paper.txt"
    src_file.write_text("研究背景：路径迁移测试。\n", encoding="utf-8")
    code, lit, msg = LiteratureImportService().import_single(str(src_file))
    assert code == C.CODE_SUCCESS, msg
    rel_path = lit["file_path"]

    new_dir = str(tmp_path / "new_store")
    service = SystemService()
    code, data, msg = service.set_storage_path("literature", new_dir, migrate=True)
    assert code == C.CODE_SUCCESS, msg
    assert data["migrated"] == 1

    # 库里相对路径不变，新目录存在文件，旧目录清空
    assert LiteratureInfoDao().select_by_id(lit["id"])["file_path"] == rel_path
    import os
    assert os.path.isfile(os.path.join(new_dir, rel_path))

    # 非法类型 / 空路径
    assert service.set_storage_path("unknown", new_dir)[0] == C.CODE_FILE_INVALID
    assert service.set_storage_path("literature", " ")[0] == C.CODE_FILE_INVALID


def test_ai_models_key_encrypted_in_db(mem_conn, tmp_path, monkeypatch):
    """AI 密钥落库必须为密文，读取默认掩码，解密可见明文。"""
    secret_file = tmp_path / ".secret_key"
    monkeypatch.setattr(
        "business.system_service._SECRET_KEY_FILE", str(secret_file)
    )
    service = SystemService()
    code, name, msg = service.add_ai_model(
        "通义测试", "https://api.example.com", "sk-plain-secret-123"
    )
    assert code == C.CODE_SUCCESS, msg

    raw = SystemConfigDao().get(C.CFG_AI_MODELS)
    assert "sk-plain-secret-123" not in raw  # 库中无明文
    stored = json.loads(raw)
    assert stored[0]["api_key"] != "sk-plain-secret-123"
    assert stored[0]["is_current"] is True  # 首个自动选中

    models = service.list_ai_models()
    assert models[0]["api_key"] == "********"
    decrypted = service.list_ai_models(decrypt=True)
    assert decrypted[0]["api_key"] == "sk-plain-secret-123"

    # 重名拒绝
    assert service.add_ai_model("通义测试")[0] == C.CODE_DUPLICATE

    # 新增第二个并切换
    code, _n, _ = service.add_ai_model("模型B", "", "key-b")
    assert code == C.CODE_SUCCESS
    assert service.set_ai_model("模型B")[0] == C.CODE_SUCCESS
    assert service.get_current_ai_model()["name"] == "模型B"

    # 更新密钥后密文变化、明文可解
    assert service.set_ai_key("模型B", "key-b2")[0] == C.CODE_SUCCESS
    assert service.list_ai_models(decrypt=True)[1]["api_key"] == "key-b2"

    # 删除当前模型，剩余模型自动选中
    assert service.delete_ai_model("模型B")[0] == C.CODE_SUCCESS
    assert service.get_current_ai_model()["name"] == "通义测试"
    assert service.delete_ai_model("不存在")[0] == C.CODE_FILE_NOT_FOUND
    assert service.set_ai_model("不存在")[0] == C.CODE_FILE_NOT_FOUND


def test_ui_style_and_logs(mem_conn):
    """主题持久化与日志读写。"""
    service = SystemService()
    assert service.set_ui_style(C.THEME_DARK_NAME)[0] == C.CODE_SUCCESS
    assert SystemConfigDao().get(C.CFG_UI_STYLE) == C.THEME_DARK_NAME
    assert service.set_ui_style("ocean")[0] == C.CODE_FILE_INVALID

    service.write_log(C.OP_CONFIG, "测试日志条目")
    logs = service.get_recent_logs(5)
    assert logs and logs[0]["operate_content"] == "测试日志条目"


def test_ensure_default_config_idempotent(mem_conn):
    """默认配置初始化幂等，不覆盖已有值。"""
    service = SystemService()
    service.ensure_default_config()
    SystemConfigDao().set(C.CFG_UI_STYLE, C.THEME_DARK_NAME)
    service.ensure_default_config()
    assert SystemConfigDao().get(C.CFG_UI_STYLE) == C.THEME_DARK_NAME


def test_ensure_storage_dirs_creates_all(mem_conn, tmp_path):
    """启动初始化后四类存储目录全部存在，且幂等不报错。"""
    import os
    config_dao = SystemConfigDao()
    for index, key in enumerate((
        C.CFG_LITERATURE_PATH, C.CFG_REPORT_PATH,
        C.CFG_NOTE_PATH, C.CFG_BACKUP_PATH,
    )):
        config_dao.set(key, str(tmp_path / f"store_{index}"))
    service = SystemService()
    service.ensure_storage_dirs()
    info = service.get_disk_info()
    for path_key in ("literature", "report", "note", "backup"):
        assert os.path.isdir(info["dirs"][path_key]["path"])
        assert info["dirs"][path_key]["exists"] is True
    # 幂等：再次执行不抛异常
    service.ensure_storage_dirs()


def test_get_disk_info_counts_files_and_db(mem_conn, tmp_path):
    """目录占用按真实文件统计；内存库 db_size 为 0；返回结构含 dirs/db_size。"""
    import os
    report_dir = tmp_path / "report_store"
    SystemConfigDao().set(C.CFG_REPORT_PATH, str(report_dir))
    service = SystemService()
    service.ensure_storage_dirs()
    info = service.get_disk_info()
    assert set(info.keys()) == {"dirs", "db_size", "db_size_text"}
    assert info["dirs"]["report"]["dir_size"] == 0
    # 内存数据库无实体文件
    assert info["db_size"] == 0
    # 写入导出文件后占用被统计到
    (report_dir / "解析报告_demo.txt").write_text("报告内容", encoding="utf-8")
    info2 = service.get_disk_info()
    assert info2["dirs"]["report"]["dir_size"] > 0
