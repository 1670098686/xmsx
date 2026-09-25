"""literature_tag 文献-标签关联表 DAO（多对多）。"""
from data_layer.dao import BaseDao


class LiteratureTagDao(BaseDao):
    """文献与分类/标签绑定关系 CRUD。"""

    def bind(self, literature_id: int, tag_id: int) -> int:
        """绑定文献与标签（重复绑定由 UNIQUE 约束忽略）。"""
        cursor = self.conn.execute(
            "INSERT OR IGNORE INTO literature_tag (literature_id, tag_id) VALUES (?, ?)",
            (literature_id, tag_id),
        )
        self.conn.commit()
        return cursor.lastrowid

    def unbind(self, literature_id: int, tag_id: int) -> int:
        """解除单条绑定关系。"""
        cursor = self.conn.execute(
            "DELETE FROM literature_tag WHERE literature_id = ? AND tag_id = ?",
            (literature_id, tag_id),
        )
        self.conn.commit()
        return cursor.rowcount

    def unbind_all_by_lit(self, literature_id: int) -> int:
        """清除某篇文献的全部标签绑定。"""
        cursor = self.conn.execute(
            "DELETE FROM literature_tag WHERE literature_id = ?", (literature_id,)
        )
        self.conn.commit()
        return cursor.rowcount

    def get_tag_ids_by_lit(self, literature_id: int) -> list:
        """查询某篇文献绑定的全部标签 id。"""
        rows = self.conn.execute(
            "SELECT tag_id FROM literature_tag WHERE literature_id = ? ORDER BY id",
            (literature_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def get_lit_ids_by_tag(self, tag_id: int) -> list:
        """查询某标签下的全部文献 id。"""
        rows = self.conn.execute(
            "SELECT literature_id FROM literature_tag WHERE tag_id = ? ORDER BY id",
            (tag_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def count_by_tag(self, tag_id: int) -> int:
        """统计某标签绑定的文献数量。"""
        return self.conn.execute(
            "SELECT COUNT(1) FROM literature_tag WHERE tag_id = ?", (tag_id,)
        ).fetchone()[0]

    def replace_tags(self, literature_id: int, tag_ids: list) -> None:
        """事务内全量替换某文献的标签绑定（先删后插）。

        Args:
            literature_id: 文献 id。
            tag_ids: 新的标签 id 列表。
        """
        try:
            self.conn.execute(
                "DELETE FROM literature_tag WHERE literature_id = ?", (literature_id,)
            )
            self.conn.executemany(
                "INSERT OR IGNORE INTO literature_tag (literature_id, tag_id) VALUES (?, ?)",
                [(literature_id, tag_id) for tag_id in tag_ids],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
