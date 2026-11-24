"""领域逻辑：名单导入、报告入档、配对、按批作废与重核。

规则（对应需求）：
- 每单须有两份“有效报告”，且原瓶号、实验室均不同才能配对采信；
- 已生效配对不轻易改动，失效（其中报告被作废）才找替代；
- 作废整批：同事务内失效报告、重核只涉及该批采样单、与名单一同生效；
- 作废失败则全部回滚（不作废）；成功后该批新报告一律拒收。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Iterable, Optional

# 总览每页最多 50 单（需求上限）
PAGE_SIZE = 50


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 待补状态与翻页版本
def refresh_pending(conn: sqlite3.Connection, form_ids: Iterable[str]) -> bool:
    """按“是否存在生效配对”重算指定单的待补标记，在当前事务内更新。

    返回待补集合是否发生净变化（有单真正在 待补/已采信 之间切换）。
    """
    changed = False
    for form_id in dict.fromkeys(form_ids):  # 去重并保持顺序
        new_pending = 0 if active_pairing(conn, form_id) is not None else 1
        cur = conn.execute(
            "UPDATE forms SET pending = ? WHERE form_id = ? AND pending <> ?",
            (new_pending, form_id, new_pending),
        )
        if cur.rowcount > 0:
            changed = True
    return changed


def bump_pagination_epoch(conn: sqlite3.Connection) -> None:
    """推进翻页版本号：仅在新单入册或待补集合净变化时调用。"""
    conn.execute(
        "UPDATE overview_meta SET pagination_epoch = pagination_epoch + 1 WHERE id = 1"
    )


# ---------------------------------------------------------------- 基础查询
def valid_reports_for_form(conn: sqlite3.Connection, form_id: str) -> list[sqlite3.Row]:
    """该单当前仍有效的报告，按入档先后排序。"""
    return conn.execute(
        "SELECT * FROM reports WHERE form_id = ? AND valid = 1 "
        "ORDER BY archived_at, report_id",
        (form_id,),
    ).fetchall()


def active_pairing(conn: sqlite3.Connection, form_id: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM pairings WHERE form_id = ? AND dissolved_at IS NULL",
        (form_id,),
    ).fetchone()


def _compatible(a: sqlite3.Row, b: sqlite3.Row) -> bool:
    """两份报告可配对：原瓶号不同且实验室不同。"""
    return a["bottle_no"] != b["bottle_no"] and a["lab"] != b["lab"]


def find_pair(
    reports: list[sqlite3.Row],
    prefer: Optional[set[str]] = None,
) -> Optional[tuple[sqlite3.Row, sqlite3.Row]]:
    """从有效报告中挑出可采信的一对。

    优先保留 prefer 中的仍有效报告（作废后原配对幸存者优先），
    其余情况按入档先后取最早可配的一对，保证结果稳定。
    """
    prefer = prefer or set()
    # 1) 优先：幸存者 + 与其兼容的最早报告
    for survivor in reports:
        if survivor["report_id"] not in prefer:
            continue
        for other in reports:
            if other["report_id"] == survivor["report_id"]:
                continue
            if _compatible(survivor, other):
                return survivor, other
    # 2) 常规：最早可配对组合
    for i, a in enumerate(reports):
        for b in reports[i + 1:]:
            if _compatible(a, b):
                return a, b
    return None


def attempt_pair(
    conn: sqlite3.Connection,
    form_id: str,
    prefer: Optional[set[str]] = None,
) -> Optional[sqlite3.Row]:
    """为采样单尝试建立/恢复生效配对；已有生效配对则原样保留。

    成功建立后，该单所有 open 状态重核记录一并核销。
    """
    existing = active_pairing(conn, form_id)
    if existing is not None:
        return existing
    pair = find_pair(valid_reports_for_form(conn, form_id), prefer)
    if pair is None:
        return None
    a, b = pair
    cur = conn.execute(
        "INSERT INTO pairings (form_id, report_a_id, report_b_id) VALUES (?, ?, ?)",
        (form_id, a["report_id"], b["report_id"]),
    )
    conn.execute(
        "UPDATE recheck_entries SET status = 'resolved', resolved_at = ? "
        "WHERE form_id = ? AND status = 'open'",
        (_now(), form_id),
    )
    return conn.execute(
        "SELECT * FROM pairings WHERE id = ?", (cur.lastrowid,)
    ).fetchone()


# ---------------------------------------------------------------- 名单导入
def import_forms(conn: sqlite3.Connection, raw: str) -> dict:
    """导入应复核单号（每行一个），返回新增/已存在/非法统计。"""
    seen: set[str] = set()
    cleaned: list[str] = []
    invalid: list[str] = []
    for line in raw.splitlines():
        token = line.strip()
        if not token:
            continue
        if any(ch.isspace() for ch in token):
            invalid.append(token)
            continue
        if token in seen:
            continue
        seen.add(token)
        cleaned.append(token)

    existing = {
        r["form_id"]
        for r in conn.execute("SELECT form_id FROM forms").fetchall()
    }
    added = [f for f in cleaned if f not in existing]
    conn.executemany(
        "INSERT INTO forms (form_id, pending) VALUES (?, 1)", [(f,) for f in added]
    )
    if added:
        # 新单入册：旧翻页位置必然失效，推进翻页版本号
        bump_pagination_epoch(conn)
    return {
        "added": added,
        "existing": [f for f in cleaned if f in existing],
        "invalid": invalid,
    }


# ---------------------------------------------------------------- 报告入档
REQUIRED_FIELDS = ("form_id", "report_id", "bottle_no", "lab", "batch_id")


def import_reports(conn: sqlite3.Connection, rows: Iterable[dict]) -> dict:
    """逐行校验并续传报告，成功入档后为相关单尝试配对。

    行级问题（缺字段、名单外单号、重复报告号、已作废批次）只拒收该行，
    不影响其他行；意外异常则整体回滚。
    """
    inserted: list[dict] = []
    rejected: list[dict] = []
    touched_forms: list[str] = []
    seen_report_ids: set[str] = set()

    for index, row in enumerate(rows, start=1):
        record = {k: (row.get(k) or "").strip() for k in REQUIRED_FIELDS}
        label = record.get("report_id") or f"第 {index} 行"

        missing = [k for k in REQUIRED_FIELDS if not record[k]]
        if missing:
            rejected.append({"row": index, "report_id": label,
                             "reason": f"缺少字段：{'、'.join(missing)}"})
            continue

        form = conn.execute(
            "SELECT 1 FROM forms WHERE form_id = ?", (record["form_id"],)
        ).fetchone()
        if form is None:
            rejected.append({"row": index, "report_id": label,
                             "reason": f"单号 {record['form_id']} 不在应复核名单中"})
            continue

        if record["report_id"] in seen_report_ids:
            rejected.append({"row": index, "report_id": label,
                             "reason": "报告号在本次文件内重复"})
            continue
        dup = conn.execute(
            "SELECT 1 FROM reports WHERE report_id = ?", (record["report_id"],)
        ).fetchone()
        if dup is not None:
            rejected.append({"row": index, "report_id": label,
                             "reason": "报告号已入档，重复续传被拒收"})
            continue

        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (record["batch_id"],)
        ).fetchone()
        if batch is not None and batch["invalidated_at"] is not None:
            rejected.append({"row": index, "report_id": label,
                             "reason": f"质控批 {record['batch_id']} 已作废，该批新报告拒收"})
            continue

        if batch is None:
            conn.execute(
                "INSERT INTO batches (batch_id) VALUES (?)", (record["batch_id"],)
            )
        conn.execute(
            "INSERT INTO reports (report_id, form_id, bottle_no, lab, batch_id) "
            "VALUES (:report_id, :form_id, :bottle_no, :lab, :batch_id)",
            record,
        )
        seen_report_ids.add(record["report_id"])
        inserted.append(record)
        if record["form_id"] not in touched_forms:
            touched_forms.append(record["form_id"])

    paired: list[str] = []
    pending: list[str] = []
    for form_id in touched_forms:
        pairing = attempt_pair(conn, form_id)
        (paired if pairing is not None else pending).append(form_id)

    # 待补标记随配对结果同事务更新；只有待补集合发生净变化才推进翻页版本。
    # 仅换报告号、只增删报告计数而未进出待补时，深页位置仍然有效，不推进。
    if refresh_pending(conn, touched_forms):
        bump_pagination_epoch(conn)

    conn.commit()
    return {
        "inserted": inserted,
        "rejected": rejected,
        "paired": paired,
        "pending": pending,
    }


# ---------------------------------------------------------------- 按批作废
def invalidate_batch(conn: sqlite3.Connection, batch_id: str, note: str = "") -> dict:
    """作废整批报告并重核该批涉及的采样单（单事务）。

    失败（批次不存在/已作废/无报告）抛 ValueError 并回滚，不产生任何作废。
    """
    try:
        batch = conn.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise ValueError(f"质控批 {batch_id} 不存在，无法作废（失败不作废）")
        if batch["invalidated_at"] is not None:
            raise ValueError(
                f"质控批 {batch_id} 已于 {batch['invalidated_at']} 作废；"
                "成功作废后该批新报告一律拒收"
            )

        batch_reports = conn.execute(
            "SELECT * FROM reports WHERE batch_id = ? AND valid = 1", (batch_id,)
        ).fetchall()
        if not batch_reports:
            raise ValueError(f"质控批 {batch_id} 没有有效报告，无可作废内容（失败不作废）")

        now = _now()
        affected_forms = sorted({r["form_id"] for r in batch_reports})
        batch_report_ids = {r["report_id"] for r in batch_reports}

        # 1) 批次与报告同时失效（名单与作废一同生效）
        conn.execute(
            "UPDATE batches SET invalidated_at = ? WHERE batch_id = ?",
            (now, batch_id),
        )
        conn.execute(
            "UPDATE reports SET valid = 0, invalidated_at = ? "
            "WHERE batch_id = ? AND valid = 1",
            (now, batch_id),
        )

        retained = rebuilt = 0
        for form_id in affected_forms:
            before = active_pairing(conn, form_id)
            if before is not None:
                members = {before["report_a_id"], before["report_b_id"]}
                hit = members & batch_report_ids
                if hit:
                    survivors = members - hit
                    conn.execute(
                        "UPDATE pairings SET dissolved_at = ?, dissolve_reason = ? "
                        "WHERE id = ?",
                        (now, f"质控批 {batch_id} 作废，报告 {'、'.join(sorted(hit))} 失效",
                         before["id"]),
                    )
                    # 仅当配对确实失效（有幸存者或整体失效）才找替代
                    new_pairing = attempt_pair(conn, form_id, prefer=survivors or None)
                    if new_pairing is not None:
                        rebuilt += 1
                else:
                    # 该单虽有本批报告，但生效配对的两份报告均不在该批，
                    # 原配对仍然有效 -> 保留，不重新配对
                    retained += 1

            # 2) 重核名单：只收录该批涉及的采样单
            conn.execute(
                "INSERT INTO recheck_entries (form_id, batch_id) VALUES (?, ?)",
                (form_id, batch_id),
            )
            # 配对仍有效/已用其他批报告重建 -> 立即核销；否则保持 open（待补）
            if active_pairing(conn, form_id) is not None:
                conn.execute(
                    "UPDATE recheck_entries SET status = 'resolved', resolved_at = ? "
                    "WHERE form_id = ? AND batch_id = ? AND status = 'open'",
                    (now, form_id, batch_id),
                )

        cur = conn.execute(
            "INSERT INTO batch_invalidations "
            "(batch_id, invalidated_at, affected_forms, retained_pairings, "
            "rebuilt_pairings, note) VALUES (?, ?, ?, ?, ?, ?)",
            (batch_id, now, len(affected_forms), retained, rebuilt, note or None),
        )
        # 作废导致配对解体/重建：同事务校准待补标记；待补集合净变化才推进翻页版本
        if refresh_pending(conn, affected_forms):
            bump_pagination_epoch(conn)
        conn.commit()
        invalidation = conn.execute(
            "SELECT * FROM batch_invalidations WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return {
            "invalidation": invalidation,
            "affected_forms": affected_forms,
            "retained": retained,
            "rebuilt": rebuilt,
        }
    except Exception:
        conn.rollback()
        raise


# ---------------------------------------------------------------- 页面查询
def overview_stats(conn: sqlite3.Connection) -> sqlite3.Row:
    """页首四项全库统计：直接读触发器维护的单行元数据，O(1)，不逐单重算。"""
    return conn.execute(
        "SELECT total_forms, paired_forms, pending_forms, open_rechecks, "
        "pagination_epoch FROM overview_meta WHERE id = 1"
    ).fetchone()


def _overview_select() -> str:
    return """
        SELECT f.form_id,
               f.created_at,
               f.pending,
               (SELECT COUNT(*) FROM reports r WHERE r.form_id = f.form_id) AS total_reports,
               (SELECT COUNT(*) FROM reports r WHERE r.form_id = f.form_id AND r.valid = 1) AS valid_reports,
               (SELECT COUNT(*) FROM recheck_entries e
                 WHERE e.form_id = f.form_id AND e.status = 'open') AS open_rechecks,
               p.id AS pairing_id,
               p.report_a_id, p.report_b_id, p.created_at AS paired_at
        FROM forms f
        LEFT JOIN pairings p ON p.form_id = f.form_id AND p.dissolved_at IS NULL
    """


def forms_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """每个采样单的入档/配对/重核状态（全量，顺序与总览一致：待补在前、组内单号）。"""
    return conn.execute(
        _overview_select() + " ORDER BY f.pending DESC, f.form_id ASC"
    ).fetchall()


def _cursor_where(forward: bool) -> str:
    """键集游标配位：排序为 (pending DESC, form_id ASC)。

    游标 (cp, cf) 之后（forward）的行：pending 更小，或 pending 相同而 form_id 更大；
    游标之前（backward）的行：pending 更大，或 pending 相同而 form_id 更小。
    三个 ? 依次绑定 (cp, cp, cf)；展开显式比较，兼容旧版 SQLite，不用行值语法。
    """
    if forward:
        return "((f.pending < ?) OR (f.pending = ? AND f.form_id > ?))"
    return "((f.pending > ?) OR (f.pending = ? AND f.form_id < ?))"


def overview_page(
    conn: sqlite3.Connection,
    direction: str = "next",
    cursor_pending: Optional[int] = None,
    cursor_form: Optional[str] = None,
    page_size: int = PAGE_SIZE,
) -> dict:
    """键集分页取一页明细，不使用 OFFSET：翻到深页也不扫过前面的单。

    - direction=next：取游标之后（首页 cursor 为空）；
    - direction=prev：取游标之前的最后一页，返回前翻转为正序。
    多取 1 条用于判断是否还有下一页，不额外发 COUNT 查询。
    """
    limit = page_size + 1
    sql = _overview_select()
    params: list = []

    if cursor_pending is not None and cursor_form is not None:
        forward = direction != "prev"
        sql += " WHERE " + _cursor_where(forward)
        # 三个 ? 的绑定顺序：pending 比较值、pending 等值、form_id 比较值
        params = [int(cursor_pending), int(cursor_pending), str(cursor_form)]
    else:
        forward = True

    if forward:
        sql += " ORDER BY f.pending DESC, f.form_id ASC LIMIT ?"
    else:
        sql += " ORDER BY f.pending ASC, f.form_id DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(sql, params).fetchall()
    has_more = len(rows) > page_size
    if has_more:
        rows = rows[:page_size]
    if not forward:
        rows = list(reversed(rows))
    return {"rows": rows, "has_more": has_more}


def form_detail(conn: sqlite3.Connection, form_id: str) -> Optional[dict]:
    form = conn.execute("SELECT * FROM forms WHERE form_id = ?", (form_id,)).fetchone()
    if form is None:
        return None
    reports = conn.execute(
        "SELECT * FROM reports WHERE form_id = ? ORDER BY archived_at, report_id",
        (form_id,),
    ).fetchall()
    pairings = conn.execute(
        "SELECT * FROM pairings WHERE form_id = ? ORDER BY id", (form_id,)
    ).fetchall()
    rechecks = conn.execute(
        "SELECT * FROM recheck_entries WHERE form_id = ? ORDER BY id", (form_id,)
    ).fetchall()
    return {"form": form, "reports": reports, "pairings": pairings, "rechecks": rechecks}


def batches_overview(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT b.batch_id, b.created_at, b.invalidated_at,
               (SELECT COUNT(*) FROM reports r WHERE r.batch_id = b.batch_id) AS total_reports,
               (SELECT COUNT(*) FROM reports r
                 WHERE r.batch_id = b.batch_id AND r.valid = 1) AS valid_reports,
               (SELECT COUNT(*) FROM recheck_entries e
                 WHERE e.batch_id = b.batch_id AND e.status = 'open') AS open_rechecks
        FROM batches b
        ORDER BY b.created_at, b.batch_id
        """
    ).fetchall()


def open_rechecks(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.*,
               (SELECT COUNT(*) FROM reports r
                 WHERE r.form_id = e.form_id AND r.valid = 1) AS valid_reports
        FROM recheck_entries e
        WHERE e.status = 'open'
        ORDER BY e.created_at, e.form_id
        """
    ).fetchall()
