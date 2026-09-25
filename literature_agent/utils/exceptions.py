"""自定义业务异常体系。

分层处理约定：
- tool_layer 捕获底层 IO/解析异常，包装为本模块异常向上抛出；
- business 层捕获业务异常，生成错误信息并记录日志；
- UI 层只接收 (code, msg)，绝不向用户展示原始堆栈。
"""


class LiteratureAgentError(Exception):
    """系统业务异常根类。

    Attributes:
        message: 面向用户的错误描述（禁止暴露原始堆栈）。
        error_code: 对应 config.constants.CODE_* 的错误码。
    """

    def __init__(self, message: str = "", error_code: int = 1):
        """初始化异常实例，保存错误信息。"""
        self.message = message or self.__class__.__name__
        self.error_code = error_code
        super().__init__(self.message)


class FileInvalidError(LiteratureAgentError):
    """文件格式不支持、文件损坏或内容为空。"""

    def __init__(self, message: str = "文件无效或格式不支持"):
        """初始化异常实例，保存错误信息。"""
        super().__init__(message, error_code=3)


class LiteratureFileNotFoundError(LiteratureAgentError):
    """目标文件不存在（避免与内置 FileNotFoundError 混淆）。"""

    def __init__(self, message: str = "文件不存在"):
        """初始化异常实例，保存错误信息。"""
        super().__init__(message, error_code=2)


class DuplicateFileError(LiteratureAgentError):
    """文件重复（hash/文件名+大小命中已有文献）。"""

    def __init__(self, message: str = "文献已存在"):
        """初始化异常实例，保存错误信息。"""
        super().__init__(message, error_code=7)


class StorageNotEnoughError(LiteratureAgentError):
    """磁盘存储空间不足。"""

    def __init__(self, message: str = "存储空间不足"):
        """初始化异常实例，保存错误信息。"""
        super().__init__(message, error_code=6)


class ParseError(LiteratureAgentError):
    """文献解析失败（加密、乱码、超时等）。"""

    def __init__(self, message: str = "文献解析失败"):
        """初始化异常实例，保存错误信息。"""
        super().__init__(message, error_code=4)


class DatabaseError(LiteratureAgentError):
    """数据库操作异常。"""

    def __init__(self, message: str = "数据库操作失败"):
        """初始化异常实例，保存错误信息。"""
        super().__init__(message, error_code=5)


class PermissionDeniedError(LiteratureAgentError):
    """路径越权或文件无读写权限。"""

    def __init__(self, message: str = "无操作权限"):
        """初始化异常实例，保存错误信息。"""
        super().__init__(message, error_code=8)


class AIServiceError(LiteratureAgentError):
    """AI 模型服务调用失败（网络异常、鉴权失败、响应非法等）。"""

    def __init__(self, message: str = "AI 模型服务调用失败"):
        """初始化异常实例，保存错误信息。"""
        super().__init__(message, error_code=9)
