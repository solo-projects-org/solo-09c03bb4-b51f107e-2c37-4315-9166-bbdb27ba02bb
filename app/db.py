"""SQLite 连接与初始化（含旧库在线升级）。"""
import os
import sqlite3

from flask import Flask, current_app, g

SCHEMA_PATH = os.path.join(os.path.dirname(__file__), "schema.sql")


def get_db() -> sqlite3.Connection:
    """请求级连接；row_factory 让结果可以按列名访问。"""
    if "db" not in g:
        conn = sqlite3.connect(
            current_app.config["DB_PATH"],
            detect_types=sqlite3.PARSE_DECLTYPES,
        )
        conn.row_factory = sqlite3.Row
        # 外键约束 + 串行写入，保证作废/入档事务的完整性
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        g.db = conn
    return g.db


def close_db(exc=None) -> None:
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall()}


def _is_legacy_db(conn: sqlite3.Connection) -> bool:
    """旧库：已有 forms 表，但缺少分页所需的 pending 物化列。"""
    has_forms = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='forms'"
    ).fetchone()
    if has_forms is None:
        return False
    return "pending" not in _table_columns(conn, "forms")


def init_db(app: Flask) -> None:
    """建表 / 升级（已存在则跳过或迁移），全程幂等，旧库可直接启动升级。"""
    conn = sqlite3.connect(app.config["DB_PATH"])
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")

        legacy = _is_legacy_db(conn)
        if legacy:
            # 先补列并按“是否存在生效配对”回填待补标记；
            # 此时新触发器尚未创建，回填不会污染统计初值。
            conn.execute(
                "ALTER TABLE forms ADD COLUMN pending "
                "INTEGER NOT NULL DEFAULT 1 CHECK (pending IN (0, 1))"
            )
            conn.execute(
                "UPDATE forms SET pending = 0 "
                "WHERE form_id IN ("
                "    SELECT form_id FROM pairings WHERE dissolved_at IS NULL)"
            )

        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            conn.executescript(f.read())

        if legacy:
            # 旧库已有业务数据：按当前状态一次性校准四项全库统计与索引
            conn.execute(
                """
                UPDATE overview_meta SET
                    total_forms   = (SELECT COUNT(*) FROM forms),
                    paired_forms  = (SELECT COUNT(*) FROM pairings
                                      WHERE dissolved_at IS NULL),
                    pending_forms = (SELECT COUNT(*) FROM forms WHERE pending = 1),
                    open_rechecks = (SELECT COUNT(*) FROM recheck_entries
                                      WHERE status = 'open')
                WHERE id = 1
                """
            )
        conn.commit()
    finally:
        conn.close()


def init_app(app: Flask) -> None:
    app.teardown_appcontext(close_db)
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)
    init_db(app)
