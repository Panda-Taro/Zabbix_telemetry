"""復旧用コマンドの中身(DL-05、要件定義書 7.4.7.5、OP-07、決定事項 D-030)。

中間サーバーのコンソールで `sudo telemetry-recover ...` と入力すると、tools/telemetry-recover.sh が
web のコンテナのイメージでこのプログラムを動かす(web が止まっていても動く)。

使い方:
    python -m tools.recovery reset-port                 WebGUIのポート番号を既定値(8080)に戻す
    python -m tools.recovery reset-password [ユーザー名]   管理者のパスワードを初期化する(省略時は admin)

本体のIPアドレスは、このコマンドでは直さない。コンソールで、Ubuntuの標準の手順で直す(7.4.7.5)。
"""

import argparse
import sqlite3
import sys
from collections.abc import Sequence

from common.config_writer import insert_audit, seed_initial_data, update_settings
from common.constants import (
    DEFAULT_DB_PATH,
    INITIAL_ACCOUNTS,
    RECOVERY_AUDIT_USER,
    ROLE_ADMIN,
    SETTING_DEFAULTS,
    SETTING_WEBGUI_PORT,
)
from common.database import connect, initialize_schema, transaction
from common.passwords import hash_password
from common.timeutil import utc_now_iso
from web.users import initial_password_for, reset_password

# 管理者が1人もいないときに作り直す管理者のユーザー名(初期アカウントの admin)
DEFAULT_ADMIN = next(name for name, _, role in INITIAL_ACCOUNTS if role == ROLE_ADMIN)


def reset_port(conn: sqlite3.Connection) -> str:
    """WebGUIのポート番号を既定値に戻す。反映には web の起動し直しが必要(呼び出す側で行う)。"""
    default = SETTING_DEFAULTS[SETTING_WEBGUI_PORT]
    update_settings(conn, {SETTING_WEBGUI_PORT: default}, audit_user=RECOVERY_AUDIT_USER, audit_category="system")
    return f"WebGUIのポート番号を、既定値の {default} に戻しました"


def _admins(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute("SELECT username FROM users WHERE role = ? ORDER BY username COLLATE NOCASE", (ROLE_ADMIN,))
    return [row["username"] for row in rows]


def _recreate_admin(conn: sqlite3.Connection) -> str:
    """管理者が1人もいないときに、admin を作り直す(パスワードは admin)。"""
    now = utc_now_iso()
    password_hash = hash_password(initial_password_for(DEFAULT_ADMIN, ROLE_ADMIN))
    with transaction(conn):
        row = conn.execute("SELECT id, role FROM users WHERE username = ? COLLATE NOCASE", (DEFAULT_ADMIN,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, is_initial_password, failed_count, locked_until,"
                " created_at, updated_at) VALUES (?, ?, ?, 1, 0, NULL, ?, ?)",
                (DEFAULT_ADMIN, password_hash, ROLE_ADMIN, now, now),
            )
            insert_audit(
                conn, username=RECOVERY_AUDIT_USER, category="user", action="create", target=DEFAULT_ADMIN,
                after={"username": DEFAULT_ADMIN, "role": ROLE_ADMIN},
            )  # fmt: skip
        else:
            conn.execute(
                "UPDATE users SET password_hash = ?, role = ?, is_initial_password = 1, failed_count = 0,"
                " locked_until = NULL, updated_at = ? WHERE id = ?",
                (password_hash, ROLE_ADMIN, now, row["id"]),
            )
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))
            insert_audit(
                conn, username=RECOVERY_AUDIT_USER, category="user", action="update", target=DEFAULT_ADMIN,
                before={"role": row["role"]}, after={"role": ROLE_ADMIN, "password_reset": True},
            )  # fmt: skip
    return f"管理者が1人もいなかったため、管理者 {DEFAULT_ADMIN} を作り直しました(パスワード:{DEFAULT_ADMIN})"


def reset_admin_password(conn: sqlite3.Connection, username: str) -> tuple[bool, str]:
    """管理者のパスワードを、ユーザー名と同じ文字列に初期化する(7.4.8.4)。(成功したか, 文言) を返す。

    - 管理者が1人もいない場合は、admin を作り直す
    - 指定したユーザーがいない、または管理者でない場合は、管理者の一覧を示して止める
    - 初期化したユーザーのログインは、すべて無効にする
    """
    admins = _admins(conn)
    if not admins:
        return True, _recreate_admin(conn)
    row = conn.execute("SELECT id, username, role FROM users WHERE username = ? COLLATE NOCASE", (username,)).fetchone()
    if row is None or row["role"] != ROLE_ADMIN:
        reason = "見つかりません" if row is None else "管理者ではありません"
        return False, f"ユーザー「{username}」は{reason}。管理者は次のとおりです:{'、'.join(admins)}"
    errors = reset_password(conn, RECOVERY_AUDIT_USER, row["id"])
    if errors:
        return False, "、".join(errors)
    return True, f"管理者 {row['username']} のパスワードを初期化しました(パスワード:{row['username']})。ログイン後に変更してください"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="telemetry-recover", description="Telemetry 中間サーバーの復旧用コマンド")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLiteデータベースのパス(既定値:{DEFAULT_DB_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("reset-port", help="WebGUIのポート番号を既定値(8080)に戻す")
    password = sub.add_parser("reset-password", help="管理者のパスワードを初期化する")
    password.add_argument("username", nargs="?", default=DEFAULT_ADMIN, help=f"ユーザー名(省略時は {DEFAULT_ADMIN})")
    args = parser.parse_args(argv)

    conn = connect(args.db)
    try:
        initialize_schema(conn)
        seed_initial_data(conn)
        if args.command == "reset-port":
            print(reset_port(conn))
            return 0
        ok, message = reset_admin_password(conn, args.username)
        print(message, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
