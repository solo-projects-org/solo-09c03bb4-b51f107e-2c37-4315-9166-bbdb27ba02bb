#!/usr/bin/env python3
"""幂等的总览分页演示数据：导入 130 个采样单并把其中 40 单配成，
使总览呈现 3 页（待补 90 单在前、已采信 40 单在后），用于 Docker Compose 演示翻页。

- 幂等：已存在的单号跳过，可在同一库重复执行；
- 用法：docker compose exec qc-review python scripts/seed_demo_paging.py
  （或本地：DB_PATH=./data/qc.db python scripts/seed_demo_paging.py）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import services  # noqa: E402

N_FORMS = 130
N_PAIRED = 40


def main() -> None:
    app = create_app()
    with app.app_context():
        from app.db import get_db
        conn = get_db()
        ids = [f"DEMO-{i:05d}" for i in range(N_FORMS)]
        imported = services.import_forms(conn, "\n".join(ids))
        conn.commit()

        rows = []
        for fid in ids[:N_PAIRED]:
            rows.append({"form_id": fid, "report_id": fid + "-A",
                         "bottle_no": "B1", "lab": "LAB-A", "batch_id": "BAT-DEMO"})
            rows.append({"form_id": fid, "report_id": fid + "-B",
                         "bottle_no": "B2", "lab": "LAB-B", "batch_id": "BAT-DEMO"})
        result = services.import_reports(conn, rows)

        meta = services.overview_stats(conn)
        print("分页演示数据已就绪（幂等，重复执行不产生重复单）：")
        print(f"  本次新增采样单 {len(imported['added'])} 个；"
              f"报告入档 {len(result['inserted'])} 份、拒收 {len(result['rejected'])} 份")
        print(f"  当前全库：应复核 {meta['total_forms']} / 已采信 {meta['paired_forms']} "
              f"/ 待补 {meta['pending_forms']} / 重核中 {meta['open_rechecks']}")
        print("  打开总览即可看到分页（每页最多 50 单，待补在前）。")


if __name__ == "__main__":
    main()
