"""ログイン、ログアウト、ダッシュボード、パスワード変更の画面(7.4.1、7.4.2、7.4.9)。"""

import sqlite3

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from common.constants import LOGIN_CSRF_COOKIE_NAME, SESSION_COOKIE_NAME
from web.auth import CurrentUser, change_own_password, find_session_user, login, logout, new_login_csrf_token
from web.context import WebContext
from web.dashboard import build_dashboard


def register(app: FastAPI, ctx: WebContext) -> None:
    @app.get("/", include_in_schema=False)
    def index() -> RedirectResponse:
        return RedirectResponse("/dashboard", status_code=303)

    # ------------------------------------------------------------------
    # ログインとログアウト(7.4.1)
    # ------------------------------------------------------------------
    def login_page(request: Request, message: str = "", status_code: int = 200) -> HTMLResponse:
        token = new_login_csrf_token()
        response = ctx.render(request, "login.html", {"message": message, "login_csrf": token}, status_code=status_code)
        response.set_cookie(LOGIN_CSRF_COOKIE_NAME, token, httponly=True, samesite="strict", path="/login")
        return response

    @app.get("/login")
    def get_login(request: Request, db: sqlite3.Connection = Depends(ctx.get_db)) -> Response:
        if ctx.current_user_or_none(request, db) is not None:
            return RedirectResponse("/dashboard", status_code=303)
        return login_page(request)

    @app.post("/login")
    def post_login(
        request: Request,
        username: str = Form(""),
        password: str = Form(""),
        csrf: str = Form(""),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        # ログインも、ログイン画面から送られたものだけを受け付ける(11.3.3)
        cookie_token = request.cookies.get(LOGIN_CSRF_COOKIE_NAME, "")
        if not csrf or not cookie_token or csrf != cookie_token:
            return login_page(request, "画面の有効期限が切れました。もう一度ログインしてください", status_code=400)
        result = login(db, username, password, ctx.now(), ctx.logger)
        if not result.ok or result.token is None:
            return login_page(request, result.message, status_code=401)
        response = RedirectResponse("/dashboard", status_code=303)
        ctx.set_session_cookie(response, result.token)
        response.delete_cookie(LOGIN_CSRF_COOKIE_NAME, path="/login")
        return response

    @app.post("/logout")
    def post_logout(
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_user),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        ctx.verify_csrf(user, csrf)
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
        user: CurrentUser = Depends(ctx.require_user),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        return ctx.render(request, "dashboard.html", {"view": build_dashboard(db, ctx.now())}, user)

    @app.get("/dashboard/content")
    def get_dashboard_content(
        user: CurrentUser = Depends(ctx.require_user),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        """10秒ごとの自動更新で読み込む、ダッシュボードの中身(7.1.4)。"""
        return HTMLResponse(ctx.render_text("_dashboard_content.html", user=user, view=build_dashboard(db, ctx.now())))

    # ------------------------------------------------------------------
    # パスワード変更(7.4.9)
    # ------------------------------------------------------------------
    @app.get("/password")
    def get_password(request: Request, user: CurrentUser = Depends(ctx.require_user)) -> HTMLResponse:
        return ctx.render(request, "password.html", {"errors": [], "done": False}, user)

    @app.post("/password")
    def post_password(
        request: Request,
        current_password: str = Form(""),
        new_password: str = Form(""),
        new_password_confirm: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_user),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        ctx.verify_csrf(user, csrf)
        errors = change_own_password(db, user, current_password, new_password, new_password_confirm, ctx.now())
        if errors:
            return ctx.render(request, "password.html", {"errors": errors, "done": False}, user, 400)
        refreshed = find_session_user(db, user.token) or user
        return ctx.render(request, "password.html", {"errors": [], "done": True}, refreshed)
