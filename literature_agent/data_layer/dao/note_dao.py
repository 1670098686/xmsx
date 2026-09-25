"""literature_note 笔记批注表 DAO。"""
from data_layer.dao import BaseDao


class LiteratureNoteDao(BaseDao):
    """笔记与批注 CRUD。"""

    def insert(self, note: dict) -> int:
        """新增一条笔记/批注。"""
        cursor = self.conn.execute(
            """
            INSERT INTO literature_note
                (literature_id, paragraph_pos, note_content, note_type, highlight_style)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                note.get("literature_id"),
                note.get("paragraph_pos", ""),
                note.get("note_content", ""),
                note.get("note_type", "paragraph"),
                note.get("highlight_style", ""),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_by_id(self, note_id: int) -> dict:
        """按主键查询单条笔记。"""
        row = self.conn.execute(
            "SELECT * FROM literature_note WHERE id = ?", (note_id,)
        ).fetchone()
        return self.row_to_dict(row)

    def get_by_lit_id(self, literature_id: int) -> list:
        """查询某文献的全部笔记（先段落批注后全局笔记，按时间正序）。"""
        rows = self.conn.execute(
            "SELECT * FROM literature_note WHERE literature_id = ? "
            "ORDER BY note_type ASC, create_time ASC, id ASC",
            (literature_id,),
        ).fetchall()
        return self.rows_to_list(rows)

    def update_by_id(self, note_id: int, fields: dict) -> int:
        """按主键动态更新笔记字段。"""
        allowed = {"paragraph_pos", "note_content", "note_type", "highlight_style", "update_time"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed and key != "update_time":
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return 0
        sets.append("update_time = CURRENT_TIMESTAMP")
        params.append(note_id)
        cursor = self.conn.execute(
            f"UPDATE literature_note SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        self.conn.commit()
        return cursor.rowcount

    def delete_by_id(self, note_id: int) -> int:
        """按主键删除单条笔记。"""
        cursor = self.conn.execute(
            "DELETE FROM literature_note WHERE id = ?", (note_id,)
        )
        self.conn.commit()
        return cursor.rowcount

    def delete_by_lit_id(self, literature_id: int) -> int:
        """删除某文献的全部笔记。"""
        cursor = self.conn.execute(
            "DELETE FROM literature_note WHERE literature_id = ?", (literature_id,)
        )
        self.conn.commit()
        return cursor.rowcount
