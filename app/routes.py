"""网页路由：名单导入、报告续传、配对名单、按批作废与重核。"""
from __future__ import annotations

from flask import (
    Blueprint,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

from . import services
from .csv_parser import decode_bytes, parse_report_csv
from .db import get_db

bp = Blueprint("main", __name__)


@bp.route("/")
def index():
    """采样单总览：键集分页（每页最多 50 单，待补在前、组内按单号）。

    - 页首四项全库统计读触发器维护的元数据，不逐单重算；
    - 统计与当页明细在同一只读事务（同一已提交快照）内读取；
    - 翻页链接带入册时的 pagination_epoch；若期间有新单入册或待补集合进出，
      则版本不一致，提示回首页，不沿旧位置继续翻。
    """
    conn = get_db()
    direction = request.args.get("dir", "next")
    if direction not in ("next", "prev"):
        direction = "next"
    pg_arg = request.args.get("pg", "1")
    try:
        page_no = max(1, int(pg_arg))
    except (TypeError, ValueError):
        page_no = 1

    cursor_pending = None
    cursor_form = None
    if "c_pending" in request.args and "c_form" in request.args:
        try:
            cursor_pending = 1 if int(request.args["c_pending"]) == 1 else 0
        except (TypeError, ValueError):
            cursor_pending = None
        cursor_form = (request.args["c_form"] or "").strip() or None
        if cursor_form is None:
            cursor_pending = None

    try:
        epoch_arg = int(request.args.get("epoch", ""))
    except (TypeError, ValueError):
        epoch_arg = None

    has_cursor = cursor_pending is not None and cursor_form is not None
    # 仅在“携带游标翻深页且 epoch 已变”时判定失效；首页（无游标）永远渲染最新状态
    stale = has_cursor and epoch_arg is not None

    # 只读事务：统计与明细取自同一个已提交快照，期间提交的写入不影响本页
    conn.execute("BEGIN DEFERRED")
    try:
        meta = services.overview_stats(conn)
        current_epoch = meta["pagination_epoch"]
        stats = {
            "total_forms": meta["total_forms"],
            "paired": meta["paired_forms"],
            "pending": meta["pending_forms"],
            "open_rechecks": meta["open_rechecks"],
        }

        stale = stale and epoch_arg != current_epoch
        if stale:
            # 数据已变（新单入册或待补进出）：不取旧位置明细，提示回首页
            page = {"rows": [], "has_more": False}
        else:
            page = services.overview_page(
                conn,
                direction=direction,
                cursor_pending=cursor_pending if has_cursor else None,
                cursor_form=cursor_form if has_cursor else None,
            )
    finally:
        conn.commit()  # 结束只读事务（无写入，commit 仅释放快照）

    forms = page["rows"]
    has_next = page["has_more"]
    has_prev = page_no > 1

    # 翻页游标：下一页用本页最后一单，上一页用本页第一单
    next_cursor = prev_cursor = None
    if forms:
        last = forms[-1]
        next_cursor = {"pending": last["pending"], "form": last["form_id"]}
        first = forms[0]
        prev_cursor = {"pending": first["pending"], "form": first["form_id"]}

    return render_template(
        "index.html",
        forms=forms,
        stats=stats,
        page_no=page_no,
        epoch=current_epoch,
        stale=stale,
        has_next=has_next,
        has_prev=has_prev,
        next_cursor=next_cursor,
        prev_cursor=prev_cursor,
        page_size=services.PAGE_SIZE,
    )


@bp.route("/forms/import", methods=["GET", "POST"])
def import_forms():
    if request.method == "POST":
        raw = request.form.get("form_ids", "")
        conn = get_db()
        result = services.import_forms(conn, raw)
        conn.commit()
        flash(f"名单导入完成：新增 {len(result['added'])} 单，"
              f"已存在 {len(result['existing'])} 单，"
              f"非法 {len(result['invalid'])} 条。", "info")
        if result["invalid"]:
            flash("非法条目（含空格）：" + "、".join(result["invalid"][:20]), "warning")
        return redirect(url_for("main.index"))
    return render_template("import_forms.html")


@bp.route("/reports/upload", methods=["GET", "POST"])
def upload_reports():
    result = None
    if request.method == "POST":
        text = request.form.get("csv_text", "")
        file = request.files.get("csv_file")
        if file and file.filename:
            text = decode_bytes(file.read())
        if not text.strip():
            flash("请上传 CSV 文件或粘贴 CSV 文本。", "warning")
            return redirect(url_for("main.upload_reports"))

        rows, errors = parse_report_csv(text)
        if errors:
            for msg in errors:
                flash("CSV 解析失败：" + msg, "danger")
            return render_template("upload_reports.html", result=None)

        conn = get_db()
        try:
            result = services.import_reports(conn, rows)
        except Exception as exc:  # 意外错误整体回滚
            conn.rollback()
            current_app.logger.exception("报告入档失败")
            flash(f"入档失败，已全部回滚（未作废任何数据）：{exc}", "danger")
            return render_template("upload_reports.html", result=None)

        flash(
            f"续传完成：入档 {len(result['inserted'])} 份，"
            f"拒收 {len(result['rejected'])} 份；"
            f"新配成 {len(result['paired'])} 单，待补 {len(result['pending'])} 单。",
            "info",
        )
    return render_template("upload_reports.html", result=result)


@bp.route("/forms/<form_id>")
def form_detail(form_id):
    conn = get_db()
    detail = services.form_detail(conn, form_id)
    if detail is None:
        flash(f"单号 {form_id} 不在名单中。", "warning")
        return redirect(url_for("main.index"))
    return render_template("form_detail.html", d=detail)


@bp.route("/batches", methods=["GET", "POST"])
def batches():
    conn = get_db()
    if request.method == "POST":
        batch_id = (request.form.get("batch_id") or "").strip()
        note = (request.form.get("note") or "").strip()
        if not batch_id:
            flash("请填写要作废的质控批号。", "warning")
            return redirect(url_for("main.batches"))
        try:
            outcome = services.invalidate_batch(conn, batch_id, note)
        except ValueError as exc:
            conn.rollback()
            flash(f"作废失败，未改动任何数据：{exc}", "danger")
            return redirect(url_for("main.batches"))
        inv = outcome["invalidation"]
        flash(
            f"质控批 {batch_id} 已作废（{inv['invalidated_at']}）："
            f"涉及 {len(outcome['affected_forms'])} 单，"
            f"原配对保留 {outcome['retained']} 单，"
            f"以其他批报告重建 {outcome['rebuilt']} 单，"
            "其余进入重核待补；该批新报告今后一律拒收。",
            "info",
        )
        return redirect(url_for("main.batches"))
    return render_template(
        "batches.html",
        batches=services.batches_overview(conn),
        rechecks=services.open_rechecks(conn),
    )


@bp.route("/rechecks")
def rechecks():
    conn = get_db()
    return render_template("rechecks.html", rechecks=services.open_rechecks(conn))
