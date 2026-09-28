"""耗时任务 QThread Worker：批量导入、文献解析、报告导出、备份恢复、路径迁移。

UI 线程只负责创建 Worker、连接信号、显示 LoadingMask；
业务调用全部在子线程执行，通过信号回传 UI（禁止在子线程操作 Widget）。
"""
import os
import threading

from PyQt5.QtCore import QThread, pyqtSignal

from business.export_backup import ExportBackupService
from business.literature_import import LiteratureImportService
from business.literature_parse import LiteratureParseService
from business.system_service import SystemService
from config import constants as C


def _batch_interval_ms() -> int:
    """按 CPU 核心数返回批量任务篇间让步毫秒数（低配机型更长）。"""
    cores = os.cpu_count() or 1
    if cores <= C.LOW_END_CPU_CORES:
        return C.BATCH_INTERVAL_MS_LOW_END
    return C.BATCH_INTERVAL_MS_NORMAL


class ImportWorker(QThread):
    """批量导入子线程。

    Signals:
        progress(int, str): 进度百分比、当前文件名。
        item_done(str, int, str): 文件路径、返回码 code、消息。
        finished_all(list, list, list): 成功/失败/重复结果列表。
        canceled(int): 用户主动停止时已处理完成的数量。
    """

    progress = pyqtSignal(int, str)
    item_done = pyqtSignal(str, int, str)
    finished_all = pyqtSignal(list, list, list)
    canceled = pyqtSignal(int)

    def __init__(self, file_paths: list, duplicate_policy: str = "skip",
                 parent=None):
        """
        Args:
            file_paths: 待导入文件绝对路径列表。
            duplicate_policy: skip=重复跳过；overwrite=重复覆盖。
        """
        super().__init__(parent)
        self.file_paths = list(file_paths)
        self.duplicate_policy = duplicate_policy
        self._service = LiteratureImportService()

    def run(self):
        """子线程执行入口：逐篇导入，支持 requestInterruption 打断。"""
        success, failed, duplicate = [], [], []
        total = len(self.file_paths)
        done_count = 0
        for index, path in enumerate(self.file_paths):
            if self.isInterruptionRequested():
                self.canceled.emit(done_count)
                break
            self.progress.emit(int(index / total * 100), path)
            code, data, msg = self._service.import_single(path, self.duplicate_policy)
            done_count += 1
            self.item_done.emit(path, code, msg)
            if code == 0:
                success.append(data)
            elif code == 7:
                duplicate.append({"path": path, "existing": (data or {}).get("existing")})
            else:
                failed.append({"path": path, "msg": msg})
            # 篇间让步，保证批量过程中 UI 仍可响应（低配机型间隔更长）
            QThread.msleep(_batch_interval_ms())
        else:
            self.progress.emit(100, "")
        self.finished_all.emit(success, failed, duplicate)


class ParseWorker(QThread):
    """文献解析子线程（单篇或批量）。

    Signals:
        progress(int, str): 进度百分比、阶段描述。
        report_ready(int, dict): 某篇解析完成，传出 lit_id 与报告字典。
        finished_one(int, int, str): 单篇结束：lit_id、code、msg。
        confirm_degradation(str): 原件通道失败，请求用户授权改用全文文本。
        finished_all(list, list): 全部结束：成功报告列表、失败列表。
        ai_notices(list): 本次解析中 AI 失败/降级的用户提示（去重后）。
    """

    progress = pyqtSignal(int, str)
    report_ready = pyqtSignal(int, dict)
    finished_one = pyqtSignal(int, int, str)
    confirm_degradation = pyqtSignal(str)
    finished_all = pyqtSignal(list, list)
    ai_notices = pyqtSignal(list)

    def __init__(self, lit_ids, rule_id: int = None, reparse: bool = False,
                 parent=None):
        """
        Args:
            lit_ids: 单个文献 id（int）或 id 列表。
            rule_id: 指定解析规则 id。
            reparse: True 时先清旧报告再解析。
        """
        super().__init__(parent)
        self.lit_ids = [lit_ids] if isinstance(lit_ids, int) else list(lit_ids)
        self.rule_id = rule_id
        self.reparse = reparse
        self._service = LiteratureParseService()
        # 降级授权的跨线程闸门：子线程阻塞等待 UI 线程答复
        self._confirm_event = threading.Event()
        self._confirm_answer = False
        # 用户选择"本批次全部生效"后缓存的批量决定
        self._batch_decision = None

    def _degradation_callback(self, error_message: str) -> bool:
        """子线程内请求降级授权（阻塞至 UI 线程答复）。

        Args:
            error_message: AI 返回的错误原文。
        Returns:
            True 允许改用全文文本通道；False 不允许，回退本地解析。
        """
        if self._batch_decision is not None:
            return self._batch_decision
        self._confirm_event.clear()
        self.confirm_degradation.emit(error_message)
        self._confirm_event.wait()
        return self._confirm_answer

    def provide_degradation_answer(self, allowed: bool,
                                   apply_to_batch: bool = False) -> None:
        """UI 线程回传降级授权结果并放行子线程。

        Args:
            allowed: 是否允许改用全文文本通道。
            apply_to_batch: True 时把该决定应用到本批次后续所有文献。
        """
        self._confirm_answer = bool(allowed)
        if apply_to_batch:
            self._batch_decision = bool(allowed)
        self._confirm_event.set()

    def run(self):
        """子线程解析入口，逐篇处理直至全部完成。"""
        success, failed = [], []
        notices = []
        total = len(self.lit_ids)
        for index, lit_id in enumerate(self.lit_ids):
            def progress_cb(percent, message, base=index):
                """适配单篇解析进度为批量整体进度并发出信号。"""
                overall = int(((base + percent / 100.0) / total) * 100)
                self.progress.emit(overall, f"[{base + 1}/{total}] {message}")

            parse_call = (
                self._service.reparse if self.reparse else self._service.parse_single
            )
            code, data, msg = parse_call(
                lit_id, self.rule_id, progress_cb,
                degradation_callback=self._degradation_callback,
            )
            self.finished_one.emit(lit_id, code, msg)
            if code == 0:
                success.append(data)
                self.report_ready.emit(lit_id, data)
                ai_error = (data or {}).get("ai_error")
                if ai_error and ai_error not in notices:
                    notices.append(ai_error)
            else:
                failed.append({"lit_id": lit_id, "msg": msg})
            # 篇间让步，保证批量解析时 UI 不卡顿（低配机型间隔更长）
            QThread.msleep(_batch_interval_ms())
        self.ai_notices.emit(notices)
        self.finished_all.emit(success, failed)


class AIProbeWorker(QThread):
    """AI 模型配置校验子线程（连通/输出/PDF 原件解析，均为网络耗时操作）。

    Signals:
        finished_all(int, object, str): validate_ai_config 的 code/result/msg。
    """

    finished_all = pyqtSignal(int, object, str)

    def __init__(self, model_name: str, base_url: str, api_key: str,
                 file_channel: str = C.AI_FILE_CHANNEL_AUTO, parent=None):
        """
        Args:
            model_name: 表单当前选择/输入的模型名称。
            base_url: 表单当前接口地址（未保存的草稿也可校验）。
            api_key: 表单当前明文密钥（为空时由业务层读已存密钥）。
            file_channel: 表单选择的原件通道（auto/file_id/file_data/none）。
        """
        super().__init__(parent)
        self.model_name = model_name
        self.base_url = base_url
        self.api_key = api_key
        self.file_channel = file_channel or C.AI_FILE_CHANNEL_AUTO
        self._service = SystemService()

    def run(self):
        """子线程执行配置校验并回传结果。"""
        code, result, msg = self._service.validate_ai_config(
            self.model_name, self.base_url, self.api_key, self.file_channel
        )
        self.finished_all.emit(code, result, msg)


class ExportWorker(QThread):
    """批量导出解析报告子线程（支持另存目录与保存到资料库两种模式）。

    Signals:
        progress(int, str): 进度百分比、阶段描述。
        finished_all(dict): {"success": [...], "failed": [...]}。
    """

    progress = pyqtSignal(int, str)
    finished_all = pyqtSignal(dict)

    def __init__(self, lit_ids: list, fmt: str, output_dir: str = "",
                 to_library: bool = False, report_map: dict = None,
                 parent=None):
        """
        Args:
            lit_ids: 文献 id 列表。
            fmt: word/txt/pdf。
            output_dir: 另存模式的导出目录（保存到资料库时忽略）。
            to_library: True 时保存到项目资料库报告目录（时间戳历史版本）。
            report_map: {lit_id: report_dict}，保留解析当次的关键词附加信息。
        """
        super().__init__(parent)
        self.lit_ids = lit_ids
        self.fmt = fmt
        self.output_dir = output_dir
        self.to_library = to_library
        self.report_map = report_map or {}
        self._service = ExportBackupService()

    def run(self):
        """子线程执行批量导出/资料库保存。"""
        if self.to_library:
            result = self._service.save_batch_to_library(
                self.lit_ids, self.fmt, self.report_map,
                progress_callback=lambda p, m: self.progress.emit(p, m),
            )
        else:
            result = self._service.export_batch(
                self.lit_ids, self.fmt, self.output_dir
            )
        self.finished_all.emit(result)


class BackupWorker(QThread):
    """备份/恢复子线程（全量备份、增量备份、恢复）。

    Signals:
        progress(int, str): 进度百分比、阶段描述。
        finished_all(int, str): 业务返回码 code 与提示消息。
    """

    progress = pyqtSignal(int, str)
    finished_all = pyqtSignal(int, str)

    MODE_FULL = "full"
    MODE_INCREMENTAL = "incremental"
    MODE_RESTORE = "restore"

    def __init__(self, mode: str, target_path: str = "", parent=None):
        """
        Args:
            mode: full / incremental / restore。
            target_path: 备份模式为输出 zip 路径（空走默认目录）；
                         恢复模式为待恢复 zip 路径。
        """
        super().__init__(parent)
        self.mode = mode
        self.target_path = target_path
        self._service = ExportBackupService()

    def run(self):
        """子线程执行备份或恢复，支持 requestInterruption 由业务侧进度回调检查。"""
        def progress_cb(percent, message):
            """子线程进度回调，桥接为 Qt 进度信号。"""
            if self.isInterruptionRequested():
                return
            self.progress.emit(percent, message)

        if self.mode == self.MODE_FULL:
            code, _data, msg = self._service.full_backup(
                self.target_path or None, progress_cb
            )
        elif self.mode == self.MODE_INCREMENTAL:
            code, _data, msg = self._service.incremental_backup(
                self.target_path or None, progress_cb
            )
        else:
            code, _data, msg = self._service.restore(self.target_path, progress_cb)
        self.finished_all.emit(code, msg)


class PathMigrateWorker(QThread):
    """存储路径修改 + 历史文件迁移子线程。

    Signals:
        progress(int, str): 迁移进度百分比、当前文件描述。
        finished_all(int, int, str): 返回码、迁移文件数、提示消息。
    """

    progress = pyqtSignal(int, str)
    finished_all = pyqtSignal(int, int, str)

    def __init__(self, path_key: str, new_path: str, parent=None):
        """
        Args:
            path_key: literature/report/note/backup。
            new_path: 新的存储绝对路径（自动迁移旧目录文件）。
        """
        super().__init__(parent)
        self.path_key = path_key
        self.new_path = new_path
        self._service = SystemService()

    def run(self):
        """子线程执行路径保存与迁移。"""
        def progress_cb(percent, message):
            """子线程进度回调，桥接为 Qt 进度信号。"""
            self.progress.emit(percent, message)

        code, data, msg = self._service.set_storage_path(
            self.path_key, self.new_path, migrate=True,
            progress_callback=progress_cb,
        )
        migrated = int(data["migrated"]) if code == 0 and data else 0
        self.finished_all.emit(code, migrated, msg)


class RenderPdfWorker(QThread):
    """文献原版式 PDF 准备子线程（Word 本地转 PDF 可能耗时数秒）。

    Signals:
        ready(int, int, object, str): lit_id、返回码 code、渲染信息 dict（失败为 None）、msg。
    """

    ready = pyqtSignal(int, int, object, str)

    def __init__(self, lit_id: int, parent=None):
        """
        Args:
            lit_id: 文献 id。
        """
        super().__init__(parent)
        self.lit_id = lit_id
        self._service = LiteratureParseService()

    def run(self):
        """子线程执行 PDF 直出/Word 转 PDF（含哈希缓存）。"""
        code, info, msg = self._service.prepare_render_pdf(self.lit_id)
        self.ready.emit(self.lit_id, code, info, msg)
