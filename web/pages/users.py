"""ユーザー管理の画面(7.4.8、UI-17、UI-18、UI-21〜UI-23、LG-05)。"""

import sqlite3

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from common.constants import ROLE_ADMIN, ROLE_VIEWER
from common.timeutil import utc_now_iso
from web.auth import CurrentUser
from web.context import WebContext
from web.users import ROLE_LABELS, change_role, create_user, delete_user, get_user, list_users, reset_password

MESSAGES = {
    "created": "ユーザーを追加しました",
    "updated": "権限を変更しました",
    "deleted": "ユーザーを削除しました",
    "reset": "パスワードを初期化しました",
    "notfound": "ユーザーが見つかりません",
}


def register(app: FastAPI, ctx: WebContext) -> None:
    def users_page(
        request: Request,
        user: CurrentUser,
        db: sqlite3.Connection,
        errors: list[str] | None = None,
        message: str = "",
        form: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return ctx.render(
            request,
            "users.html",
            {
                "users": list_users(db),
                "role_labels": ROLE_LABELS,
                "errors": errors or [],
                "message": message,
                "form": form or {"username": "", "role": ROLE_VIEWER},
                "now_iso": utc_now_iso(ctx.now()),
            },
            user,
            status_code,
        )

    @app.get("/users")
    def get_users(
        request: Request,
        message: str = "",
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        return users_page(request, user, db, message=MESSAGES.get(message, ""))

    @app.post("/users/new")
    def post_new_user(
        request: Request,
        username: str = Form(""),
        role: str = Form(""),
        password: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        ctx.verify_csrf(user, csrf)
        errors = create_user(db, user, username, role, password)
        if errors:
            return users_page(request, user, db, errors, form={"username": username, "role": role}, status_code=400)
        return RedirectResponse("/users?message=created", status_code=303)

    @app.post("/users/{user_id}/role")
    def post_change_role(
        request: Request,
        user_id: int,
        role: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        ctx.verify_csrf(user, csrf)
        errors = change_role(db, user, user_id, role)
        if errors:
            return users_page(request, user, db, errors, status_code=400)
        if user_id == user.id and role != ROLE_ADMIN:
            # 自分を閲覧者に変えた場合は、管理者用の画面は使えなくなる
            return RedirectResponse("/dashboard", status_code=303)
        return RedirectResponse("/users?message=updated", status_code=303)

    # ------------------------------------------------------------------
    # 削除とパスワードの初期化(確認画面を表示してから行う。UI-21)
    # ------------------------------------------------------------------
    @app.get("/users/{user_id}/delete")
    def get_delete_user(
        request: Request,
        user_id: int,
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        target = get_user(db, user_id)
        if target is None:
            return RedirectResponse("/users?message=notfound", status_code=303)
        return ctx.render(request, "user_confirm.html", {"target": target, "action": "delete"}, user)

    @app.post("/users/{user_id}/delete")
    def post_delete_user(
        request: Request,
        user_id: int,
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        ctx.verify_csrf(user, csrf)
        errors = delete_user(db, user, user_id)
        if errors:
            return users_page(request, user, db, errors, status_code=400)
        return RedirectResponse("/users?message=deleted", status_code=303)

    @app.get("/users/{user_id}/reset")
    def get_reset_password(
        request: Request,
        user_id: int,
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        target = get_user(db, user_id)
        if target is None:
            return RedirectResponse("/users?message=notfound", status_code=303)
        return ctx.render(request, "user_confirm.html", {"target": target, "action": "reset"}, user)

    @app.post("/users/{user_id}/reset")
    def post_reset_password(
        request: Request,
        user_id: int,
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        ctx.verify_csrf(user, csrf)
        errors = reset_password(db, user.username, user_id)
        if errors:
            return users_page(request, user, db, errors, status_code=400)
        if user_id == user.id:
            # 自分のパスワードを初期化した場合は、ログインし直す(AU-10)
            return RedirectResponse("/login", status_code=303)
        return RedirectResponse("/users?message=reset", status_code=303)
