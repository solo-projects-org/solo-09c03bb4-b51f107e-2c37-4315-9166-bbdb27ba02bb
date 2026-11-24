#!/usr/bin/env python3
"""三个需求场景的端到端试跑：
  A. 作废换证：整批作废 -> 原配对解体/保留 -> 幸存者优先重建 -> 该批新报告拒收
  B. 交错入档：重核期间名单照常查询，其他批报告交错续传、自动核销
  C. 总览分页：每页最多 50、待补在前、键集翻页不扫全库、翻页版本失效提示
用法：python3 scripts/demo_scenarios.py [数据库路径]（默认 data/demo.db，自动重建）
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app          # noqa: E402
from app import services           # noqa: E402
from app.csv_parser import parse_report_csv  # noqa: E402

HEADER = "单号,报告号,原瓶号,实验室,质控批号"


def rep(form, rid, bottle, lab, batch):
    return {"form_id": form, "report_id": rid, "bottle_no": bottle,
            "lab": lab, "batch_id": batch}


def upload(conn, csv_text):
    rows, errors = parse_report_csv(csv_text)
    assert not errors, errors
    return services.import_reports(conn, rows)


def snapshot(conn, title):
    print(f"\n----- {title} -----")
    for f in services.forms_overview(conn):
        if f["pairing_id"]:
            status = f"采信 {f['report_a_id']} + {f['report_b_id']}"
        else:
            status = "待补（无生效配对）"
        recheck = f"，重核中×{f['open_rechecks']}" if f["open_rechecks"] else ""
        print(f"  {f['form_id']}: 有效报告 {f['valid_reports']}/{f['total_reports']} | {status}{recheck}")
    opens = services.open_rechecks(conn)
    if opens:
        print("  重核待补名单:", ", ".join(
            f"{e['form_id']}(触发批 {e['batch_id']}, 有效报告 {e['valid_reports']})"
            for e in opens))
    else:
        print("  重核待补名单: 空")


def scenario_a_invalidate_and_replace(conn):
    print("=" * 70)
    print("场景 A：作废换证")
    print("=" * 70)
    services.import_forms(conn, "F-101\nF-102\nF-103")
    conn.commit()

    upload(conn, "\n".join([
        HEADER,
        # F-101 两份均 BAT-1；F-102 一份 BAT-1 一份 BAT-2；F-103 全 BAT-2
        "F-101,R101-1,B1,LAB-A,BAT-1",
        "F-101,R101-2,B2,LAB-B,BAT-1",
        "F-102,R102-1,B3,LAB-A,BAT-1",
        "F-102,R102-2,B4,LAB-B,BAT-2",
        "F-103,R103-1,B5,LAB-A,BAT-2",
        "F-103,R103-2,B6,LAB-B,BAT-2",
    ]))
    snapshot(conn, "首批报告入档后（三单均已采信）")

    out = services.invalidate_batch(conn, "BAT-1", note="演练作废")
    print(f"\n>> 作废 BAT-1：涉及 {out['affected_forms']}，"
          f"原配对保留 {out['retained']} 单，当场重建 {out['rebuilt']} 单")
    snapshot(conn, "作废生效后（与重核名单同一事务）")

    # 重核期间补其他批报告
    result = upload(conn, "\n".join([
        HEADER,
        "F-101,R101-3,B7,LAB-A,BAT-3",   # F-101 第一份替代
        "F-102,R102-3,B8,LAB-A,BAT-3",   # F-102 幸存者 R102-2 的替代搭档
    ]))
    print(f"\n>> 第一批替代报告入档：配成 {result['paired']}，待补 {result['pending']}")
    p102 = services.active_pairing(conn, "F-102")
    print(f">> F-102 新配对：{p102['report_a_id']} + {p102['report_b_id']}"
          f"（幸存者 R102-2 {'已保留' if 'R102-2' in (p102['report_a_id'], p102['report_b_id']) else '丢失！'}）")
    snapshot(conn, "部分替代报告入档后")

    # 已作废批次的新报告拒收
    rejected = upload(conn, "\n".join([
        HEADER,
        "F-101,R101-X,B9,LAB-C,BAT-1",
    ]))
    print(f"\n>> 作废后再传 BAT-1 报告：入档 {len(rejected['inserted'])} 份，"
          f"拒收 {len(rejected['rejected'])} 份（原因：{rejected['rejected'][0]['reason']}）")

    result = upload(conn, "\n".join([
        HEADER,
        "F-101,R101-4,B9,LAB-B,BAT-3",
    ]))
    print(f">> 第二批替代报告入档：配成 {result['paired']}，待补 {result['pending']}")
    snapshot(conn, "F-101 补齐后的最终状态")
    assert not services.open_rechecks(conn), "重核应全部核销"


def scenario_b_interleaved_archiving(conn):
    print("\n" + "=" * 70)
    print("场景 B：交错入档（重核期间照常查名单、收其他批报告）")
    print("=" * 70)
    services.import_forms(conn, "F-201\nF-202\nF-203")
    conn.commit()

    upload(conn, "\n".join([
        HEADER,
        "F-201,R201-1,B1,LAB-A,BAT-X1",
        "F-201,R201-2,B2,LAB-B,BAT-X1",
        "F-202,R202-1,B3,LAB-A,BAT-X1",   # 只有一份，本就待补
    ]))
    snapshot(conn, "BAT-X1 部分入档（F-201 采信，F-202/F-203 待补）")

    services.invalidate_batch(conn, "BAT-X1")
    print("\n>> BAT-X1 作废，F-201/F-202 进入重核；重核期间继续操作")
    snapshot(conn, "作废后即刻查询名单（页面查询不受影响）")

    # 交错：其他批报告一份一份来，中间夹一个无关新单的配对
    upload(conn, "\n".join([HEADER, "F-201,R201-3,B4,LAB-A,BAT-X2"]))
    print("\n>> F-201 收到 BAT-X2 第一份（仍缺一份），名单仍可查：")
    snapshot(conn, "F-201 半补齐状态")

    upload(conn, "\n".join([
        HEADER,
        "F-203,R203-1,B5,LAB-A,BAT-X4",
        "F-203,R203-2,B6,LAB-B,BAT-X4",
    ]))
    print("\n>> 重核期间无关单 F-203 用 BAT-X4 正常入档并配成：")
    snapshot(conn, "无关单 F-203 正常配成，F-201/F-202 仍重核")

    upload(conn, "\n".join([
        HEADER,
        "F-201,R201-4,B7,LAB-B,BAT-X3",
        "F-202,R202-2,B8,LAB-B,BAT-X2",   # 与 R202-1？R202-1 已失效；还需第二份
    ]))
    print("\n>> F-201 补齐自动核销；F-202 只来一份仍待补：")
    snapshot(conn, "F-201 核销，F-202 继续待补")

    upload(conn, "\n".join([HEADER, "F-202,R202-3,B9,LAB-C,BAT-X3"]))
    snapshot(conn, "F-202 补齐后的最终状态")
    assert not services.open_rechecks(conn)


def scenario_c_pagination(conn):
    """场景 C：总览分页——每页最多 50、待补在前、键集翻页不扫全库、版本失效提示。"""
    print("\n" + "=" * 70)
    print("场景 C：总览键集分页（130 单，前 40 单已采信、90 单待补）")
    print("=" * 70)
    ids = [f"P-{i:05d}" for i in range(130)]
    services.import_forms(conn, "\n".join(ids))
    conn.commit()
    for fid in ids[:40]:
        services.import_reports(conn, [
            rep(fid, fid + "-A", "B1", "LAB-A", "BAT-P1"),
            rep(fid, fid + "-B", "B2", "LAB-B", "BAT-P1"),
        ])

    meta = services.overview_stats(conn)
    print(f"\n>> 页首统计（读单行元数据，O(1)）：应复核 {meta['total_forms']} / "
          f"已采信 {meta['paired_forms']} / 待补 {meta['pending_forms']} / "
          f"重核中 {meta['open_rechecks']}；当前翻页版本 epoch={meta['pagination_epoch']}")

    print(">> 跟随游标翻页（无 OFFSET，深页不扫前面的单）：")
    seen, cp, cf, page_no = [], None, None, 0
    while True:
        pg = services.overview_page(conn, "next", cp, cf)
        rows = pg["rows"]
        page_no += 1
        n_pending = sum(1 for r in rows if r["pending"] == 1)
        n_paired = len(rows) - n_pending
        label = f"待补 {n_pending}" + (f"、已采信 {n_paired}" if n_paired else "")
        print(f"   第 {page_no} 页：{len(rows)} 单（{label}）"
              f"  首 {rows[0]['form_id']} … 尾 {rows[-1]['form_id']}")
        seen.extend(r["form_id"] for r in rows)
        if not pg["has_more"]:
            break
        cp, cf = rows[-1]["pending"], rows[-1]["form_id"]
    # 分页覆盖的是全表：拼接顺序必须与总览全量顺序逐单一致（含前两个场景的单）
    full_all = [r["form_id"] for r in services.forms_overview(conn)]
    assert seen == full_all, "翻页拼接顺序必须与总览全量顺序一致"
    p_ids = {fid for fid in full_all if fid.startswith("P-")}
    assert len(p_ids) == 130 and p_ids.issubset(set(seen))
    print(f"   共 {page_no} 页、{len(seen)} 单，无重复无遗漏；"
          f"待补组全部排在已采信组之前，本场景 130 单均在其中。")

    # 只改计数、不进出待补 -> epoch 不变，深页链接继续有效
    before = services.overview_stats(conn)["pagination_epoch"]
    services.import_reports(conn, [rep(ids[-1], "R-ONLY", "B9", "LAB-Z", "BAT-P2")])
    after = services.overview_stats(conn)["pagination_epoch"]
    print(f"\n>> 给待补单 {ids[-1]} 加一份不构成配对的报告：epoch {before} -> {after}"
          f"（{'不变，深页照常翻' if before == after else '异常！'}）")

    # 新单入册 -> epoch 推进，旧链接应被判定失效
    before = services.overview_stats(conn)["pagination_epoch"]
    services.import_forms(conn, "P-NEW")
    conn.commit()
    after = services.overview_stats(conn)["pagination_epoch"]
    stale = before != after
    print(f">> 新单 P-NEW 入册：epoch {before} -> {after}；"
          f"携带旧 epoch 的深页链接会被总览页拦下，提示回首页：{stale}")
    assert stale


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "demo.db")
    if os.path.exists(db_path):
        for suffix in ("", "-wal", "-shm"):
            p = db_path + suffix
            if os.path.exists(p):
                os.remove(p)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    app = create_app({"DB_PATH": db_path})
    with app.app_context():
        from app.db import get_db
        conn = get_db()
        scenario_a_invalidate_and_replace(conn)
        scenario_b_interleaved_archiving(conn)
        scenario_c_pagination(conn)
    print("\n三个场景试跑全部通过 ✔")


if __name__ == "__main__":
    main()
