"""笔记批注业务：段落批注/全局笔记 CRUD + 1 秒防抖自动保存。

防抖在业务层用 threading.Timer 实现（而非 UI 层），子线程触发落库时
持有 DatabaseManager 写锁，与 QThread 批量任务互斥。

PDF 文献的区域批注复用 paragraph_pos 字段存锚点，格式见
encode_pdf_anchor / parse_pdf_anchor，不新增数据表字段。
"""
import threading

from config import constants as C
from data_layer.dao.lit_info_dao import LiteratureInfoDao
from data_layer.dao.note_dao import LiteratureNoteDao
from data_layer.db_connect import DatabaseManager
from utils.logger import get_logger

from business.service_common import exception_to_code, write_operation_log

logger = get_logger()


def encode_pdf_anchor(page_index: int, rect_points) -> str:
    """将 PDF 页码与矩形坐标编码为锚点字符串。

    Args:
        page_index: 页码序号（从 0 开始）。
        rect_points: (x0, y0, x1, y1)，PDF 点坐标（1/72 英寸，左上原点）。
    Returns:
        锚点字符串，如 "pdf:2:90,170,506,590"。
    Raises:
        ValueError: 页码或矩形非法。
    """
    try:
        page_index = int(page_index)
        x0, y0, x1, y1 = (round(float(v), 1) for v in rect_points)
    except (TypeError, ValueError) as exc:
        raise ValueError("PDF 批注锚点坐标非法") from exc
    if page_index < 0 or not (x0 < x1 and y0 < y1):
        raise ValueError("PDF 批注锚点页码或矩形非法")
    # 整坐标更短，锚点字段长度上限 50
    return (f"{C.NOTE_PDF_ANCHOR_PREFIX}{page_index}:"
            f"{int(round(x0))},{int(round(y0))},{int(round(x1))},{int(round(y1))}")


def parse_pdf_anchor(anchor: str):
    """解析 PDF 批注锚点。

    Args:
        anchor: paragraph_pos 中存储的锚点字符串。
    Returns:
        (page_index:int, (x0, y0, x1, y1):tuple[float])；非 PDF 锚点返回 None。
    """
    if not anchor or not str(anchor).startswith(C.NOTE_PDF_ANCHOR_PREFIX):
        return None
    body = str(anchor)[len(C.NOTE_PDF_ANCHOR_PREFIX):]
    try:
        page_text, rect_text = body.split(":", 1)
        page_index = int(page_text)
        coords = tuple(float(v) for v in rect_text.split(","))
    except (ValueError, TypeError):
        return None
    if page_index < 0 or len(coords) != 4 or coords[0] >= coords[2] or coords[1] >= coords[3]:
        return None
    return page_index, coords


def is_pdf_anchor(anchor: str) -> bool:
    """判断锚点是否为 PDF 区域批注。"""
    return parse_pdf_anchor(anchor) is not None


class NoteManageService:
    """笔记批注业务服务。"""

    def __init__(self):
        """初始化服务，组装所需 DAO 组件。"""
        self.note_dao = LiteratureNoteDao()
        self.lit_dao = LiteratureInfoDao()
        # note_id -> 待执行的防抖 Timer
        self._timers = {}
        # note_id -> 最后一次待保存字段（flush 时取此值立即落库）
        self._pending_fields = {}
        self._timers_lock = threading.Lock()
        self._debounce_seconds = C.NOTE_SAVE_DEBOUNCE_MS / 1000.0

    # ================= 新增 =================

    def add_paragraph_note(self, lit_id: int, paragraph_pos: str,
                           content: str, highlight_style: str = "") -> tuple:
        """添加段落批注。

        Args:
            lit_id: 文献 id。
            paragraph_pos: 段落锚点（段落序号字符串）。
            content: 批注内容。
            highlight_style: 高亮样式标识（如 "#E8F4F8" 或 style key）。
        Returns:
            (code, note_dict, msg)
        """
        try:
            if not self.lit_dao.select_by_id(lit_id):
                return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
            if content is None or not str(content).strip():
                return C.CODE_FILE_INVALID, None, "批注内容不能为空"
            note_id = self.note_dao.insert({
                "literature_id": lit_id,
                "paragraph_pos": str(paragraph_pos),
                "note_content": content,
                "note_type": C.NOTE_TYPE_PARAGRAPH,
                "highlight_style": highlight_style or "",
            })
            note = self.note_dao.get_by_id(note_id)
            write_operation_log(C.OP_NOTE, f"新增段落批注：文献 {lit_id}", lit_id)
            return C.CODE_SUCCESS, note, "已保存"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def add_pdf_annotation(self, lit_id: int, page_index: int,
                           rect_points, content: str,
                           highlight_style: str = "") -> tuple:
        """添加 PDF 区域批注（在 PDF 页面上框选矩形区域）。

        Args:
            lit_id: 文献 id。
            page_index: PDF 页码序号（从 0 开始）。
            rect_points: (x0, y0, x1, y1) PDF 点坐标。
            content: 批注内容。
            highlight_style: 预留高亮样式标识。
        Returns:
            (code, note_dict, msg)
        """
        try:
            if not self.lit_dao.select_by_id(lit_id):
                return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
            if content is None or not str(content).strip():
                return C.CODE_FILE_INVALID, None, "批注内容不能为空"
            anchor = encode_pdf_anchor(page_index, rect_points)
            note_id = self.note_dao.insert({
                "literature_id": lit_id,
                "paragraph_pos": anchor,
                "note_content": content,
                "note_type": C.NOTE_TYPE_PARAGRAPH,
                "highlight_style": highlight_style or "",
            })
            note = self.note_dao.get_by_id(note_id)
            write_operation_log(
                C.OP_NOTE, f"新增 PDF 区域批注：文献 {lit_id} 第 {page_index + 1} 页",
                lit_id,
            )
            return C.CODE_SUCCESS, note, "已保存"
        except ValueError as exc:
            return C.CODE_FILE_INVALID, None, str(exc)
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def add_global_note(self, lit_id: int, content: str) -> tuple:
        """添加或更新全局笔记（每篇文献仅一条）。

        Returns:
            (code, note_dict, msg)
        """
        try:
            if not self.lit_dao.select_by_id(lit_id):
                return C.CODE_FILE_NOT_FOUND, None, "文献不存在"
            existing = self._get_global_note(lit_id)
            if existing:
                self.note_dao.update_by_id(existing["id"], {"note_content": content})
                note = self.note_dao.get_by_id(existing["id"])
            else:
                note_id = self.note_dao.insert({
                    "literature_id": lit_id,
                    "paragraph_pos": "",
                    "note_content": content,
                    "note_type": C.NOTE_TYPE_GLOBAL,
                    "highlight_style": "",
                })
                note = self.note_dao.get_by_id(note_id)
            write_operation_log(C.OP_NOTE, f"保存全局笔记：文献 {lit_id}", lit_id)
            return C.CODE_SUCCESS, note, "已保存"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    # ================= 更新（防抖） =================

    def update_note(self, note_id: int, fields: dict) -> tuple:
        """更新笔记（1 秒防抖节流，连续编辑只落库最后一次）。

        Returns:
            (code, None, msg)
        """
        try:
            if not self.note_dao.get_by_id(note_id):
                return C.CODE_FILE_NOT_FOUND, None, "笔记不存在"
            clean_fields = {
                key: value for key, value in fields.items()
                if key in ("paragraph_pos", "note_content", "note_type",
                           "highlight_style")
            }
            if not clean_fields:
                return C.CODE_SUCCESS, None, "无需要保存的字段"
            self._schedule_save(note_id, clean_fields)
            return C.CODE_SUCCESS, None, "已加入自动保存"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def _schedule_save(self, note_id: int, fields: dict) -> None:
        """取消旧 Timer 并重新计时（连续编辑只落库最后一次字段）。"""
        with self._timers_lock:
            self._pending_fields[note_id] = fields
            old_timer = self._timers.pop(note_id, None)
            if old_timer is not None:
                old_timer.cancel()
            timer = threading.Timer(
                self._debounce_seconds, self._execute_save, args=(note_id,)
            )
            timer.daemon = True
            self._timers[note_id] = timer
            timer.start()

    def _execute_save(self, note_id: int) -> None:
        """Timer 回调：取最后一次待写字段持写锁落库，异常仅记日志。"""
        with self._timers_lock:
            self._timers.pop(note_id, None)
            fields = self._pending_fields.pop(note_id, None)
        if fields is None:
            return
        try:
            with DatabaseManager().write_lock:
                self.note_dao.update_by_id(note_id, fields)
            logger.info("笔记防抖落库完成：note_id=%s", note_id)
        except Exception as exc:
            logger.error("笔记防抖保存失败 note_id=%s：%s", note_id, exc, exc_info=True)

    def flush_note(self, note_id: int) -> None:
        """立即写入指定笔记的挂起更新（手动保存/切换文献时调用）。"""
        timer = self._cancel_timer(note_id)
        if timer is None:
            return
        fields = self._pending_fields.pop(note_id, None)
        if fields is not None:
            try:
                with DatabaseManager().write_lock:
                    self.note_dao.update_by_id(note_id, fields)
            except Exception as exc:
                logger.error("flush 笔记失败 note_id=%s：%s", note_id, exc)

    def flush_all(self) -> None:
        """立即落库全部挂起的防抖更新（程序退出/切换文献前调用）。"""
        with self._timers_lock:
            timers = dict(self._timers)
            self._timers.clear()
            pending = dict(self._pending_fields)
            self._pending_fields.clear()
        for timer in timers.values():
            timer.cancel()
        for note_id, fields in pending.items():
            try:
                with DatabaseManager().write_lock:
                    self.note_dao.update_by_id(note_id, fields)
            except Exception as exc:
                logger.error("flush 笔记失败 note_id=%s：%s", note_id, exc)

    # ================= 删除/清空 =================

    def delete_note(self, note_id: int) -> tuple:
        """删除单条笔记（高危，调用方需先二次确认）。

        Returns:
            (code, None, msg)
        """
        try:
            note = self.note_dao.get_by_id(note_id)
            if not note:
                return C.CODE_FILE_NOT_FOUND, None, "笔记不存在"
            self._cancel_timer(note_id)
            with DatabaseManager().write_lock:
                self.note_dao.delete_by_id(note_id)
            write_operation_log(
                C.OP_NOTE, f"删除笔记 {note_id}：文献 {note['literature_id']}",
                note["literature_id"],
            )
            return C.CODE_SUCCESS, None, "已删除"
        except Exception as exc:
            return exception_to_code(exc), None, getattr(exc, "message", str(exc))

    def clear_all_notes(self, lit_id: int) -> tuple:
        """清空某文献全部笔记（高危，业务层二次校验文献存在）。

        Returns:
            (code, deleted_count, msg)
        """
        try:
            if not self.lit_dao.select_by_id(lit_id):
                return C.CODE_FILE_NOT_FOUND, 0, "文献不存在"
            notes = self.note_dao.get_by_lit_id(lit_id)
            for note in notes:
                self._cancel_timer(note["id"])
            with DatabaseManager().write_lock:
                count = self.note_dao.delete_by_lit_id(lit_id)
            write_operation_log(C.OP_NOTE, f"清空文献 {lit_id} 全部笔记（{count} 条）",
                                lit_id)
            return C.CODE_SUCCESS, count, "已清空"
        except Exception as exc:
            return exception_to_code(exc), 0, getattr(exc, "message", str(exc))

    # ================= 查询 =================

    def get_notes_by_lit(self, lit_id: int) -> list:
        """获取文献全部笔记（段落批注在前，全局笔记在后）。"""
        return self.note_dao.get_by_lit_id(lit_id)

    def get_paragraph_notes(self, lit_id: int) -> list:
        """获取段落批注列表。"""
        return [
            note for note in self.note_dao.get_by_lit_id(lit_id)
            if note["note_type"] == C.NOTE_TYPE_PARAGRAPH
        ]

    def get_global_note(self, lit_id: int):
        """获取全局笔记（无则 None）。"""
        return self._get_global_note(lit_id)

    def _get_global_note(self, lit_id: int):
        """查询某文献的全局笔记记录（无则返回 None）。"""
        notes = self.note_dao.get_by_lit_id(lit_id)
        for note in notes:
            if note["note_type"] == C.NOTE_TYPE_GLOBAL:
                return note
        return None

    # ================= 防抖内部维护 =================

    def _cancel_timer(self, note_id: int):
        """取消某笔记挂起的防抖任务，返回被取消的 Timer（无则 None）。"""
        with self._timers_lock:
            return self._timers.pop(note_id, None)
