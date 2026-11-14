"""ログイン、セッション、パスワード変更(7.4.1、7.4.9、8.3.4、8.3.5、AU-01〜AU-10、LG-05)。

users と sessions は設定データのテーブルで、web だけが書き込む(8.1.3)。
ログイン状態を示す合言葉(セッショントークン)は、ブラウザに渡し、データベースにはハッシュ値だけを保存する。
"""

import hashlib
import hmac
import logging
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from common.config_writer import insert_audit
from common.constants import (
    LOGIN_LOCK_SEC,
    LOGIN_MAX_FAILURES,
    MAX_USERNAME_INPUT_LENGTH,
    ROLE_ADMIN,
)
from common.database import transaction
from common.passwords import hash_password, verify_password
from common.timeutil import utc_now_iso

# ユーザー名が存在しないときにも、同じだけ時間をかけて照合するためのハッシュ値
_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
    return _DUMMY_HASH


@dataclass(frozen=True)
class CurrentUser:
    """ログイン中のユーザー。"""

    id: int
    username: str
    role: str
    is_initial_password: bool
    token: str
    """ブラウザから届いた合言葉(セッショントークン)。"""

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    message: str = ""
    token: str | None = None


# ======================================================================
# 合言葉(セッショントークン)と、不正な操作を防ぐための印(CSRF対策)
# ======================================================================


def token_hash(token: str) -> str:
    """合言葉のハッシュ値(sessions.token_hash に保存する形)。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token_for(session_token: str) -> str:
    """ログイン中の操作に付ける印。合言葉から作るため、他のサイトからは作れない(11.3.3)。"""
    return hmac.new(session_token.encode("utf-8"), b"csrf", hashlib.sha256).hexdigest()


def check_csrf(session_token: str, submitted: str | None) -> bool:
    if not submitted:
        return False
    return hmac.compare_digest(csrf_token_for(session_token), submitted)


def new_login_csrf_token() -> str:
    return secrets.token_urlsafe(32)


# ======================================================================
# セッション(8.3.5)
# ======================================================================


def find_session_user(conn: sqlite3.Connection, token: str | None) -> CurrentUser | None:
    """合言葉から、ログイン中のユーザーを探す。期限は設けない(7.4.1.3、8.3.5.1)。"""
    if not token or len(token) > 256:
        return None
    row = conn.execute(
        "SELECT u.id, u.username, u.role, u.is_initial_password FROM sessions s"
        " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (token_hash(token),),
    ).fetchone()
    if row is None:
        return None
    return CurrentUser(
        id=row["id"],
        username=row["username"],
        role=row["role"],
        is_initial_password=bool(row["is_initial_password"]),
        token=token,
    )


def _create_session(conn: sqlite3.Connection, user_id: int, now: datetime) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at) VALUES (?, ?, ?)",
        (token_hash(token), user_id, utc_now_iso(now)),
    )
    return token


# ======================================================================
# ログインとログアウト(7.4.1、AU-02、AU-05、LG-05)
# ======================================================================


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def login(
    conn: sqlite3.Connection,
    username: str,
    password: str,
    now: datetime,
    logger: logging.Logger,
) -> LoginResult:
    """ユーザー名とパスワードでログインする。

    - 5回続けて失敗したユーザーは、15分間ログインできないようにする(7.4.1.2)
    - ログイン、ログイン失敗を変更履歴に記録する(7.4.6.3)
    """
    entered = username[:MAX_USERNAME_INPUT_LENGTH]
    failure = LoginResult(ok=False, message="ユーザー名またはパスワードが違います")
    row = conn.execute(
        "SELECT id, password_hash, failed_count, locked_until FROM users WHERE username = ?",
        (username,),
    ).fetchone()

    if row is None:
        verify_password(password, _dummy_hash())  # 存在するユーザーと同じだけ時間をかける
        with transaction(conn):
            insert_audit(conn, username=entered, category="auth", action="login_failed", target=entered)
        return failure

    locked_until = _parse_time(row["locked_until"])
    if locked_until is not None and locked_until > now:
        with transaction(conn):
            insert_audit(
                conn, username=entered, category="auth", action="login_failed", target=entered,
                after={"reason": "ログインできない期間中"},
            )  # fmt: skip
        return LoginResult(
            ok=False,
            message="ログインに続けて失敗したため、しばらくログインできません。時間をおいてから、やり直してください",
        )

    if not verify_password(password, row["password_hash"]):
        failed_count = row["failed_count"] + 1
        new_locked_until = None
        if failed_count >= LOGIN_MAX_FAILURES:
            new_locked_until = utc_now_iso(now + timedelta(seconds=LOGIN_LOCK_SEC))
            failed_count = 0
            logger.warning(
                "ユーザー「%s」がログインに%d回続けて失敗したため、%d分間ログインできないようにしました",
                entered,
                LOGIN_MAX_FAILURES,
                LOGIN_LOCK_SEC // 60,
            )
        with transaction(conn):
            conn.execute(
                "UPDATE users SET failed_count = ?, locked_until = ?, updated_at = ? WHERE id = ?",
                (failed_count, new_locked_until, utc_now_iso(now), row["id"]),
            )
            insert_audit(conn, username=entered, category="auth", action="login_failed", target=entered)
        return failure

    with transaction(conn):
        conn.execute(
            "UPDATE users SET failed_count = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
            (utc_now_iso(now), row["id"]),
        )
        token = _create_session(conn, row["id"], now)
        insert_audit(conn, username=username, category="auth", action="login", target=username)
    return LoginResult(ok=True, token=token)


def logout(conn: sqlite3.Connection, user: CurrentUser) -> None:
    """ログアウトする。このブラウザのセッションを削除し、変更履歴に記録する(8.3.5.1)。"""
    with transaction(conn):
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash(user.token),))
        insert_audit(conn, username=user.username, category="auth", action="logout", target=user.username)


# ======================================================================
# パスワード変更(7.4.9、UI-19、UI-20、AU-10)
# ======================================================================


def change_own_password(
    conn: sqlite3.Connection,
    user: CurrentUser,
    current_password: str,
    new_password: str,
    new_password_confirm: str,
    now: datetime,
) -> list[str]:
    """ログイン中のユーザーが、自分のパスワードを変更する。問題があればエラーの文言の一覧を返す。

    - 現在のパスワードの入力を必須とする(7.4.9.2)
    - 管理者のパスワードは空欄にできない。閲覧者は空欄を許す(7.4.9.3)
    - 変更したら、そのユーザーの他のログイン(セッション)を無効にする(AU-10)
    - 変更した事実だけを変更履歴に記録し、パスワードそのものは記録しない(8.3.6.1)
    """
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user.id,)).fetchone()
    if row is None:
        return ["ユーザーが見つかりません"]
    errors: list[str] = []
    if not verify_password(current_password, row["password_hash"]):
        errors.append("現在のパスワードが違います")
    if new_password != new_password_confirm:
        errors.append("新しいパスワードと、確認のために入力したパスワードが一致しません")
    if user.is_admin and new_password == "":
        errors.append("管理者のパスワードは空欄にできません")
    if errors:
        return errors

    with transaction(conn):
        conn.execute(
            "UPDATE users SET password_hash = ?, is_initial_password = 0, updated_at = ? WHERE id = ?",
            (hash_password(new_password), utc_now_iso(now), user.id),
        )
        conn.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (user.id, token_hash(user.token)),
        )
        insert_audit(
            conn, username=user.username, category="user", action="update", target=user.username,
            after={"password_changed": True},
        )  # fmt: skip
    return []
