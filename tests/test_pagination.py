"""总览分页、全库统计与翻页版本（pagination_epoch）测试。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import services  # noqa: E402


def rep(form, rid, bottle, lab, batch):
    return {"form_id": form, "report_id": rid, "bottle_no": bottle,
            "lab": lab, "batch_id": batch}


class PaginationTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": os.path.join(self.tmp.name, "page.db"),
        })
        self.ctx = self.app.app_context()
        self.ctx.push()
        from app.db import get_db
        self.conn = get_db()

    def tearDown(self):
        self.ctx.pop()
        self.tmp.cleanup()

    def seed_forms(self, n):
        ids = [f"F{i:05d}" for i in range(n)]
        services.import_forms(self.conn, "\n".join(ids))
        self.conn.commit()
        return ids


class OverviewPaginationTests(PaginationTestBase):
    def test_page_size_cap_and_ordering(self):
        ids = self.seed_forms(130)
        # 前 60 单配成（pending=0），其余 70 单待补（pending=1）
        for fid in ids[:60]:
            services.import_reports(self.conn, [
                rep(fid, fid + "-A", "B1", "LA", "BA"),
                rep(fid, fid + "-B", "B2", "LB", "BA"),
            ])
        full = services.forms_overview(self.conn)
        # 排序规则：待补在前，组内按单号
        pending_ids = sorted(r["form_id"] for r in full if r["pending"] == 1)
        paired_ids = sorted(r["form_id"] for r in full if r["pending"] == 0)
        self.assertEqual([r["form_id"] for r in full], pending_ids + paired_ids)

        # 键集翻页：每页不超过 50，拼接后与全量顺序完全一致、无重复无遗漏
        seen, cp, cf, pages = [], None, None, 0
        while True:
            pg = services.overview_page(self.conn, "next", cp, cf)
            rows = pg["rows"]
            self.assertLessEqual(len(rows), services.PAGE_SIZE)
            seen.extend(r["form_id"] for r in rows)
            pages += 1
            if not pg["has_more"]:
                break
            cp, cf = rows[-1]["pending"], rows[-1]["form_id"]
        self.assertEqual(pages, 3)  # 70 -> 50 + 20，再到 60 -> 50 + 10
        self.assertEqual(len(seen), 130)
        self.assertEqual(len(set(seen)), 130)
        self.assertEqual(seen, pending_ids + paired_ids)

    def test_keyset_crosses_pending_group_boundary(self):
        ids = self.seed_forms(120)
        # 只配成最后 20 单 -> 待补 100 单（两页），第三页进入已采信组
        for fid in ids[100:]:
            services.import_reports(self.conn, [
                rep(fid, fid + "-A", "B1", "LA", "BA"),
                rep(fid, fid + "-B", "B2", "LB", "BA"),
            ])
        p1 = services.overview_page(self.conn)
        self.assertEqual(len(p1["rows"]), 50)
        self.assertTrue(all(r["pending"] == 1 for r in p1["rows"]))
        last1 = p1["rows"][-1]
        p2 = services.overview_page(self.conn, "next", last1["pending"], last1["form_id"])
        self.assertTrue(all(r["pending"] == 1 for r in p2["rows"]))
        last2 = p2["rows"][-1]
        # 第三页游标仍在待补组，但应能跨到 pending=0 的已采信组
        p3 = services.overview_page(self.conn, "next", last2["pending"], last2["form_id"])
        self.assertTrue(p3["rows"])
        self.assertTrue(all(r["pending"] == 0 for r in p3["rows"]))
        self.assertFalse(p3["has_more"])

    def test_backward_page_is_ascending_and_excludes_cursor(self):
        ids = self.seed_forms(80)
        for fid in ids[:30]:
            services.import_reports(self.conn, [
                rep(fid, fid + "-A", "B1", "LA", "BA"),
                rep(fid, fid + "-B", "B2", "LB", "BA"),
            ])
        full = services.forms_overview(self.conn)
        ordered = [r["form_id"] for r in full]
        anchor_idx = 60  # 落在已采信组，同时前面隔着待补组，验证跨组回翻
        anchor = full[anchor_idx]
        pg = services.overview_page(
            self.conn, "prev", anchor["pending"], anchor["form_id"]
        )
        ids_prev = [r["form_id"] for r in pg["rows"]]
        # 取锚点之前的最后 50 行（全局顺序），翻转后与前翻页一致
        expected = ordered[max(0, anchor_idx - 50):anchor_idx]
        self.assertEqual(ids_prev, expected)
        self.assertEqual(len(ids_prev), 50)
        # 游标本身不重复出现，且本页全部严格位于锚点之前
        self.assertNotIn(anchor["form_id"], ids_prev)
        self.assertTrue(
            all(ordered.index(x) < anchor_idx for x in ids_prev)
        )

    def test_no_offset_in_pagination_sql(self):
        # 深页查询计划必须走复合索引（而非全表扫描），且不需要排序临时表
        self.seed_forms(60)
        sql = services._overview_select() + " WHERE " + services._cursor_where(True) + \
            " ORDER BY f.pending DESC, f.form_id ASC LIMIT ?"
        plan_lines = [
            r["detail"] for r in self.conn.execute(
                "EXPLAIN QUERY PLAN " + sql, [1, 1, "F00010", 51]
            ).fetchall()
        ]
        plan = "\n".join(plan_lines)
        forms_lines = [d for d in plan_lines if d.startswith(("SCAN f", "SEARCH f"))]
        self.assertTrue(forms_lines)
        self.assertIn("idx_forms_pending", forms_lines[0])
        self.assertNotIn("TEMP B-TREE", plan.upper())
        self.assertNotIn("USE TEMP B-TREE", plan.upper())


class OverviewStatsTests(PaginationTestBase):
    def test_stats_come_from_meta_and_match_rows(self):
        ids = self.seed_forms(10)
        for fid in ids[:4]:
            services.import_reports(self.conn, [
                rep(fid, fid + "-A", "B1", "LA", "BA"),
                rep(fid, fid + "-B", "B2", "LB", "BA"),
            ])
        m = services.overview_stats(self.conn)
        self.assertEqual(m["total_forms"], 10)
        self.assertEqual(m["paired_forms"], 4)
        self.assertEqual(m["pending_forms"], 6)
        self.assertEqual(m["open_rechecks"], 0)

        services.invalidate_batch(self.conn, "BA")
        m = services.overview_stats(self.conn)
        self.assertEqual(m["paired_forms"], 0)
        self.assertEqual(m["pending_forms"], 10)
        self.assertEqual(m["open_rechecks"], 4)

    def test_stats_o1_no_per_row_scan(self):
        # 页首统计只查单行元数据，不含针对 forms 的逐单聚合
        sql = ("SELECT total_forms, paired_forms, pending_forms, open_rechecks, "
               "pagination_epoch FROM overview_meta WHERE id = 1")
        plan = "\n".join(
            r["detail"] for r in self.conn.execute(
                "EXPLAIN QUERY PLAN " + sql
            ).fetchall()
        )
        self.assertIn("overview_meta", plan)
        self.assertNotIn("SCAN forms", plan)


class PaginationEpochTests(PaginationTestBase):
    def _epoch(self):
        return services.overview_stats(self.conn)["pagination_epoch"]

    def test_import_bumps_epoch(self):
        before = self._epoch()
        services.import_forms(self.conn, "F-1\nF-2")
        self.conn.commit()
        self.assertEqual(self._epoch(), before + 1)

    def test_enter_and_exit_pending_bump_epoch(self):
        services.import_forms(self.conn, "F-1")
        self.conn.commit()
        services.import_reports(self.conn, [
            rep("F-1", "R1", "B1", "LA", "BA"),
            rep("F-1", "R2", "B2", "LB", "BA"),
        ])
        after_pair = self._epoch()
        self.assertEqual(
            self.conn.execute("SELECT pending FROM forms WHERE form_id='F-1'").fetchone()["pending"],
            0,
        )
        # 作废且无替代 -> 进入待补
        services.invalidate_batch(self.conn, "BA")
        self.assertEqual(self._epoch(), after_pair + 1)
        self.assertEqual(
            self.conn.execute("SELECT pending FROM forms WHERE form_id='F-1'").fetchone()["pending"],
            1,
        )
        # 补齐 -> 退出待补
        before = self._epoch()
        services.import_reports(self.conn, [
            rep("F-1", "R3", "B3", "LC", "BC"),
            rep("F-1", "R4", "B4", "LD", "BC"),
        ])
        self.assertEqual(self._epoch(), before + 1)

    def test_count_only_and_swap_only_do_not_bump(self):
        services.import_forms(self.conn, "F-1\nF-2")
        self.conn.commit()
        services.import_reports(self.conn, [
            rep("F-1", "R1", "B1", "LA", "BA"),
            rep("F-1", "R2", "B2", "LB", "BA"),
        ])
        # F-2 仍是待补：只增加报告计数，不推进版本
        before = self._epoch()
        services.import_reports(self.conn, [rep("F-2", "R3", "B1", "LA", "BA")])
        self.assertEqual(self._epoch(), before)

        # F-1 已采信：再续传兼容报告只增计数、配对保留，不推进版本
        services.import_reports(self.conn, [
            rep("F-1", "R5", "B5", "LE", "BE"),
            rep("F-1", "R6", "B6", "LF", "BF"),
        ])
        self.assertEqual(self._epoch(), before)

    def test_failed_invalidation_keeps_epoch_and_stats(self):
        services.import_forms(self.conn, "F-1")
        self.conn.commit()
        before = self._epoch()
        with self.assertRaises(ValueError):
            services.invalidate_batch(self.conn, "GHOST")
        self.assertEqual(self._epoch(), before)
        m = services.overview_stats(self.conn)
        self.assertEqual(m["total_forms"], 1)
        self.assertEqual(m["pending_forms"], 1)

    def test_rejected_report_import_does_not_bump(self):
        services.import_forms(self.conn, "F-1")
        self.conn.commit()
        before = self._epoch()
        # 名单外、缺字段行全部拒收，无任何提交变化
        services.import_reports(self.conn, [
            rep("F-9", "RX", "B1", "LA", "BA"),
            {"form_id": "F-1", "report_id": "", "bottle_no": "B1",
             "lab": "LA", "batch_id": "BA"},
        ])
        self.assertEqual(self._epoch(), before)


class LegacyMigrationTests(unittest.TestCase):
    """旧库（无 pending 列、无 overview_meta）启动即升级。"""

    def _build_legacy_db(self, path):
        import sqlite3
        conn = sqlite3.connect(path)
        conn.executescript("""
        CREATE TABLE forms (form_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
        CREATE TABLE batches (batch_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), invalidated_at TEXT);
        CREATE TABLE reports (report_id TEXT PRIMARY KEY, form_id TEXT NOT NULL,
            bottle_no TEXT NOT NULL, lab TEXT NOT NULL, batch_id TEXT NOT NULL,
            archived_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            valid INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0,1)), invalidated_at TEXT);
        CREATE INDEX idx_reports_form ON reports(form_id, valid);
        CREATE TABLE pairings (id INTEGER PRIMARY KEY AUTOINCREMENT, form_id TEXT NOT NULL,
            report_a_id TEXT NOT NULL, report_b_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            dissolved_at TEXT, dissolve_reason TEXT);
        CREATE UNIQUE INDEX idx_pairings_active ON pairings(form_id) WHERE dissolved_at IS NULL;
        CREATE TABLE batch_invalidations (id INTEGER PRIMARY KEY AUTOINCREMENT,
            batch_id TEXT NOT NULL UNIQUE, invalidated_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            affected_forms INTEGER NOT NULL DEFAULT 0, retained_pairings INTEGER NOT NULL DEFAULT 0,
            rebuilt_pairings INTEGER NOT NULL DEFAULT 0, note TEXT);
        CREATE TABLE recheck_entries (id INTEGER PRIMARY KEY AUTOINCREMENT, form_id TEXT NOT NULL,
            batch_id TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            resolved_at TEXT, status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','resolved')));
        CREATE INDEX idx_recheck_open ON recheck_entries(status, form_id);
        """)
        conn.execute("INSERT INTO forms (form_id) VALUES ('F-1'),('F-2'),('F-3')")
        conn.execute("INSERT INTO batches (batch_id) VALUES ('BA')")
        conn.execute(
            "INSERT INTO reports (report_id,form_id,bottle_no,lab,batch_id) VALUES "
            "('R1','F-1','B1','LA','BA'),('R2','F-1','B2','LB','BA'),('R3','F-2','B3','LA','BA')"
        )
        conn.execute(
            "INSERT INTO pairings (form_id,report_a_id,report_b_id) VALUES ('F-1','R1','R2')"
        )
        conn.execute(
            "INSERT INTO recheck_entries (form_id,batch_id,status) VALUES ('F-3','BA','open')"
        )
        conn.commit()
        conn.close()

    def test_legacy_db_upgrades_and_backfills(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "legacy.db")
        self._build_legacy_db(path)

        app = create_app({"TESTING": True, "DB_PATH": path})
        with app.app_context():
            from app.db import get_db
            conn = get_db()
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(forms)")}
            self.assertIn("pending", cols)
            pending = dict(
                (r["form_id"], r["pending"])
                for r in conn.execute("SELECT form_id, pending FROM forms")
            )
            self.assertEqual(pending, {"F-1": 0, "F-2": 1, "F-3": 1})
            m = services.overview_stats(conn)
            self.assertEqual(
                (m["total_forms"], m["paired_forms"], m["pending_forms"], m["open_rechecks"]),
                (3, 1, 2, 1),
            )
            pg = services.overview_page(conn)
            self.assertEqual(
                [r["form_id"] for r in pg["rows"]], ["F-2", "F-3", "F-1"]
            )

        # 再次启动：迁移幂等，不报错、统计不变
        app2 = create_app({"TESTING": True, "DB_PATH": path})
        with app2.app_context():
            from app.db import get_db
            m = services.overview_stats(get_db())
            self.assertEqual(
                (m["total_forms"], m["paired_forms"], m["pending_forms"], m["open_rechecks"]),
                (3, 1, 2, 1),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
