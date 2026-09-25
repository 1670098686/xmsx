"""数据库初始化：首次启动自动建表、建索引并写入种子数据。

包含 9 张表（7 张原始表 + literature_tag 关联表 + backup_record 备份记录表）。
幂等设计：可重复执行，不会重复插入种子数据。
"""
import json
import os
import sqlite3

from config import constants as C
from data_layer.db_connect import DatabaseManager
from utils.logger import get_logger

SCHEMA_VERSION = 2

# ========== 建表 DDL（按外键依赖顺序排列）==========
_DDL_STATEMENTS = [
    # 1. 分类标签表（被 literature_info/literature_tag 关联，需先建）
    """
    CREATE TABLE IF NOT EXISTS category_tag (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        tag_type     TINYINT      NOT NULL DEFAULT 2,
        tag_name     VARCHAR(50)  NOT NULL,
        tag_color    VARCHAR(20)  NOT NULL DEFAULT '#000000',
        parent_id    INTEGER      NOT NULL DEFAULT 0,
        sort_order   INTEGER      NOT NULL DEFAULT 0,
        create_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # 2. 解析规则配置表（被 literature_report 关联，需先建）
    """
    CREATE TABLE IF NOT EXISTS parse_rule (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_name       VARCHAR(50)  NOT NULL,
        subject_type    VARCHAR(20)  NOT NULL DEFAULT '通用',
        rule_detail     TEXT         NOT NULL DEFAULT '{}',
        precision_level TINYINT      NOT NULL DEFAULT 3,
        is_default      TINYINT      NOT NULL DEFAULT 0,
        create_time     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # 3. 文献信息表（file_path 存相对路径，运行时按 system_config 基准路径还原）
    """
    CREATE TABLE IF NOT EXISTS literature_info (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        literature_title   VARCHAR(255) NOT NULL DEFAULT '',
        literature_author  VARCHAR(100) NOT NULL DEFAULT '',
        publish_time       VARCHAR(20)  NOT NULL DEFAULT '',
        journal_source     VARCHAR(255) NOT NULL DEFAULT '',
        literature_type    VARCHAR(20)  NOT NULL DEFAULT '',
        file_path          VARCHAR(500) NOT NULL,
        file_size          INTEGER      NOT NULL DEFAULT 0,
        file_hash          VARCHAR(64)  NOT NULL DEFAULT '',
        category_id        INTEGER      NOT NULL DEFAULT 0,
        is_parsed          TINYINT      NOT NULL DEFAULT 0,
        create_time        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        update_time        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # 4. 文献-标签多对多关联表（替代原 tag_id 逗号分隔方案）
    """
    CREATE TABLE IF NOT EXISTS literature_tag (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        literature_id  INTEGER   NOT NULL,
        tag_id         INTEGER   NOT NULL,
        bind_time      DATETIME  NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (literature_id) REFERENCES literature_info(id) ON DELETE CASCADE,
        FOREIGN KEY (tag_id)        REFERENCES category_tag(id)    ON DELETE CASCADE,
        UNIQUE (literature_id, tag_id)
    )
    """,
    # 5. 文献解析报告表
    """
    CREATE TABLE IF NOT EXISTS literature_report (
        id                    INTEGER PRIMARY KEY AUTOINCREMENT,
        literature_id         INTEGER  NOT NULL,
        research_background   TEXT     NOT NULL DEFAULT '',
        core_view             TEXT     NOT NULL DEFAULT '',
        research_method       TEXT     NOT NULL DEFAULT '',
        innovation_point      TEXT     NOT NULL DEFAULT '',
        research_conclusion   TEXT     NOT NULL DEFAULT '',
        reference_list        TEXT     NOT NULL DEFAULT '',
        parse_time            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
        rule_id               INTEGER  DEFAULT 1,
        FOREIGN KEY (literature_id) REFERENCES literature_info(id) ON DELETE CASCADE,
        FOREIGN KEY (rule_id)       REFERENCES parse_rule(id)      ON DELETE SET NULL
    )
    """,
    # 6. 笔记批注表
    """
    CREATE TABLE IF NOT EXISTS literature_note (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        literature_id   INTEGER      NOT NULL,
        paragraph_pos   VARCHAR(50)  NOT NULL DEFAULT '',
        note_content    TEXT         NOT NULL DEFAULT '',
        note_type       VARCHAR(20)  NOT NULL DEFAULT 'paragraph',
        highlight_style VARCHAR(30)  NOT NULL DEFAULT '',
        create_time     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        update_time     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (literature_id) REFERENCES literature_info(id) ON DELETE CASCADE
    )
    """,
    # 7. 系统配置表（键值对，灵活扩展）
    """
    CREATE TABLE IF NOT EXISTS system_config (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        config_key   VARCHAR(100) NOT NULL UNIQUE,
        config_value TEXT         NOT NULL DEFAULT '',
        config_desc  VARCHAR(255) NOT NULL DEFAULT '',
        update_time  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    # 8. 备份记录表
    """
    CREATE TABLE IF NOT EXISTS backup_record (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        backup_name   VARCHAR(100) NOT NULL,
        backup_path   VARCHAR(500) NOT NULL,
        backup_size   INTEGER      NOT NULL DEFAULT 0,
        backup_type   VARCHAR(20)  NOT NULL DEFAULT 'full',
        content_scope VARCHAR(100) NOT NULL DEFAULT 'all',
        backup_time   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        backup_status TINYINT      NOT NULL DEFAULT 1
    )
    """,
    # 9. 操作日志表
    """
    CREATE TABLE IF NOT EXISTS operation_log (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        operate_type    VARCHAR(30)  NOT NULL,
        operate_content VARCHAR(500) NOT NULL DEFAULT '',
        literature_id   INTEGER      NOT NULL DEFAULT 0,
        operate_time    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
        operate_status  TINYINT      NOT NULL DEFAULT 1
    )
    """,
]

# ========== 索引 DDL ==========
_INDEX_STATEMENTS = [
    "CREATE INDEX IF NOT EXISTS idx_lit_title    ON literature_info(literature_title)",
    "CREATE INDEX IF NOT EXISTS idx_lit_author   ON literature_info(literature_author)",
    "CREATE INDEX IF NOT EXISTS idx_lit_category ON literature_info(category_id)",
    "CREATE INDEX IF NOT EXISTS idx_lit_type     ON literature_info(literature_type)",
    "CREATE INDEX IF NOT EXISTS idx_lit_parsed   ON literature_info(is_parsed)",
    "CREATE INDEX IF NOT EXISTS idx_lit_create   ON literature_info(create_time)",
    "CREATE INDEX IF NOT EXISTS idx_lit_hash     ON literature_info(file_hash)",
    "CREATE INDEX IF NOT EXISTS idx_lt_lit       ON literature_tag(literature_id)",
    "CREATE INDEX IF NOT EXISTS idx_lt_tag       ON literature_tag(tag_id)",
    "CREATE INDEX IF NOT EXISTS idx_report_lit   ON literature_report(literature_id)",
    "CREATE INDEX IF NOT EXISTS idx_note_lit     ON literature_note(literature_id)",
    "CREATE INDEX IF NOT EXISTS idx_tag_type     ON category_tag(tag_type)",
    "CREATE INDEX IF NOT EXISTS idx_tag_parent   ON category_tag(parent_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name_unique "
    "ON category_tag(tag_type, tag_name, parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_backup_time  ON backup_record(backup_time)",
    "CREATE INDEX IF NOT EXISTS idx_log_time     ON operation_log(operate_time)",
    "CREATE INDEX IF NOT EXISTS idx_log_type     ON operation_log(operate_type)",
    "CREATE INDEX IF NOT EXISTS idx_log_lit      ON operation_log(literature_id)",
]


def _default_rule_detail(weights=None):
    """构造规则明细 JSON（维度开关 + 权重 + 关键词数量）。"""
    return json.dumps({
        "dimensions": {
            "research_background": True,
            "core_view": True,
            "research_method": True,
            "innovation_point": True,
            "research_conclusion": True,
            "reference_list": True,
        },
        "weights": weights or {
            "research_background": 1.0,
            "core_view": 1.2,
            "research_method": 1.0,
            "innovation_point": 1.5,
            "research_conclusion": 1.5,
            "reference_list": 1.0,
        },
        "keyword_top_n": 20,
    }, ensure_ascii=False)


def _seed_parse_rules(conn: sqlite3.Connection) -> None:
    """写入默认解析规则（通用/文科/理科/工科），已存在则跳过。"""
    rules = [
        (1, "默认通用规则", "通用", _default_rule_detail(), 3, 1),
        (2, "文科通用规则", "文科", _default_rule_detail(), 3, 0),
        (3, "理科通用规则", "理科",
         _default_rule_detail({"research_background": 1.0, "core_view": 1.2,
                               "research_method": 1.5, "innovation_point": 1.3,
                               "research_conclusion": 1.5, "reference_list": 1.0}),
         4, 0),
        (4, "工科通用规则", "工科",
         _default_rule_detail({"research_background": 0.8, "core_view": 1.0,
                               "research_method": 1.5, "innovation_point": 1.5,
                               "research_conclusion": 1.3, "reference_list": 0.8}),
         4, 0),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO parse_rule "
        "(id, rule_name, subject_type, rule_detail, precision_level, is_default) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rules,
    )


def _seed_category_tags(conn: sqlite3.Connection) -> None:
    """写入学术常用预设分类与标签。"""
    tags = [
        # (tag_type, tag_name, tag_color, parent_id, sort_order)
        (C.TAG_TYPE_CATEGORY, "综述类",   "#E8F4F8", 0, 1),
        (C.TAG_TYPE_CATEGORY, "期刊论文", "#E8F4F8", 0, 2),
        (C.TAG_TYPE_CATEGORY, "学位论文", "#E8F4F8", 0, 3),
        (C.TAG_TYPE_LABEL, "高创新性", "#FAAD14", 0, 1),
        (C.TAG_TYPE_LABEL, "需精读",   "#52C41A", 0, 2),
        (C.TAG_TYPE_LABEL, "引用素材", "#4096bb", 0, 3),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO category_tag "
        "(tag_type, tag_name, tag_color, parent_id, sort_order) "
        "VALUES (?, ?, ?, ?, ?)",
        tags,
    )


def _seed_system_config(conn: sqlite3.Connection) -> None:
    """写入系统默认配置（键唯一，已存在则跳过）。"""
    base_dir = os.path.join(os.path.expanduser("~"), ".literature_agent")
    configs = [
        (C.CFG_LITERATURE_PATH, os.path.join(base_dir, "data", "literature"), "文献存储路径"),
        (C.CFG_REPORT_PATH, os.path.join(base_dir, "data", "report"), "解析报告存储路径"),
        (C.CFG_NOTE_PATH, os.path.join(base_dir, "data", "note"), "笔记存储路径"),
        (C.CFG_BACKUP_PATH, os.path.join(base_dir, "backup"), "备份文件存储路径"),
        (C.CFG_AUTO_SAVE_TIME, "30", "自动保存频率（秒）"),
        (C.CFG_BACKUP_CYCLE, "7", "自动备份周期（天）"),
        (C.CFG_EXPORT_DEFAULT_TYPE, "PDF", "默认导出格式"),
        (C.CFG_UI_STYLE, C.THEME_LIGHT_NAME, "界面样式（light/dark）"),
        (C.CFG_PARSE_DEFAULT_RULE, "1", "默认解析规则ID"),
        (C.CFG_PRECISION_DEFAULT, "3", "默认解析精度"),
        (C.CFG_AUTO_SAVE_DEBOUNCE, "1", "自动保存防抖（秒）"),
        (C.CFG_AI_MODELS, "[]", "AI模型配置（加密密钥的JSON数组）"),
    ]
    conn.executemany(
        "INSERT OR IGNORE INTO system_config (config_key, config_value, config_desc) "
        "VALUES (?, ?, ?)",
        configs,
    )


def _migrate_v1_to_v2_collision_tags(conn: sqlite3.Connection) -> tuple:
    """v1→v2：清理与文献分类同名的个性化标签。

    旧版预设模板含“综述类”等分类同名标签，导致同一文献在卡片上同时出现
    分类角标与同名标签。迁移策略：
    1. 标签绑定的文献若未归类（category_id=0），提升到同名分类；
       已归入其它分类的文献保留用户的显式选择，不覆盖；
    2. 删除冗余同名标签（literature_tag 绑定由外键级联清理）。

    Returns:
        (删除标签数, 提升分类的文献数)。
    """
    collisions = conn.execute(
        """
        SELECT t.id AS tag_id, t.tag_name AS tag_name, c.id AS category_id
        FROM category_tag t
        JOIN category_tag c
          ON c.tag_type = ? AND c.tag_name = t.tag_name
        WHERE t.tag_type = ?
        ORDER BY t.id, c.id
        """,
        (C.TAG_TYPE_CATEGORY, C.TAG_TYPE_LABEL),
    ).fetchall()
    removed, promoted = 0, 0
    # 同名分类可能存在于不同层级，按标签收敛，取 id 最小的同名分类作为归属
    collision_map = {}
    for row in collisions:
        collision_map.setdefault(
            row["tag_id"], (row["tag_name"], row["category_id"])
        )
    for tag_id, (tag_name, category_id) in collision_map.items():
        cursor = conn.execute(
            "UPDATE literature_info "
            "SET category_id = ?, update_time = CURRENT_TIMESTAMP "
            "WHERE category_id = 0 AND id IN ("
            "SELECT literature_id FROM literature_tag WHERE tag_id = ?)",
            (category_id, tag_id),
        )
        promoted += cursor.rowcount
        conn.execute("DELETE FROM category_tag WHERE id = ?", (tag_id,))
        removed += 1
        conn.execute(
            "INSERT INTO operation_log "
            "(operate_type, operate_content, literature_id, operate_status) "
            "VALUES (?, ?, 0, ?)",
            (
                C.OP_CONFIG,
                f"v2数据迁移：同名标签“{tag_name}”并入文献分类"
                f"（{cursor.rowcount} 篇未归类文献自动归入）",
                C.OP_STATUS_SUCCESS,
            ),
        )
    return removed, promoted


def init_db(db_manager: DatabaseManager = None) -> None:
    """初始化数据库：建表、建索引、写种子数据、记录 schema 版本。

    Args:
        db_manager: 可选的已初始化 DatabaseManager；不传则取全局单例。
    """
    manager = db_manager or DatabaseManager()
    conn = manager.get_conn()
    logger = get_logger()

    with manager.transaction():
        old_version = conn.execute("PRAGMA user_version").fetchone()[0]
        for ddl in _DDL_STATEMENTS:
            conn.execute(ddl)
        for index_sql in _INDEX_STATEMENTS:
            conn.execute(index_sql)

        _seed_parse_rules(conn)
        _seed_category_tags(conn)
        _seed_system_config(conn)

        # 版本迁移（旧库升级；全新库 old_version=0 且无冲突数据，迁移为空操作）
        if old_version < 2:
            removed, promoted = _migrate_v1_to_v2_collision_tags(conn)
            if removed:
                logger.info(
                    "v2 迁移完成：清理 %s 个分类同名标签，%s 篇文献自动归入对应分类",
                    removed, promoted,
                )

        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    logger.info("数据库初始化完成：9 张表、18 个索引、种子数据就绪（schema v%s）", SCHEMA_VERSION)


if __name__ == "__main__":
    init_db()
