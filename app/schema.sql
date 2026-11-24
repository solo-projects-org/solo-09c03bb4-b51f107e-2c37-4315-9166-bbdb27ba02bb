-- 水质采样独立复核管理系统 —— SQLite 表结构
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 应复核采样单名单
-- pending：是否待补（无生效配对即 1），物化为列以支持键集分页与索引排序；
-- 由服务层在配对/解体的同一事务内维护，触发器同步全库统计。
CREATE TABLE IF NOT EXISTS forms (
    form_id        TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    pending        INTEGER NOT NULL DEFAULT 1 CHECK (pending IN (0, 1))
);
-- 总览排序：待补在前（pending DESC），组内按单号（form_id ASC）
CREATE INDEX IF NOT EXISTS idx_forms_pending ON forms(pending DESC, form_id ASC);

-- 质控批号
CREATE TABLE IF NOT EXISTS batches (
    batch_id       TEXT PRIMARY KEY,
    created_at     TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    invalidated_at TEXT
);

-- 续传报告（单号、报告号、原瓶号、实验室、质控批号）
CREATE TABLE IF NOT EXISTS reports (
    report_id      TEXT PRIMARY KEY,
    form_id        TEXT NOT NULL,
    bottle_no      TEXT NOT NULL,
    lab            TEXT NOT NULL,
    batch_id       TEXT NOT NULL,
    archived_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    valid          INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
    invalidated_at TEXT,
    FOREIGN KEY (form_id)  REFERENCES forms(form_id),
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);
CREATE INDEX IF NOT EXISTS idx_reports_form ON reports(form_id, valid);
CREATE INDEX IF NOT EXISTS idx_reports_batch ON reports(batch_id);

-- 采样单配对（采信的两份报告）；同一单最多一条生效配对（由部分唯一索引保证）
CREATE TABLE IF NOT EXISTS pairings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id          TEXT NOT NULL,
    report_a_id      TEXT NOT NULL,
    report_b_id      TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    dissolved_at     TEXT,
    dissolve_reason  TEXT,
    FOREIGN KEY (form_id)     REFERENCES forms(form_id),
    FOREIGN KEY (report_a_id) REFERENCES reports(report_id),
    FOREIGN KEY (report_b_id) REFERENCES reports(report_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_pairings_active
    ON pairings(form_id) WHERE dissolved_at IS NULL;

-- 作废事件（与名单一同生效，记录作废时刻）
CREATE TABLE IF NOT EXISTS batch_invalidations (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          TEXT NOT NULL UNIQUE,
    invalidated_at    TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    affected_forms    INTEGER NOT NULL DEFAULT 0,
    retained_pairings INTEGER NOT NULL DEFAULT 0,
    rebuilt_pairings  INTEGER NOT NULL DEFAULT 0,
    note              TEXT,
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);

-- 重核名单：作废该批涉及的采样单
CREATE TABLE IF NOT EXISTS recheck_entries (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    form_id          TEXT NOT NULL,
    batch_id         TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved')),
    FOREIGN KEY (form_id)  REFERENCES forms(form_id),
    FOREIGN KEY (batch_id) REFERENCES batches(batch_id)
);
CREATE INDEX IF NOT EXISTS idx_recheck_open ON recheck_entries(status, form_id);
CREATE INDEX IF NOT EXISTS idx_recheck_form ON recheck_entries(form_id, status);

-- 总览全库统计与翻页版本号（单行表）：
-- 四项统计由触发器随业务事务增量维护，页首读取为 O(1)，不逐单重算；
-- pagination_epoch 仅在“新单入册”或“待补集合发生净变化”时由服务层 +1，
-- 只换证、只改报告计数不推进，深页链接据此判断是否需要回首页。
CREATE TABLE IF NOT EXISTS overview_meta (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    total_forms      INTEGER NOT NULL DEFAULT 0,
    paired_forms     INTEGER NOT NULL DEFAULT 0,
    pending_forms    INTEGER NOT NULL DEFAULT 0,
    open_rechecks    INTEGER NOT NULL DEFAULT 0,
    pagination_epoch INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO overview_meta (id) VALUES (1);

-- ---- 统计触发器：随写入事务提交/回滚，保证与业务数据同一状态 ----
CREATE TRIGGER IF NOT EXISTS trg_forms_ai AFTER INSERT ON forms
BEGIN
    UPDATE overview_meta
       SET total_forms = total_forms + 1,
           pending_forms = pending_forms + NEW.pending
     WHERE id = 1;
END;

CREATE TRIGGER IF NOT EXISTS trg_forms_au AFTER UPDATE OF pending ON forms
BEGIN
    UPDATE overview_meta
       SET pending_forms = pending_forms
           + CASE WHEN OLD.pending = 1 AND NEW.pending = 0 THEN -1
                  WHEN OLD.pending = 0 AND NEW.pending = 1 THEN 1
                  ELSE 0 END
     WHERE id = 1;
END;

CREATE TRIGGER IF NOT EXISTS trg_pairings_ai AFTER INSERT ON pairings
BEGIN
    UPDATE overview_meta
       SET paired_forms = paired_forms + 1
     WHERE id = 1 AND NEW.dissolved_at IS NULL;
END;

CREATE TRIGGER IF NOT EXISTS trg_pairings_au AFTER UPDATE ON pairings
BEGIN
    UPDATE overview_meta
       SET paired_forms = paired_forms
           + CASE WHEN OLD.dissolved_at IS NULL AND NEW.dissolved_at IS NOT NULL THEN -1
                  WHEN OLD.dissolved_at IS NOT NULL AND NEW.dissolved_at IS NULL THEN 1
                  ELSE 0 END
     WHERE id = 1;
END;

CREATE TRIGGER IF NOT EXISTS trg_recheck_ai AFTER INSERT ON recheck_entries
BEGIN
    UPDATE overview_meta
       SET open_rechecks = open_rechecks + 1
     WHERE id = 1 AND NEW.status = 'open';
END;

CREATE TRIGGER IF NOT EXISTS trg_recheck_au AFTER UPDATE ON recheck_entries
BEGIN
    UPDATE overview_meta
       SET open_rechecks = open_rechecks
           + CASE WHEN OLD.status = 'open' AND NEW.status <> 'open' THEN -1
                  WHEN OLD.status <> 'open' AND NEW.status = 'open' THEN 1
                  ELSE 0 END
     WHERE id = 1;
END;
