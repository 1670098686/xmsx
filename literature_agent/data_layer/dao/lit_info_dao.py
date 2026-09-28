"""literature_info 文献信息表 DAO。"""
from data_layer.dao import BaseDao


class LiteratureInfoDao(BaseDao):
    """文献基础信息表 CRUD（仅原子操作，不含业务判断）。"""

    def insert(self, lit_info: dict) -> int:
        """新增一条文献记录。

        Args:
            lit_info: 文献字段字典。
        Returns:
            新记录主键 id。
        """
        cursor = self.conn.execute(
            """
            INSERT INTO literature_info
                (literature_title, literature_author, publish_time, journal_source,
                 literature_type, file_path, file_size, file_hash, category_id, is_parsed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                lit_info.get("literature_title", ""),
                lit_info.get("literature_author", ""),
                lit_info.get("publish_time", ""),
                lit_info.get("journal_source", ""),
                lit_info.get("literature_type", ""),
                lit_info.get("file_path", ""),
                lit_info.get("file_size", 0),
                lit_info.get("file_hash", ""),
                lit_info.get("category_id", 0),
                lit_info.get("is_parsed", 0),
            ),
        )
        self.conn.commit()
        return cursor.lastrowid

    def delete_by_id(self, lit_id: int) -> int:
        """按主键删除文献（关联表/报告/笔记由外键级联删除）。"""
        cursor = self.conn.execute(
            "DELETE FROM literature_info WHERE id = ?", (lit_id,)
        )
        self.conn.commit()
        return cursor.rowcount

    def update_by_id(self, lit_id: int, fields: dict) -> int:
        """按主键动态更新字段。

        Args:
            lit_id: 文献 id。
            fields: 待更新字段字典（键必须为白名单列名）。
        Returns:
            受影响行数。
        """
        allowed = {
            "literature_title", "literature_author", "publish_time", "journal_source",
            "literature_type", "file_path", "file_size", "file_hash",
            "category_id", "is_parsed", "update_time",
        }
        sets, params = [], []
        for key, value in fields.items():
            if key in allowed:
                sets.append(f"{key} = ?")
                params.append(value)
        if not sets:
            return 0
        sets.append("update_time = CURRENT_TIMESTAMP")
        params.append(lit_id)
        cursor = self.conn.execute(
            f"UPDATE literature_info SET {', '.join(sets)} WHERE id = ?", tuple(params)
        )
        self.conn.commit()
        return cursor.rowcount

    def select_by_id(self, lit_id: int) -> dict:
        """按主键查询单篇文献。"""
        row = self.conn.execute(
            "SELECT * FROM literature_info WHERE id = ?", (lit_id,)
        ).fetchone()
        return self.row_to_dict(row)

    def select_all(self) -> list:
        """查询全部文献（按入库时间倒序）。"""
        rows = self.conn.execute(
            "SELECT * FROM literature_info ORDER BY create_time DESC, id DESC"
        ).fetchall()
        return self.rows_to_list(rows)

    def select_by_category(self, category_id: int) -> list:
        """按所属分类查询。"""
        rows = self.conn.execute(
            "SELECT * FROM literature_info WHERE category_id = ? ORDER BY create_time DESC",
            (category_id,),
        ).fetchall()
        return self.rows_to_list(rows)

    def select_by_parsed(self, is_parsed: int) -> list:
        """按解析状态查询。"""
        rows = self.conn.execute(
            "SELECT * FROM literature_info WHERE is_parsed = ? ORDER BY create_time DESC",
            (is_parsed,),
        ).fetchall()
        return self.rows_to_list(rows)

    def select_by_hash(self, file_hash: str) -> dict:
        """按文件 hash 精确查询（去重检查）。"""
        row = self.conn.execute(
            "SELECT * FROM literature_info WHERE file_hash = ?", (file_hash,)
        ).fetchone()
        return self.row_to_dict(row)

    def search_by_title(self, keyword: str) -> list:
        """按标题模糊检索（参数化 LIKE）。"""
        rows = self.conn.execute(
            "SELECT * FROM literature_info WHERE literature_title LIKE ? "
            "ORDER BY create_time DESC",
            (f"%{keyword}%",),
        ).fetchall()
        return self.rows_to_list(rows)

    def count_all(self) -> int:
        """统计文献总数。"""
        return self.conn.execute("SELECT COUNT(1) FROM literature_info").fetchone()[0]

    def delete_all(self) -> int:
        """清空全部文献（关联表/报告/笔记由外键级联删除）。

        Returns:
            删除的记录数。
        """
        count = self.count_all()
        self.conn.execute("DELETE FROM literature_info")
        self.conn.commit()
        return count

    # 排序字段白名单（禁止外部直接拼接列名，防止 SQL 注入）
    _SORT_COLUMNS = {
        "create_time": "create_time",
        "literature_title": "literature_title",
        "publish_time": "publish_time",
        "file_size": "file_size",
    }

    def search(self, filters: dict = None) -> list:
        """按结构化条件组合检索文献（全部参数化，动态拼 WHERE）。

        Args:
            filters: 支持键：
                keyword(str)：标题/作者/来源/笔记/报告内容模糊匹配；
                tag_id(int)：绑定了指定标签；
                category_id(int)：所属分类；
                literature_type(str)：文件格式（PDF/TXT/DOCX/DOC）；
                publish_time(str)：年份，匹配发表时间前缀或入库年份；
                is_parsed_eq(int)：仅匹配该解析状态；
                is_parsed_neq(int)：排除该解析状态；
                create_time_from(str)：入库时间下界（含，UTC 定长时间串）；
                create_time_to(str)：入库时间上界（不含，UTC 定长时间串）；
                sort_by(str)：create_time/literature_title/publish_time/file_size；
                order(str)：desc/asc。
        Returns:
            文献字典列表。
        """
        filters = filters or {}
        where_parts = []
        params = []

        keyword = (filters.get("keyword") or "").strip()
        if keyword:
            like = f"%{keyword}%"
            where_parts.append(
                "(l.literature_title LIKE ? OR l.literature_author LIKE ? "
                "OR l.journal_source LIKE ?"
                # 标签名
                " OR EXISTS (SELECT 1 FROM literature_tag lt2 "
                "JOIN category_tag ct ON ct.id = lt2.tag_id "
                "WHERE lt2.literature_id = l.id AND ct.tag_name LIKE ?)"
                # 笔记内容
                " OR EXISTS (SELECT 1 FROM literature_note n "
                "WHERE n.literature_id = l.id AND n.note_content LIKE ?)"
                # 已解析报告（结构化后的正文核心段落）
                " OR EXISTS (SELECT 1 FROM literature_report r "
                "WHERE r.literature_id = l.id AND ("
                "r.research_background LIKE ? OR r.core_view LIKE ? "
                "OR r.research_method LIKE ? OR r.innovation_point LIKE ? "
                "OR r.research_conclusion LIKE ? OR r.reference_list LIKE ?)))"
            )
            params.extend([like, like, like, like, like,
                           like, like, like, like, like, like])

        tag_id = filters.get("tag_id")
        if tag_id:
            where_parts.append(
                "EXISTS (SELECT 1 FROM literature_tag lt "
                "WHERE lt.literature_id = l.id AND lt.tag_id = ?)"
            )
            params.append(int(tag_id))

        category_id = filters.get("category_id")
        if category_id:
            where_parts.append("l.category_id = ?")
            params.append(int(category_id))

        literature_type = (filters.get("literature_type") or "").strip()
        if literature_type:
            where_parts.append("l.literature_type = ?")
            params.append(literature_type)

        publish_time = (filters.get("publish_time") or "").strip()
        if publish_time:
            # 命中发表年份；未填写发表时间的文献才按入库年份归入对应桶
            where_parts.append(
                "(l.publish_time LIKE ? OR "
                "((l.publish_time IS NULL OR l.publish_time = '') "
                "AND strftime('%Y', l.create_time) = ?))"
            )
            params.extend([f"{publish_time}%", publish_time])

        parsed_eq = filters.get("is_parsed_eq")
        if parsed_eq is not None:
            where_parts.append("l.is_parsed = ?")
            params.append(int(parsed_eq))
        parsed_neq = filters.get("is_parsed_neq")
        if parsed_neq is not None:
            # 未开始/解析失败（解析中为瞬态）均归入“未解析”
            where_parts.append("l.is_parsed <> ?")
            params.append(int(parsed_neq))

        # 入库时间范围（UTC 定长 'YYYY-MM-DD HH:MM:SS' 字符串，可直接字典序比较）
        create_time_from = (filters.get("create_time_from") or "").strip()
        if create_time_from:
            where_parts.append("l.create_time >= ?")
            params.append(create_time_from)
        create_time_to = (filters.get("create_time_to") or "").strip()
        if create_time_to:
            where_parts.append("l.create_time < ?")
            params.append(create_time_to)

        where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
        sort_column = self._SORT_COLUMNS.get(
            filters.get("sort_by", "create_time"), "create_time"
        )
        order = "ASC" if str(filters.get("order", "desc")).lower() == "asc" else "DESC"
        sql = (
            f"SELECT l.* FROM literature_info l{where_sql} "
            f"ORDER BY l.{sort_column} {order}, l.id DESC"
        )
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return self.rows_to_list(rows)
