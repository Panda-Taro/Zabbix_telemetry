"""WebGUI の画面が共通で使う部品(データベース、ログイン状態と権限の確認、画面の組み立て)。

権限の確認は、画面の表示(メニューの出し分け)だけでなく、サーバー側の処理でも必ず行う(7.2.2)。
画面に表示する文字列は、テンプレートの自動の無害化(エスケープ)を必ず通す(11.3.3)。
"""

import logging
import sqlite3
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import jinja2
from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, Response

from common.constants import DASHBOARD_REFRESH_SEC, SESSION_COOKIE_MAX_AGE_SEC, SESSION_COOKIE_NAME, SYSTEM_NAME
from common.database import connect
from web.auth import CurrentUser, check_csrf, csrf_token_for, find_session_user
from web.hostctl import HostCtlClient

WEB_DIR = Path(__file__).resolve().parent

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


class NotAuthenticated(Exception):
    """ログインしていない。"""


class Forbidden(Exception):
    """権限がない。"""

    def __init__(self, user: CurrentUser) -> None:
        super().__init__("権限がありません")
        self.user = user


class CsrfError(Exception):
    """不正な操作を防ぐための印が正しくない(11.3.3)。"""


class WebContext:
    """画面が共通で使う部品の入れ物。"""

    def __init__(
        self,
        db_path: str | Path,
        logger: logging.Logger,
        now: Callable[[], datetime],
        log_dir: Path | None,
        hostctl: HostCtlClient | None = None,
        listen_port: int | None = None,
    ) -> None:
        self.db_path = db_path
        self.logger = logger
        self.now = now
        self.log_dir = log_dir
        # 本体側の補助プログラムへの依頼の出し方(決定事項 D-029)
        self.hostctl = hostctl or HostCtlClient(None)
        # 実際に待ち受けているWebGUIのポート番号(分からないときは None)
        self.listen_port = listen_port
        self.templates = jinja2.Environment(
            loader=jinja2.FileSystemLoader(WEB_DIR / "templates"),
            autoescape=True,  # 画面に表示する文字列は、すべて無害化する(11.3.3)
            undefined=jinja2.StrictUndefined,
        )

        # FastAPI の依存関係として使う関数(引数の形を FastAPI が読み取る)
        def get_db() -> Iterator[sqlite3.Connection]:
            db = connect(self.db_path, check_same_thread=False)
            try:
                yield db
            finally:
                db.close()

        def require_user(request: Request, db: sqlite3.Connection = Depends(get_db)) -> CurrentUser:
            """ログインを必須にする(AU-01)。"""
            user = find_session_user(db, request.cookies.get(SESSION_COOKIE_NAME))
            if user is None:
                raise NotAuthenticated()
            return user

        def require_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
            """管理者だけに許す(AU-03、AU-04)。"""
            if not user.is_admin:
                raise Forbidden(user)
            return user

        self.get_db = get_db
        self.require_user = require_user
        self.require_admin = require_admin

    def current_user_or_none(self, request: Request, db: sqlite3.Connection) -> CurrentUser | None:
        return find_session_user(db, request.cookies.get(SESSION_COOKIE_NAME))

    @staticmethod
    def verify_csrf(user: CurrentUser, submitted: str | None) -> None:
        """設定を変更する操作で、不正な操作を防ぐための印を確かめる(11.3.3)。"""
        if not check_csrf(user.token, submitted):
            raise CsrfError()

    @staticmethod
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

    def render_text(self, template: str, **context: Any) -> str:
        return self.templates.get_template(template).render(**context)

    def render(
        self,
        request: Request,
        template: str,
        context: dict[str, Any],
        user: CurrentUser | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        """画面の枠(ヘッダー、左ペイン)を付けて、画面を組み立てる。"""
        page = self.render_text(
            template,
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
            self.set_session_cookie(response, user.token)
        return response
