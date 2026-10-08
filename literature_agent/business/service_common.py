"""业务层公共辅助：操作日志写入、基准路径读取、异常→返回码转换。"""
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.log_dao import OperationLogDao
from tool_layer import file_helper
from utils.exceptions import LiteratureAgentError
from utils.logger import get_logger

logger = get_logger()


# ========== 删除前文件句柄释放钩子 ==========
# 背景：UI 预览器（PyMuPDF）打开 PDF 期间持有 Windows 文件句柄，若用户
# 预览后直接在检索页删除该文献，os.remove 会报 WinError 32。业务层不得
# 反向导入 UI 层，故采用依赖倒置：UI 启动时注册"文件即将删除"回调，
# 业务层在删除受管文件前统一触发，由 UI 侧关闭对应预览器释放句柄。
_pre_file_delete_hooks = []


def register_pre_file_delete_hook(hook) -> None:
    """注册"受管文件即将被删除"回调（UI 层启动时调用一次即可）。

    Args:
        hook: 可调用对象，入参为待删除文件的绝对路径（str），无返回值；
              回调内异常只记录日志，不会阻断删除主流程。
    Note:
        当前删除流程（检索页单删/批删、清空导入记录、覆盖导入清理旧件）
        均在 UI 主线程同步执行，回调内可直接操作 QWidget；若后续删除
        改为子线程执行，回调需自行做线程封送。
    """
    if callable(hook) and hook not in _pre_file_delete_hooks:
        _pre_file_delete_hooks.append(hook)


def notify_file_will_delete(abs_path: str) -> None:
    """删除受管文件前触发句柄释放回调（business 层内部调用）。

    Args:
        abs_path: 即将删除的文件绝对路径。
    """
    if not abs_path:
        return
    for hook in list(_pre_file_delete_hooks):
        try:
            hook(abs_path)
        except Exception as exc:  # 回调失败不允许阻断删除
            logger.warning("删除前句柄释放回调执行失败 %s：%s", abs_path, exc)


def write_operation_log(operate_type: str, operate_content: str,
                        lit_id: int = 0, status: int = C.OP_STATUS_SUCCESS) -> None:
    """统一写操作日志（失败仅记文件日志，绝不阻断主业务）。

    Args:
        operate_type: constants.OP_* 操作类型。
        operate_content: 操作内容描述。
        lit_id: 关联文献 id（无则 0）。
        status: 1 成功 / 0 失败。
    """
    try:
        OperationLogDao().insert({
            "operate_type": operate_type,
            "operate_content": operate_content,
            "literature_id": int(lit_id or 0),
            "operate_status": status,
        })
    except Exception as exc:
        logger.error("操作日志写入失败：%s", exc, exc_info=True)


def get_base_dir(path_key: str) -> str:
    """从 system_config 读取存储基准目录，缺省/异常时回退默认目录。

    Args:
        path_key: constants.CFG_*_PATH 对应的键后缀
                  （literature/report/note/backup）。
    Returns:
        基准目录绝对路径。
    """
    config_key_map = {
        "literature": C.CFG_LITERATURE_PATH,
        "report": C.CFG_REPORT_PATH,
        "note": C.CFG_NOTE_PATH,
        "backup": C.CFG_BACKUP_PATH,
    }
    fallback = file_helper.get_default_base_dir(path_key)
    try:
        value = SystemConfigDao().get(config_key_map[path_key])
        return value or fallback
    except Exception as exc:
        logger.warning("读取存储路径配置失败，使用默认目录：%s", exc)
        return fallback


def exception_to_code(exc: Exception) -> int:
    """把自定义业务异常映射为统一返回码。

    Args:
        exc: 捕获到的异常。
    Returns:
        constants.CODE_* 错误码。
    """
    if isinstance(exc, LiteratureAgentError):
        return exc.error_code
    return C.CODE_ERROR
