"""WebGUI のアプリ(⑦、11.3.3)。

画面ごとの処理は web/pages/ に置き、ここでまとめる。
"""

import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from common.config_writer import seed_initial_data
from common.constants import SYSTEM_NAME
from common.database import connect, initialize_schema
from web.context import WEB_DIR, CsrfError, Forbidden, NotAuthenticated, WebContext
from web.pages import account, devices, logs, settings, system, users

# 画面からの応答に付ける、セキュリティのための指定(11.3.3)
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "frame-ancestors 'none'; form-action 'self'; base-uri 'self'"
    ),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "Cache-Control": "no-store",
}


def create_app(
    db_path: str | Path,
    logger: logging.Logger,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    log_dir: Path | None = None,
) -> FastAPI:
    """WebGUI のアプリを作る。データベースの準備(テーブルと初期データ)もここで行う(D-012)。

    log_dir は、ログ画面で読む動作ログのフォルダ(8.1.2)。
    """
    conn = connect(db_path)
    try:
        initialize_schema(conn)
        seed_initial_data(conn)
    finally:
        conn.close()

    app = FastAPI(title=SYSTEM_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    ctx = WebContext(db_path, logger, now, log_dir)

    @app.exception_handler(NotAuthenticated)
    async def handle_not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if request.headers.get("HX-Request"):
            # 自動更新の途中でログアウトされていた場合は、ログイン画面へ移る
            return Response(status_code=200, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Forbidden)
    async def handle_forbidden(request: Request, exc: Forbidden) -> Response:
        return ctx.render(request, "forbidden.html", {}, exc.user, status_code=403)

    @app.exception_handler(CsrfError)
    async def handle_csrf_error(request: Request, exc: CsrfError) -> Response:
        return Response("不正な操作です。画面を開き直してから、もう一度操作してください。", status_code=403,
                        media_type="text/plain; charset=utf-8")  # fmt: skip

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            if name == "Cache-Control" and request.url.path.startswith("/static/"):
                continue
            response.headers.setdefault(name, value)
        return response

    for page in (account, devices, settings, users, logs, system):
        page.register(app, ctx)
    return app
