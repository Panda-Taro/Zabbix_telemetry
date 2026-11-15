"""ログ画面(動作ログと変更履歴のタブ、絞り込み、ダウンロード)(7.4.6、LG-07〜LG-09)。"""

import sqlite3

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from common.constants import LOG_LEVELS, LOG_VIEW_MAX_ROWS
from web.auth import CurrentUser
from web.context import WebContext
from web.dashboard import JST
from web.logs import audit_logs_csv, make_filter, operation_logs_csv, read_audit_logs, read_operation_logs


def _download(content: bytes, prefix: str, ctx: WebContext) -> Response:
    stamp = ctx.now().astimezone(JST).strftime("%Y%m%d-%H%M%S")
    return Response(
        content,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{prefix}-{stamp}.csv"'},
    )


def register(app: FastAPI, ctx: WebContext) -> None:
    @app.get("/logs")
    def get_operation_logs(
        request: Request,
        date_from: str = "",
        date_to: str = "",
        level: str = "",
        hostname: str = "",
        keyword: str = "",
        user: CurrentUser = Depends(ctx.require_admin),
    ) -> HTMLResponse:
        flt = make_filter(date_from, date_to, level, hostname, keyword)
        entries, truncated = read_operation_logs(ctx.log_dir, flt)
        return ctx.render(
            request,
            "logs.html",
            {
                "tab": "operation",
                "entries": entries,
                "truncated": truncated,
                "max_rows": LOG_VIEW_MAX_ROWS,
                "filters": {"date_from": date_from[:32], "date_to": date_to[:32], "level": flt.level,
                            "hostname": flt.hostname, "keyword": flt.keyword},
                "log_levels": LOG_LEVELS,
                "query": request.url.query,
            },  # fmt: skip
            user,
        )

    @app.get("/logs/download")
    def download_operation_logs(
        date_from: str = "",
        date_to: str = "",
        level: str = "",
        hostname: str = "",
        keyword: str = "",
        user: CurrentUser = Depends(ctx.require_admin),
    ) -> Response:
        """表示している動作ログを、CSVファイルとしてダウンロードする(7.4.6.4)。"""
        entries, _ = read_operation_logs(ctx.log_dir, make_filter(date_from, date_to, level, hostname, keyword))
        return _download(operation_logs_csv(entries), "operation-log", ctx)

    @app.get("/logs/audit")
    def get_audit_logs(
        request: Request,
        date_from: str = "",
        date_to: str = "",
        keyword: str = "",
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        flt = make_filter(date_from, date_to, "", "", keyword)
        entries, truncated = read_audit_logs(db, flt)
        return ctx.render(
            request,
            "logs.html",
            {
                "tab": "audit",
                "entries": entries,
                "truncated": truncated,
                "max_rows": LOG_VIEW_MAX_ROWS,
                "filters": {"date_from": date_from[:32], "date_to": date_to[:32], "level": "", "hostname": "",
                            "keyword": flt.keyword},
                "log_levels": LOG_LEVELS,
                "query": request.url.query,
            },  # fmt: skip
            user,
        )

    @app.get("/logs/audit/download")
    def download_audit_logs(
        date_from: str = "",
        date_to: str = "",
        keyword: str = "",
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        """表示している変更履歴を、CSVファイルとしてダウンロードする(7.4.6.4)。"""
        entries, _ = read_audit_logs(db, make_filter(date_from, date_to, "", "", keyword))
        return _download(audit_logs_csv(entries), "audit-log", ctx)
