"""WebGUI の画面と処理(⑦、AU-01〜AU-10、UI-01〜UI-06、UI-19、UI-20、LG-05、11.3.3)。

権限の確認は、画面の表示(メニューの出し分け)だけでなく、サーバー側の処理でも必ず行う(7.2.2)。
画面に表示する文字列は、テンプレートの自動の無害化(エスケープ)を必ず通す(11.3.3)。
"""

import logging
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import jinja2
from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from common.config_writer import seed_initial_data
from common.constants import (
    DASHBOARD_REFRESH_SEC,
    LOGIN_CSRF_COOKIE_NAME,
    SESSION_COOKIE_MAX_AGE_SEC,
    SESSION_COOKIE_NAME,
    SYSTEM_NAME,
)
from common.database import connect, initialize_schema
from web.auth import (
    CurrentUser,
    change_own_password,
    check_csrf,
    csrf_token_for,
    find_session_user,
    login,
    logout,
    new_login_csrf_token,
)
from web.dashboard import build_dashboard

WEB_DIR = Path(__file__).resolve().parent

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

# 左ペインのメニュー(7.3)。(表示名, URL, 管理者だけか, 階層)
MENU: tuple[tuple[str, str, bool, int], ...] = (
    ("ダッシュボード", "/dashboard", False, 0),
    ("機器", "", True, 0),
    ("機器一覧", "/devices", True, 1),
    ("全体設定", "/settings", True, 0),
    ("ログ", "", True, 0),
    ("動作ログ", "/logs", True, 1),
    ("変更履歴", "/logs/audit", True, 1),
    ("管理", "", True, 0),
    ("システム設定", "/system", True, 1),
    ("ユーザー管理", "/users", True, 1),
)

# 管理者だけが使える画面のうち、次の段階で作るもの(この段階では「準備中」を表示する)
ADMIN_PLACEHOLDER_PAGES: dict[str, str] = {
    "/devices": "機器一覧",
    "/settings": "全体設定",
    "/logs": "動作ログ",
    "/logs/audit": "変更履歴",
    "/system": "システム設定",
    "/users": "ユーザー管理",
}


class NotAuthenticated(Exception):
    """ログインしていない。"""


class Forbidden(Exception):
    """権限がない。"""

    def __init__(self, user: CurrentUser) -> None:
        super().__init__("権限がありません")
        self.user = user


def create_app(
    db_path: str | Path,
    logger: logging.Logger,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> FastAPI:
    """WebGUI のアプリを作る。データベースの準備(テーブルと初期データ)もここで行う(D-012)。"""
    conn = connect(db_path)
    try:
        initialize_schema(conn)
        seed_initial_data(conn)
    finally:
        conn.close()

    app = FastAPI(title=SYSTEM_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
    templates = jinja2.Environment(
        loader=jinja2.FileSystemLoader(WEB_DIR / "templates"),
        autoescape=True,  # 画面に表示する文字列は、すべて無害化する(11.3.3)
        undefined=jinja2.StrictUndefined,
    )

    # ------------------------------------------------------------------
    # 共通の部品
    # ------------------------------------------------------------------
    def get_db() -> Iterator[sqlite3.Connection]:
        db = connect(db_path, check_same_thread=False)
        try:
            yield db
        finally:
            db.close()

    def current_user_or_none(request: Request, db: sqlite3.Connection) -> CurrentUser | None:
        return find_session_user(db, request.cookies.get(SESSION_COOKIE_NAME))

    def require_user(request: Request, db: sqlite3.Connection = Depends(get_db)) -> CurrentUser:
        """ログインを必須にする(AU-01)。"""
        user = current_user_or_none(request, db)
        if user is None:
            raise NotAuthenticated()
        return user

    def require_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
        """管理者だけに許す(AU-03、AU-04)。"""
        if not user.is_admin:
            raise Forbidden(user)
        return user

    def set_session_cookie(response: Response, token: str) -> None:
        # 画面上のプログラムから読み取れないようにする(HttpOnly、11.3.3)。
        # ブラウザを閉じてもログインしたままにするため、期限付きのCookieとし、画面を開くたびに延ばす(D-024)
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token,
            max_age=SESSION_COOKIE_MAX_AGE_SEC,
            httponly=True,
            samesite="lax",
            path="/",
        )

    def render(
        request: Request,
        template: str,
        context: dict[str, Any],
        user: CurrentUser | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        page = templates.get_template(template).render(
            system_name=SYSTEM_NAME,
            user=user,
            menu=[item for item in MENU if user is not None and (user.is_admin or not item[2])],
            current_path=request.url.path,
            csrf_token=csrf_token_for(user.token) if user else "",
            refresh_sec=DASHBOARD_REFRESH_SEC,
            **context,
        )
        response = HTMLResponse(page, status_code=status_code)
        if user is not None:
            set_session_cookie(response, user.token)
        return response

    # ------------------------------------------------------------------
    # 例外の扱いと、応答ヘッダー
    # ------------------------------------------------------------------
    @app.exception_handler(NotAuthenticated)
    async def handle_not_authenticated(request: Request, exc: NotAuthenticated) -> Response:
        if request.headers.get("HX-Request"):
            # 自動更新の途中でログアウトされていた場合は、ログイン画面へ移る
            return Response(status_code=200, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.exception_handler(Forbidden)
    async def handle_forbidden(request: Request, exc: Forbidden) -> Response:
        return render(request, "forbidden.html", {}, exc.user, status_code=403)

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            if name == "Cache-Control" and request.url.path.startswith("/static/"):
                continue
            response.headers.setdefault(name, value)
        return response

    # ------------------------------------------------------------------
    # ログインとログアウト(7.4.1)
    # ------------------------------------------------------------------
    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=303)

    def login_page(request: Request, message: str = "", status_code: int = 200) -> HTMLResponse:
        token = new_login_csrf_token()
        response = render(request, "login.html", {"message": message, "login_csrf": token}, status_code=status_code)
        response.set_cookie(LOGIN_CSRF_COOKIE_NAME, token, httponly=True, samesite="strict", path="/login")
        return response

    @app.get("/login")
    def get_login(request: Request, db: sqlite3.Connection = Depends(get_db)) -> Response:
        if current_user_or_none(request, db) is not None:
            return RedirectResponse("/dashboard", status_code=303)
        return login_page(request)

    @app.post("/login")
    def post_login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        csrf: str = Form(""),
        db: sqlite3.Connection = Depends(get_db),
    ) -> Response:
        cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE_NAME, "")
        if not csrf or not cookie_token or csrf != cookie_token:
            return login_page(request, "画面の有効期限が切れました。もう一度ログインしてください", status_code=400)
        result = login(db, username, password, now(), logger)
        if not result.ok or result.token is None:
            return login_page(request, result.message, status_code=401)
        response = RedirectResponse("/dashboard", status_code=303)
        set_session_cookie(response, result.token)
        response.delete_cookie(LOGIN_CSRF_COOKIE_NAME, path="/login")
        return response

    @app.post("/logout")
    def post_logout(
        csrf: str = Form(""),
        user: CurrentUser = Depends(require_user),
        db: sqlite3.Connection = Depends(get_db),
    ) -> Response:
        if not check_csrf(user.token, csrf):
            return Response("不正な操作です", status_code=403)
        logout(db, user)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE_NAME, path="/")
        return response

    # ------------------------------------------------------------------
    # ダッシュボード(7.4.2)
    # ------------------------------------------------------------------
    @app.get("/dashboard")
    def get_dashboard(
        request: Request,
        user: CurrentUser = Depends(require_user),
        db: sqlite3.Connection = Depends(get_db),
    ) -> HTMLResponse:
        return render(request, "dashboard.html", {"view": build_dashboard(db, now())}, user)

    @app.get("/dashboard/content")
    def get_dashboard_content(
        request: Request,
        user: CurrentUser = Depends(require_user),
        db: sqlite3.Connection = Depends(get_db),
    ) -> HTMLResponse:
        """10秒ごとの自動更新で読み込む、ダッシュボードの中身(7.1.4)。"""
        page = templates.get_template("_dashboard_content.html").render(
            user=user, view=build_dashboard(db, now())
        )
        return HTMLResponse(page)

    # ------------------------------------------------------------------
    # パスワード変更(7.4.9)
    # ------------------------------------------------------------------
    @app.get("/password")
    def get_password(request: Request, user: CurrentUser = Depends(require_user)) -> HTMLResponse:
        return render(request, "password.html", {"errors": [], "done": False}, user)

    @app.post("/password")
    def post_password(
        request: Request,
        current_password: str = Form(""),
        new_password: str = Form(""),
        new_password_confirm: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(require_user),
        db: sqlite3.Connection = Depends(get_db),
    ) -> HTMLResponse:
        if not check_csrf(user.token, csrf):
            return render(request, "password.html", {"errors": ["不正な操作です"], "done": False}, user, 403)
        errors = change_own_password(db, user, current_password, new_password, new_password_confirm, now())
        if errors:
            return render(request, "password.html", {"errors": errors, "done": False}, user, 400)
        refreshed = find_session_user(db, user.token) or user
        return render(request, "password.html", {"errors": [], "done": True}, refreshed)

    # ------------------------------------------------------------------
    # 管理者用の画面(次の段階で作る。権限の確認だけは、この段階から行う)
    # ------------------------------------------------------------------
    def make_placeholder(title: str) -> Callable[..., HTMLResponse]:
        def placeholder(request: Request, user: CurrentUser = Depends(require_admin)) -> HTMLResponse:
            return render(request, "placeholder.html", {"title": title}, user)

        return placeholder

    for path, title in ADMIN_PLACEHOLDER_PAGES.items():
        app.add_api_route(path, make_placeholder(title), methods=["GET"], response_class=HTMLResponse)

    return app
