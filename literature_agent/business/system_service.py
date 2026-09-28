"""系统配置业务：存储路径管理（含历史文件迁移）、磁盘占用、主题、操作日志、
AI 模型配置（API Key 使用 Fernet 加密后写入 system_config）。

安全约定：
- AI 密钥明文绝不出现在数据库中，库里只存 Fernet 密文（base64 字符串）；
- Fernet 本地密钥单独存放于用户目录下的密钥文件（~/.literature_agent/.secret_key），
  与数据库分离，避免库文件泄露即泄露密钥；
- 解密仅在 UI 层用户主动点击“查看”时临时发生。
"""
import json
import os

from cryptography.fernet import Fernet, InvalidToken

from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.log_dao import OperationLogDao
from tool_layer import file_helper
from utils.common_helper import format_file_size
from utils.logger import get_logger

from business.service_common import exception_to_code, write_operation_log

logger = get_logger()

# path_key（业务内简称）→ system_config 配置键
_PATH_KEY_MAP = {
    "literature": C.CFG_LITERATURE_PATH,
    "report": C.CFG_REPORT_PATH,
    "note": C.CFG_NOTE_PATH,
    "backup": C.CFG_BACKUP_PATH,
}
_PATH_DISPLAY = {
    "literature": "文献存储路径",
    "report": "解析报告存储路径",
    "note": "笔记存储路径",
    "backup": "备份存储路径",
}

_MASK_TEXT = "********"  # 密钥掩码
_APP_DIR = os.path.join(os.path.expanduser("~"), ".literature_agent")
_SECRET_KEY_FILE = os.path.join(_APP_DIR, ".secret_key")


class SystemService:
    """系统配置业务服务。"""

    def __init__(self):
        """初始化服务，组装所需 DAO 组件。"""
        self.config_dao = SystemConfigDao()
        self.log_dao = OperationLogDao()

    # ================= 预置配置 =================

    def ensure_default_config(self) -> None:
        """启动时确保 system_config 表具备全部默认键值（幂等）。"""
        defaults = {
            C.CFG_AUTO_SAVE_TIME: ("30", "自动保存频率（秒）"),
            C.CFG_BACKUP_CYCLE: ("7", "自动备份周期（天）"),
            C.CFG_EXPORT_DEFAULT_TYPE: ("PDF", "默认导出格式"),
            C.CFG_UI_STYLE: (C.THEME_LIGHT_NAME, "界面样式（light/dark）"),
            C.CFG_PARSE_DEFAULT_RULE: ("1", "默认解析规则ID"),
            C.CFG_PRECISION_DEFAULT: ("3", "默认解析精度"),
            C.CFG_AUTO_SAVE_DEBOUNCE: ("1", "自动保存防抖（秒）"),
            C.CFG_AI_MODELS: ("[]", "AI模型配置（加密密钥的JSON数组）"),
            C.CFG_LAST_PAGE: ("0", "上次退出时所在页面索引"),
            C.CFG_LAST_SETTING_MODULE: ("rules", "上次退出时管理中心子模块"),
            C.CFG_SEARCH_STATE: ("{}", "上次检索条件（JSON）"),
        }
        for key, (value, desc) in defaults.items():
            if self.config_dao.get(key) is None:
                self.config_dao.set(key, value, desc)

    def ensure_storage_dirs(self) -> None:
        """启动时确保四类存储目录均已创建（幂等）。

        笔记与解析报告正文存储在数据库中，对应目录用于存放导出文件；
        目录创建失败仅记日志，不阻断应用启动。
        """
        from business.service_common import get_base_dir
        for key in _PATH_KEY_MAP:
            try:
                file_helper.ensure_dir(get_base_dir(key))
            except Exception as exc:
                logger.error("初始化存储目录失败 %s：%s", key, exc, exc_info=True)

    # ================= 存储路径 =================

    def get_storage_paths(self) -> dict:
        """返回四个存储区当前生效的绝对路径。"""
        from business.service_common import get_base_dir
        return {key: get_base_dir(key) for key in _PATH_KEY_MAP}

    def set_storage_path(self, path_key: str, path_value: str,
                         migrate: bool = False,
                         progress_callback=None) -> tuple:
        """设置存储路径，可选迁移历史文件（迁移后库内相对路径保持不变）。

        Args:
            path_key: literature/report/note/backup。
            path_value: 新的绝对目录。
            migrate: True 时把旧目录全部文件按相对结构移动到新目录。
            progress_callback: 迁移进度回调 (percent, message)。
        Returns:
            (code, {"migrated": int}, msg)
        """
        config_key = _PATH_KEY_MAP.get(path_key)
        if not config_key:
            return C.CODE_FILE_INVALID, None, "未知的存储路径类型"
        path_value = (path_value or "").strip()
        if not path_value:
            return C.CODE_FILE_INVALID, None, "路径不能为空"
        try:
            new_dir = file_helper.ensure_dir(path_value)
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

        from business.service_common import get_base_dir
        old_dir = os.path.realpath(get_base_dir(path_key))
        migrated = 0
        if migrate and os.path.isdir(old_dir) and os.path.realpath(new_dir) != old_dir:
            code, migrated, msg = self._migrate_files(
                old_dir, new_dir, progress_callback
            )
            if code != C.CODE_SUCCESS:
                return code, None, msg

        try:
            self.config_dao.set(config_key, new_dir)
            display = _PATH_DISPLAY.get(path_key, path_key)
            write_operation_log(
                C.OP_CONFIG,
                f"修改{display}：{old_dir} → {new_dir}"
                + (f"（迁移 {migrated} 个文件）" if migrated else ""),
            )
            return C.CODE_SUCCESS, {"migrated": migrated}, "存储路径已保存"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def _migrate_files(self, old_dir: str, new_dir: str,
                       progress_callback) -> tuple:
        """把旧目录内全部文件按相对结构安全移动到新目录。"""
        files = []
        for root, _dirs, names in os.walk(old_dir):
            for name in names:
                files.append(os.path.join(root, name))
        total = len(files)
        try:
            for index, src in enumerate(files):
                rel = os.path.relpath(src, old_dir)
                dst = os.path.join(new_dir, rel)
                file_helper.safe_move(src, dst)
                if progress_callback:
                    percent = int((index + 1) * 100 / (total or 1))
                    progress_callback(percent, f"迁移文件 {index + 1}/{total}")
            return C.CODE_SUCCESS, total, "迁移完成"
        except Exception as exc:
            return exception_to_code(exc), 0, f"文件迁移失败：{getattr(exc, 'message', exc)}"

    def get_disk_info(self) -> dict:
        """获取各存储区路径、磁盘占用与目录占用。

        Returns:
            {"dirs": {path_key: {"path","exists","dir_size","dir_size_text",
                    "total","used","free","percent"}},
             "db_size": int, "db_size_text": str}。
            解析报告、笔记、标签与配置等结构化数据存于数据库文件，
            其占用单独以 db_size 返回供 UI 展示。
        """
        from business.service_common import get_base_dir
        info = {}
        for key in _PATH_KEY_MAP:
            path = get_base_dir(key)
            disk = file_helper.get_disk_usage(path)
            dir_size = self._calc_dir_size(path)
            percent = (
                round(disk["used"] * 100.0 / disk["total"], 1)
                if disk.get("total") else 0
            )
            info[key] = {
                "path": path,
                "exists": os.path.isdir(path),
                "dir_size": dir_size,
                "dir_size_text": format_file_size(dir_size),
                "total": disk["total"],
                "used": disk["used"],
                "free": disk["free"],
                "total_text": format_file_size(disk["total"]),
                "used_text": format_file_size(disk["used"]),
                "free_text": format_file_size(disk["free"]),
                "percent": percent,
            }
        db_size = self._calc_db_size()
        return {
            "dirs": info,
            "db_size": db_size,
            "db_size_text": format_file_size(db_size),
        }

    @staticmethod
    def _calc_db_size() -> int:
        """统计数据库文件占用（主库 + WAL 日志 + SHM 共享内存，内存库返回 0）。"""
        try:
            from data_layer.db_connect import DatabaseManager
            db_path = DatabaseManager().db_path
        except Exception as exc:
            logger.warning("读取数据库路径失败：%s", exc)
            return 0
        if not db_path or db_path == ":memory:":
            return 0
        total = 0
        for suffix in ("", "-wal", "-shm"):
            candidate = db_path + suffix
            try:
                if os.path.isfile(candidate):
                    total += os.path.getsize(candidate)
            except OSError:
                continue
        return total

    @staticmethod
    def _calc_dir_size(path: str) -> int:
        """统计目录内全部文件字节数（目录不存在返回 0）。"""
        total = 0
        if not os.path.isdir(path):
            return total
        for root, _dirs, names in os.walk(path):
            for name in names:
                full = os.path.join(root, name)
                try:
                    total += os.path.getsize(full)
                except OSError:
                    continue
        return total

    # ================= 主题 =================

    def set_ui_style(self, style: str) -> tuple:
        """持久化界面主题（light/dark）。"""
        if style not in (C.THEME_LIGHT_NAME, C.THEME_DARK_NAME):
            return C.CODE_FILE_INVALID, None, "不支持的主题类型"
        try:
            self.config_dao.set(C.CFG_UI_STYLE, style)
            return C.CODE_SUCCESS, None, "主题已保存"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    # ================= 操作日志 =================

    def write_log(self, operate_type: str, operate_content: str,
                  lit_id: int = 0, status: int = C.OP_STATUS_SUCCESS) -> None:
        """统一日志写入入口（委托 service_common，失败不阻断业务）。"""
        write_operation_log(operate_type, operate_content, lit_id, status)

    def get_recent_logs(self, limit: int = 50) -> list:
        """获取最近 N 条操作日志。"""
        return self.log_dao.get_recent(limit)

    # ================= AI 模型 =================

    def list_ai_models(self, decrypt: bool = False) -> list:
        """返回 AI 模型列表。

        Args:
            decrypt: True 时返回明文密钥（仅用户主动查看时使用）；
                     默认返回掩码 "********"。
        Returns:
            [{"name","base_url","api_key","is_current"}, ...]。
        """
        models = self._read_ai_models_raw()
        fernet = self._get_fernet()
        result = []
        for model in models:
            cipher = model.get("api_key", "")
            if decrypt and cipher:
                api_key = self._decrypt_key(fernet, cipher)
            elif cipher:
                api_key = _MASK_TEXT
            else:
                api_key = ""
            result.append({
                "name": model.get("name", ""),
                "base_url": model.get("base_url", ""),
                "api_key": api_key,
                "is_current": bool(model.get("is_current")),
                # 老配置无该字段时默认自动识别
                "file_channel": model.get("file_channel")
                or C.AI_FILE_CHANNEL_AUTO,
            })
        return result

    def get_current_ai_model(self, decrypt: bool = False):
        """获取当前选中的 AI 模型（无则 None）。"""
        for model in self.list_ai_models(decrypt):
            if model["is_current"]:
                return model
        return None

    def add_ai_model(self, model_name: str, base_url: str = "",
                     api_key: str = "") -> tuple:
        """新增 AI 模型（密钥 Fernet 加密存储）。

        Returns:
            (code, model_name, msg)
        """
        model_name = (model_name or "").strip()
        if not model_name:
            return C.CODE_FILE_INVALID, None, "模型名称不能为空"
        models = self._read_ai_models_raw()
        if any(m.get("name") == model_name for m in models):
            return C.CODE_DUPLICATE, None, "已存在同名模型"
        entry = {
            "name": model_name,
            "base_url": (base_url or "").strip(),
            "api_key": self._encrypt_key(api_key or ""),
            "is_current": not models,  # 首个模型自动选中
            "file_channel": C.AI_FILE_CHANNEL_AUTO,
        }
        models.append(entry)
        code, _data, msg = self._write_ai_models(models, f"新增 AI 模型：{model_name}")
        return code, model_name if code == C.CODE_SUCCESS else None, msg

    def delete_ai_model(self, model_name: str) -> tuple:
        """删除 AI 模型；若删除的是当前模型，自动选中剩余的第一个。"""
        models = self._read_ai_models_raw()
        target = next((m for m in models if m.get("name") == model_name), None)
        if not target:
            return C.CODE_FILE_NOT_FOUND, None, "模型不存在"
        remaining = [m for m in models if m.get("name") != model_name]
        was_current = bool(target.get("is_current"))
        if was_current and remaining:
            remaining[0]["is_current"] = True
        code, _data, msg = self._write_ai_models(
            remaining, f"删除 AI 模型：{model_name}"
        )
        return code, None, msg

    def set_ai_model(self, model_name: str) -> tuple:
        """切换当前使用的 AI 模型。"""
        models = self._read_ai_models_raw()
        if not any(m.get("name") == model_name for m in models):
            return C.CODE_FILE_NOT_FOUND, None, "模型不存在"
        for model in models:
            model["is_current"] = model.get("name") == model_name
        return self._write_ai_models(models, f"切换当前 AI 模型：{model_name}")

    def set_ai_key(self, model_name: str, api_key: str = None,
                   base_url: str = None, file_channel: str = None) -> tuple:
        """更新指定模型的 API 密钥（加密落库），可选同时更新地址/原件通道。

        Args:
            model_name: 模型名称。
            api_key: 明文密钥；传 None 表示本次不修改密钥（仅更新地址），
                     传空串表示显式清空密钥。
            base_url: 接口地址；None 表示不修改。
            file_channel: 原件解析通道（auto/file_id/file_data/none）；
                          None 表示不修改。
        Returns:
            (code, data, msg)。
        """
        models = self._read_ai_models_raw()
        target = next((m for m in models if m.get("name") == model_name), None)
        if not target:
            return C.CODE_FILE_NOT_FOUND, None, "模型不存在"
        if api_key is not None:
            target["api_key"] = self._encrypt_key(api_key)
        if base_url is not None:
            target["base_url"] = base_url.strip()
        if file_channel is not None:
            if file_channel not in C.AI_FILE_CHANNELS:
                return C.CODE_FILE_INVALID, None, "不支持的原件解析通道"
            target["file_channel"] = file_channel
        return self._write_ai_models(models, f"更新 AI 模型配置：{model_name}")

    # ================= AI 配置校验 =================

    def validate_ai_config(self, model_name: str = None, base_url: str = None,
                           api_key: str = None,
                           file_channel: str = None) -> tuple:
        """校验一份 AI 配置（可用于未保存的表单草稿，不落库、不写日志）。

        依次检测三项：
        1. 接口地址/密钥能否正常连通并通过鉴权（最小对话探测）；
        2. 模型输出能否正确返回非空文本（回显一句样例回复）；
        3. 模型能否直读 PDF 原件（内置微型 PDF，按所选通道实测）。
        探测全程只发送测试语句与内置测试文件，不接触任何用户文献。

        Args:
            model_name: 模型名称（作为 model 参数发送）。
            base_url: 接口根地址。
            api_key: 明文密钥；为空时尝试按模型名读取已保存密钥。
            file_channel: 表单选择的原件通道（auto/file_id/file_data/none）；
                          None 时按自动识别处理。
        Returns:
            (code, result_dict, msg)：对话探测通过即 code=0
            （PDF 直读不通过不影响可用性结论，仅提示走全文文本模式）。
            result["detected_channel"] 为实测可用的通道，供界面回填配置。
        """
        from tool_layer import ai_client
        from utils.exceptions import AIServiceError

        name = (model_name or "").strip()
        url = (base_url or "").strip()
        key = (api_key or "").strip()
        channel = file_channel or C.AI_FILE_CHANNEL_AUTO
        if channel not in C.AI_FILE_CHANNELS:
            channel = C.AI_FILE_CHANNEL_AUTO
        result = {
            "name": name,
            "base_url": url,
            "chat_ok": False,
            "chat_reply": "",
            "chat_error": "",
            "file_ok": None,
            "file_reply": "",
            "file_error": "",
            "file_channel": channel,
            "detected_channel": "",
            "name_hint_file": ai_client.model_supports_file(name, url),
        }
        if not name:
            return C.CODE_FILE_INVALID, result, "模型名称不能为空"
        if not url:
            return C.CODE_FILE_INVALID, result, "接口地址不能为空"
        if not url.lower().startswith(("http://", "https://")):
            return C.CODE_FILE_INVALID, result, "接口地址需以 http:// 或 https:// 开头"
        if not key:
            stored = next(
                (m for m in self.list_ai_models(decrypt=True)
                 if m["name"] == name), None,
            )
            key = (stored or {}).get("api_key", "")
            if not key or key.startswith("[密钥无法解密"):
                return C.CODE_FILE_INVALID, result, "API 密钥为空或无法解密，请重新填写"

        # 1+2：连通/鉴权与正常输出
        try:
            reply = ai_client.probe_chat(url, key, name)
            result["chat_ok"] = True
            result["chat_reply"] = reply[:C.AI_PROBE_REPLY_PREVIEW]
        except AIServiceError as exc:
            result["chat_error"] = exc.message
            return C.CODE_AI_SERVICE, result, exc.message
        except Exception as exc:  # 探测兜底：不向 UI 抛原始堆栈
            message = f"校验请求发生异常：{exc}"
            result["chat_error"] = message
            logger.error("AI 配置校验异常：%s", exc, exc_info=True)
            return C.CODE_AI_SERVICE, result, message

        # 3：PDF 原件直读能力（失败只是不支持该通道，配置整体仍可用）
        # 用户选择“仅全文文本”时不发送任何文件探测请求
        if channel == C.AI_FILE_CHANNEL_NONE:
            result["file_ok"] = False
            result["file_error"] = "已选择仅使用全文文本通道，未进行原件直读探测。"
        else:
            try:
                file_reply, used_channel = ai_client.probe_file_parsing(
                    url, key, name, mode=channel,
                )
                result["file_ok"] = True
                result["file_reply"] = file_reply[:C.AI_PROBE_REPLY_PREVIEW]
                result["detected_channel"] = used_channel
            except AIServiceError as exc:
                result["file_ok"] = False
                result["file_error"] = exc.message
            except Exception as exc:
                result["file_ok"] = False
                result["file_error"] = f"文件探测发生异常：{exc}"
                logger.error("AI 文件能力校验异常：%s", exc, exc_info=True)
        return C.CODE_SUCCESS, result, "校验完成"

    # ================= AI 加解密内部实现 =================

    def _read_ai_models_raw(self) -> list:
        """读取并解析 ai_models 配置 JSON（异常时返回空列表，绝不抛给 UI）。"""
        raw = self.config_dao.get(C.CFG_AI_MODELS, "[]")
        try:
            data = json.loads(raw or "[]")
            return data if isinstance(data, list) else []
        except (TypeError, ValueError) as exc:
            logger.error("ai_models 配置解析失败：%s", exc)
            return []

    def _write_ai_models(self, models: list, log_content: str) -> tuple:
        """序列化写回 ai_models 配置并写日志。"""
        try:
            self.config_dao.set(
                C.CFG_AI_MODELS,
                json.dumps(models, ensure_ascii=False),
            )
            write_operation_log(C.OP_CONFIG, log_content)
            return C.CODE_SUCCESS, None, "已保存"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    @staticmethod
    def _load_or_create_key() -> bytes:
        """读取本地 Fernet 密钥，首次使用时生成并写入密钥文件。"""
        if os.path.isfile(_SECRET_KEY_FILE):
            with open(_SECRET_KEY_FILE, "rb") as fp:
                key = fp.read().strip()
            if key:
                return key
        os.makedirs(_APP_DIR, exist_ok=True)
        key = Fernet.generate_key()
        # 仅当前用户可读写（Windows 上 chmod 有限支持，至少去除其他用户位）
        with open(_SECRET_KEY_FILE, "wb") as fp:
            fp.write(key)
        try:
            os.chmod(_SECRET_KEY_FILE, 0o600)
        except OSError:
            pass
        return key

    def _get_fernet(self) -> Fernet:
        """构造 Fernet 加解密器。"""
        return Fernet(self._load_or_create_key())

    @staticmethod
    def _encrypt_key(api_key: str) -> str:
        """加密明文密钥为 base64 密文；空串原样返回。"""
        if not api_key:
            return ""
        fernet = SystemService._get_fernet_static()
        return fernet.encrypt(api_key.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decrypt_key(fernet: Fernet, cipher_text: str) -> str:
        """解密密钥；密文失效（如密钥文件被换）时返回错误提示串，不抛异常。"""
        try:
            return fernet.decrypt(cipher_text.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError):
            return "[密钥无法解密：本地密钥可能已更换]"

    @staticmethod
    def _get_fernet_static() -> Fernet:
        """静态方法场景下的 Fernet 实例获取。"""
        return Fernet(SystemService._load_or_create_key())
