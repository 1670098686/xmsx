"""业务层公共辅助：操作日志写入、基准路径读取、异常→返回码转换。"""
from config import constants as C
from data_layer.dao.config_dao import SystemConfigDao
from data_layer.dao.log_dao import OperationLogDao
from tool_layer import file_helper
from utils.exceptions import LiteratureAgentError
from utils.logger import get_logger

logger = get_logger()


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
