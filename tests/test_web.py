"""网页端到端测试：名单导入、CSV 上传、配对展示、整批作废、总览分页。"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import services  # noqa: E402

CSV_HEADER = "单号,报告号,原瓶号,实验室,质控批号"


def rep(form, rid, bottle, lab, batch):
    return {"form_id": form, "report_id": rid, "bottle_no": bottle,
            "lab": lab, "batch_id": batch}


class WebFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": os.path.join(self.tmp.name, "web.db"),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow_via_http(self):
        # 1) 导入名单（含一个非法条目）
        resp = self.client.post("/forms/import", data={
            "form_ids": "F-1\nF-2\nBAD ITEM\n"
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("F-1", resp.text)
        self.assertIn("非法 1 条", resp.text)

        # 2) 续传报告：F-1 可配对；F-2 只有一份 -> 待补；名单外行拒收
        csv_text = "\n".join([
            CSV_HEADER,
            "F-1,R1,B1,LAB-A,BAT-1",
            "F-1,R2,B2,LAB-B,BAT-1",
            "F-2,R3,B3,LAB-A,BAT-1",
            "F-9,R9,B9,LAB-Z,BAT-1",
        ])
        resp = self.client.post(
            "/reports/upload",
            data={"csv_file": (io.BytesIO(csv_text.encode("utf-8-sig")), "r.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("入档 3 份", resp.text)
        self.assertIn("拒收 1 份", resp.text)

        # 首页列出采信报告号，F-2 标待补
        resp = self.client.get("/")
        self.assertIn("R1", resp.text)
        self.assertIn("R2", resp.text)
        self.assertIn("待补", resp.text)

        # 详情页
        resp = self.client.get("/forms/F-1")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("生效中（采信）", resp.text)

        # 3) 作废 BAT-1：F-1/F-2 全部进入重核待补
        resp = self.client.post("/batches", data={"batch_id": "BAT-1", "note": "演练"},
                                follow_redirects=True)
        self.assertIn("已作废", resp.text)
        self.assertIn("F-1", resp.text)
        self.assertIn("F-2", resp.text)

        resp = self.client.get("/rechecks")
        self.assertIn("F-1", resp.text)
        self.assertIn("F-2", resp.text)

        # 4) 作废后该批新报告被拒；其他批报告补齐 F-1 后自动核销
        csv_text2 = "\n".join([
            CSV_HEADER,
            "F-1,R4,B4,LAB-A,BAT-1",   # 已作废批 -> 拒收
            "F-1,R5,B5,LAB-A,BAT-2",
            "F-1,R6,B6,LAB-B,BAT-2",
        ])
        resp = self.client.post(
            "/reports/upload",
            data={"csv_text": csv_text2},
            follow_redirects=True,
        )
        self.assertIn("入档 2 份", resp.text)
        self.assertIn("拒收 1 份", resp.text)

        resp = self.client.get("/rechecks")
        self.assertNotIn("F-1", resp.text)   # 已核销
        self.assertIn("F-2", resp.text)      # 仍待补

        # 5) 重复作废 -> 失败提示且不改动
        resp = self.client.post("/batches", data={"batch_id": "BAT-1"},
                                follow_redirects=True)
        self.assertIn("作废失败", resp.text)

    def test_invalid_batch_failure_changes_nothing(self):
        self.client.post("/forms/import", data={"form_ids": "F-1"})
        resp = self.client.post("/batches", data={"batch_id": "GHOST"},
                                follow_redirects=True)
        self.assertIn("作废失败", resp.text)
        resp = self.client.get("/batches")
        self.assertNotIn("GHOST", resp.text)


class OverviewPagingWebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "DB_PATH": os.path.join(self.tmp.name, "webpage.db"),
        })
        self.client = self.app.test_client()
        self.ctx = self.app.app_context()
        self.ctx.push()
        from app.db import get_db
        self.conn = get_db()

    def tearDown(self):
        self.ctx.pop()
        self.tmp.cleanup()

    def _seed(self, n):
        ids = [f"F{i:05d}" for i in range(n)]
        services.import_forms(self.conn, "\n".join(ids))
        self.conn.commit()
        return ids

    def test_first_page_caps_at_50_and_keeps_columns_and_stats(self):
        ids = self._seed(130)
        # 前 40 单配成，其余待补 -> 待补在前
        for fid in ids[:40]:
            services.import_reports(self.conn, [
                rep(fid, fid + "-A", "B1", "LA", "BA"),
                rep(fid, fid + "-B", "B2", "LB", "BA"),
            ])
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        # 页首四项全库统计仍在
        self.assertIn("应复核单", resp.text)
        self.assertIn("已采信（两份报告）", resp.text)
        self.assertIn("待补/无法配对", resp.text)
        self.assertIn("重核中", resp.text)
        # 原列表各列表头保留
        for header in ("采样单号", "报告入档（有效/总数）", "采信的报告号", "状态", "重核", "操作"):
            self.assertIn(header, resp.text)
        # 首页 50 单且全部待补（90 待补的前 50）
        page_rows = services.overview_page(self.conn)["rows"]
        self.assertEqual(len(page_rows), 50)
        self.assertTrue(all(r["pending"] == 1 for r in page_rows))
        # 下一页链接带游标与 epoch
        self.assertIn("c_pending=", resp.text)
        self.assertIn("c_form=", resp.text)
        self.assertIn("epoch=", resp.text)
        self.assertIn("下一页", resp.text)

    def test_walk_all_pages_via_links_without_offset(self):
        ids = self._seed(130)
        for fid in ids[:40]:
            services.import_reports(self.conn, [
                rep(fid, fid + "-A", "B1", "LA", "BA"),
                rep(fid, fid + "-B", "B2", "LB", "BA"),
            ])
        # 跟着“下一页”链接翻完全部 3 页，每页不超过 50，覆盖全部 130 单
        seen = set()
        url = "/"
        page_count = 0
        while True:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
            # 从页面提取本页单号（详情链接 /forms/<id>）
            import re
            found = set(re.findall(r"/forms/(F\d+)", resp.text))
            self.assertLessEqual(len(found), 50)
            self.assertTrue(found.isdisjoint(seen))
            seen |= found
            page_count += 1
            m = re.search(
                r'href="(/\?dir=next[^"]+)"[^>]*>下一页', resp.text
            )
            if not m:
                break
            url = m.group(1).replace("&amp;", "&")
            self.assertLessEqual(page_count, 4)
        self.assertEqual(len(seen), 130)
        self.assertEqual(page_count, 3)

    def test_stale_epoch_shows_home_notice_and_hides_rows(self):
        ids = self._seed(130)
        # 取首页并构造一个深页链接
        resp = self.client.get("/")
        import re
        link = re.search(r'href="(/\?dir=next[^"]+)"[^>]*>下一页', resp.text)
        self.assertIsNotNone(link)
        next_url = link.group(1).replace("&amp;", "&")

        # 数据未变：深页正常
        resp = self.client.get(next_url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("总览数据已变化", resp.text)
        self.assertIn("采样单号", resp.text)

        # 模拟用旧 epoch 翻页：直接把链接里的 epoch 减 1
        stale_url = re.sub(r"epoch=\d+", "epoch=0", next_url)
        resp = self.client.get(stale_url)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("总览数据已变化", resp.text)
        self.assertIn("回到首页重新查看", resp.text)
        # 旧位置明细不再渲染
        self.assertNotIn("/forms/F00050", resp.text)

    def test_new_form_insertion_invalidates_deep_page(self):
        ids = self._seed(130)
        resp = self.client.get("/")
        import re
        next_url = re.search(
            r'href="(/\?dir=next[^"]+)"[^>]*>下一页', resp.text
        ).group(1).replace("&amp;", "&")

        # 新单入册后，沿用旧链接 -> 提示回首页
        services.import_forms(self.conn, "F99999")
        self.conn.commit()
        resp = self.client.get(next_url)
        self.assertIn("总览数据已变化", resp.text)

    def test_count_only_change_keeps_deep_page_valid(self):
        ids = self._seed(130)
        resp = self.client.get("/")
        import re
        next_url = re.search(
            r'href="(/\?dir=next[^"]+)"[^>]*>下一页', resp.text
        ).group(1).replace("&amp;", "&")

        # 给一个仍待补的单加一份不构成配对的报告：只改计数、不进出待补
        target = next(f for f in ids)
        services.import_reports(self.conn, [rep(target, "R-ONLY", "B9", "LZ", "BZ")])
        resp = self.client.get(next_url)
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("总览数据已变化", resp.text)
        self.assertIn("采样单号", resp.text)

    def test_invalidation_and_onspot_repair_reflect_immediately(self):
        # 跨页补证与作废当场换证：提交后首页统计与标记立即反映
        ids = self._seed(130)
        for fid in ids[:60]:
            services.import_reports(self.conn, [
                rep(fid, fid + "-A", "B1", "LA", "BA"),
                rep(fid, fid + "-B", "B2", "LB", "BA"),
            ])
        resp = self.client.get("/")
        self.assertIn(">60<", resp.text)   # 已采信 60

        # 作废 BA：60 单全部回到待补，首页待补变为 130、重核中 60
        services.invalidate_batch(self.conn, "BA")
        resp = self.client.get("/")
        self.assertIn(">130<", resp.text)  # 待补/无法配对 130
        self.assertIn(">60<", resp.text)  # 重核中 60

        # 当场用其他批补两份报告换证 -> 立即采信、重核核销
        fid = ids[0]
        services.import_reports(self.conn, [
            rep(fid, fid + "-C", "B3", "LC", "BC"),
            rep(fid, fid + "-D", "B4", "LD", "BC"),
        ])
        resp = self.client.get("/")
        self.assertIn(">1<", resp.text)   # 已采信恢复为 1
        # 该单已移到已采信组（总览末尾），采信报告号在详情页立即可见
        self.assertEqual(
            self.conn.execute(
                "SELECT pending FROM forms WHERE form_id=?", (fid,)
            ).fetchone()["pending"],
            0,
        )
        detail = self.client.get(f"/forms/{fid}")
        self.assertIn(fid + "-C", detail.text)
        self.assertIn(fid + "-D", detail.text)
        self.assertIn("生效中（采信）", detail.text)

    def test_failed_submission_does_not_change_overview(self):
        ids = self._seed(10)
        before = self.client.get("/").text
        resp = self.client.post("/batches", data={"batch_id": "GHOST"},
                                follow_redirects=True)
        self.assertIn("作废失败", resp.text)
        after = self.client.get("/").text
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main(verbosity=2)
