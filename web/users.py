"""ユーザー管理(7.4.8、UI-17、UI-18、UI-22、UI-23、LG-05)。

users と sessions は設定データのテーブルで、web だけが書き込む(8.1.3)。
"""

import re
import sqlite3
from dataclasses import dataclass

from common.config_writer import insert_audit
from common.constants import ROLE_ADMIN, ROLE_VIEWER, USERNAME_PATTERN
from common.database import transaction
from common.passwords import hash_password, verify_password
from common.timeutil import utc_now_iso
from web.auth import CurrentUser

ROLE_LABELS = {ROLE_ADMIN: "管理者", ROLE_VIEWER: "閲覧者"}
_USERNAME_RE = re.compile(USERNAME_PATTERN, re.ASCII)


@dataclass(frozen=True)
class UserRow:
    id: int
    username: str
    role: str
    is_initial_password: bool
    locked_until: str | None
    created_at: str

    @property
    def role_label(self) -> str:
        return ROLE_LABELS.get(self.role, self.role)


def list_users(conn: sqlite3.Connection) -> list[UserRow]:
    return [
        UserRow(
            id=row["id"],
            username=row["username"],
            role=row["role"],
            is_initial_password=bool(row["is_initial_password"]),
            locked_until=row["locked_until"],
            created_at=row["created_at"],
        )
        for row in conn.execute("SELECT * FROM users ORDER BY username COLLATE NOCASE, id")
    ]


def get_user(conn: sqlite3.Connection, user_id: int) -> UserRow | None:
    return next((u for u in list_users(conn) if u.id == user_id), None)


def _admin_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM users WHERE role = ?", (ROLE_ADMIN,)).fetchone()[0]


def _has_empty_password(conn: sqlite3.Connection, user_id: int) -> bool:
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
    return row is not None and verify_password("", row["password_hash"])


def create_user(conn: sqlite3.Connection, actor: CurrentUser, username: str, role: str, password: str) -> list[str]:
    """ユーザーを追加する。パスワードはその場で入力する(7.4.8.5)。問題があればエラーの文言の一覧を返す。"""
    username = username.strip()
    errors: list[str] = []
    if _USERNAME_RE.fullmatch(username) is None:
        errors.append("ユーザー名は、半角の英数字、ピリオド(.)、ハイフン(-)、アンダースコア(_)で、1〜32文字で入力してください")
    if role not in ROLE_LABELS:
        errors.append("権限を選んでください")
    if role == ROLE_ADMIN and password == "":
        errors.append("管理者のパスワードは空欄にできません")
    if errors:
        return errors
    now = utc_now_iso()
    password_hash = hash_password(password)
    with transaction(conn):
        exists = conn.execute("SELECT 1 FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
        if exists:
            return ["このユーザー名は、すでに使われています"]
        conn.execute(
            "INSERT INTO users (username, password_hash, role, is_initial_password, failed_count, locked_until,"
            " created_at, updated_at) VALUES (?, ?, ?, 0, 0, NULL, ?, ?)",
            (username, password_hash, role, now, now),
        )
        insert_audit(
            conn, username=actor.username, category="user", action="create", target=username,
            after={"username": username, "role": role},
        )  # fmt: skip
    return []


def change_role(conn: sqlite3.Connection, actor: CurrentUser, user_id: int, role: str) -> list[str]:
    """権限を変える。管理者が1人もいなくなる変更と、パスワードが空欄の閲覧者を管理者にする変更は拒否する。"""
    if role not in ROLE_LABELS:
        return ["権限を選んでください"]
    with transaction(conn):
        user = get_user(conn, user_id)
        if user is None:
            return ["ユーザーが見つかりません"]
        if user.role == role:
            return []
        if user.role == ROLE_ADMIN and role != ROLE_ADMIN and _admin_count(conn) <= 1:
            return ["管理者が1人もいなくなるため、変更できません"]  # 7.4.8.2
        if role == ROLE_ADMIN and _has_empty_password(conn, user_id):
            return ["パスワードが空欄のユーザーは、管理者にできません。先にパスワードを設定してください"]  # 7.4.8.6
        conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, utc_now_iso(), user_id))
        insert_audit(
            conn, username=actor.username, category="user", action="update", target=user.username,
            before={"role": user.role}, after={"role": role},
        )  # fmt: skip
    return []


def delete_user(conn: sqlite3.Connection, actor: CurrentUser, user_id: int) -> list[str]:
    """ユーザーを削除する。自分自身と、最後の管理者は削除できない(7.4.8.2、7.4.8.3)。"""
    if user_id == actor.id:
        return ["ログイン中の自分自身は、削除できません"]
    with transaction(conn):
        user = get_user(conn, user_id)
        if user is None:
            return ["ユーザーが見つかりません"]
        if user.role == ROLE_ADMIN and _admin_count(conn) <= 1:
            return ["管理者が1人もいなくなるため、削除できません"]
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))  # セッションも一緒に削除される
        insert_audit(
            conn, username=actor.username, category="user", action="delete", target=user.username,
            before={"username": user.username, "role": user.role},
        )  # fmt: skip
    return []


def initial_password_for(username: str, role: str) -> str:
    """初期化したときのパスワード。管理者はユーザー名と同じ文字列、閲覧者は空欄(7.4.8.4)。"""
    return username if role == ROLE_ADMIN else ""


def reset_password(conn: sqlite3.Connection, actor_name: str, user_id: int) -> list[str]:
    """パスワードを初期化する(7.4.8.4、UI-22)。actor_name は、変更履歴に記録する操作した人の名前。

    初期化したユーザーは「初期パスワードのまま」として扱い、ログイン中のセッションを無効にする(AU-10)。
    ログインできない期間中だった場合は、それも解除する。復旧用コマンド(7.4.7.5)からも使う。
    """
    user = get_user(conn, user_id)
    if user is None:
        return ["ユーザーが見つかりません"]
    password_hash = hash_password(initial_password_for(user.username, user.role))
    with transaction(conn):
        conn.execute(
            "UPDATE users SET password_hash = ?, is_initial_password = 1, failed_count = 0, locked_until = NULL,"
            " updated_at = ? WHERE id = ?",
            (password_hash, utc_now_iso(), user_id),
        )
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        insert_audit(
            conn, username=actor_name, category="user", action="update", target=user.username,
            after={"password_reset": True},
        )  # fmt: skip
    return []
