"""category_tag 分类标签表 DAO。"""
from data_layer.dao import BaseDao


class CategoryTagDao(BaseDao):
    """分类目录与个性化标签 CRUD（tag_type 区分类型）。"""

    def insert(self, tag: dict) -> int:
        """新增分类或标签。"""
        cursor = self.conn.execute(
            """
            INSERT INTO category_tag (tag_type, tag_name, tag_color, parent_id, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                tag.get("tag_type", 2),
                tag.get("tag_name", ""),
                tag.get("tag_color", "#000000"),
                tag.get("parent_id", 0),
                tag.get("sort_order", 0),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def get_by_id(self, tag_id: int) -> dict:
        """按主键查询。"""
        row = self.conn.execute(
            "SELECT * FROM category_tag WHERE id = ?", (tag_id,)
        ).fetchone()
        return self.row_to_dict(row)

    def update_by_id(self, tag_id: int, fields: dict) -> int:
        """按主键动态更新（名称/颜色/父级/排序）。"""
        allowed = {"tag_name", "tag_color", "parent_id", "sort_order"}
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return 0
        params.append(tag_id)
        cursor = self.conn.execute(
            f"UPDATE category_tag SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        self.conn.commit()
        return cursor.rowcount

    def delete_by_id(self, tag_id: int) -> int:
        """按主键删除（文献绑定关系由外键级联清理）。"""
        cursor = self.conn.execute(
            "DELETE FROM category_tag WHERE id = ?", (tag_id,)
        )
        self.conn.commit()
        return cursor.rowcount

    def get_all(self) -> list:
        """查询全部分类与标签（按类型、排序、id）。"""
        rows = self.conn.execute(
            "SELECT * FROM category_tag ORDER BY tag_type, sort_order, id"
        ).fetchall()
        return self.rows_to_list(rows)

    def get_all_categories(self) -> list:
        """查询全部分类目录（tag_type=1）。"""
        rows = self.conn.execute(
            "SELECT * FROM category_tag WHERE tag_type = 1 ORDER BY sort_order, id"
        ).fetchall()
        return self.rows_to_list(rows)

    def get_all_labels(self) -> list:
        """查询全部标签（tag_type=2）。"""
        rows = self.conn.execute(
            "SELECT * FROM category_tag WHERE tag_type = 2 ORDER BY sort_order, id"
        ).fetchall()
        return self.rows_to_list(rows)

    def get_children(self, parent_id: int) -> list:
        """查询指定父级下的直接子分类。"""
        rows = self.conn.execute(
            "SELECT * FROM category_tag WHERE parent_id = ? AND tag_type = 1 "
            "ORDER BY sort_order, id",
            (parent_id,),
        ).fetchall()
        return self.rows_to_list(rows)
