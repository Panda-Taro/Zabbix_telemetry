"""復旧用コマンドと、WebGUIのポート番号の切り替えの自動テスト(OP-07、DL-05、決定事項 D-029、D-030)。"""

import logging
import socket

import pytest

from common.config_reader import get_settings
from common.config_writer import seed_initial_data, update_settings
from common.constants import RECOVERY_AUDIT_USER
from common.database import connect, initialize_schema
from common.passwords import hash_password, verify_password
from tests.web_support import free_tcp_port
from tools import recovery
from web.__main__ import open_listen_socket


@pytest.fixture
def conn(tmp_path):
    db = connect(tmp_path / "telemetry.db")
    initialize_schema(db)
    seed_initial_data(db)
    try:
        yield db
    finally:
        db.close()


def _user(conn, username):
    return conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()


def _audit(conn):
    return [dict(r) for r in conn.execute("SELECT username, category, action, target FROM audit_log ORDER BY id")]


def _add_user(conn, username, role, password="secret"):
    conn.execute(
        "INSERT INTO users (username, password_hash, role, is_initial_password, failed_count, locked_until, created_at, updated_at)"
        " VALUES (?, ?, ?, 0, 0, NULL, '2027-01-01T00:00:00+00:00', '2027-01-01T00:00:00+00:00')",
        (username, hash_password(password), role),
    )
    conn.commit()


def test_reset_port(conn):
    update_settings(conn, {"webgui_port": "9443"})
    assert "8080" in recovery.reset_port(conn)
    assert get_settings(conn)["webgui_port"] == "8080"
    assert _audit(conn)[-1] == {"username": RECOVERY_AUDIT_USER, "category": "system", "action": "update", "target": "webgui_port"}


def test_reset_password_of_admin_invalidates_sessions(conn):
    admin = _user(conn, "admin")
    conn.execute("UPDATE users SET password_hash = ?, is_initial_password = 0, failed_count = 5,"
                 " locked_until = '2099-01-01T00:00:00+00:00' WHERE id = ?", (hash_password("changed"), admin["id"]))  # fmt: skip
    conn.execute("INSERT INTO sessions (token_hash, user_id, created_at) VALUES ('t', ?, 'x')", (admin["id"],))
    conn.commit()
    ok, message = recovery.reset_admin_password(conn, "admin")
    assert ok and "初期化しました" in message
    row = _user(conn, "admin")
    assert verify_password("admin", row["password_hash"]) and row["is_initial_password"] == 1
    assert row["locked_until"] is None and row["failed_count"] == 0
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (admin["id"],)).fetchone()[0] == 0
    assert _audit(conn)[-1]["username"] == RECOVERY_AUDIT_USER


def test_reset_password_of_named_admin(conn):
    _add_user(conn, "ops", "admin")
    ok, _ = recovery.reset_admin_password(conn, "OPS")  # 大文字・小文字は区別しない
    assert ok and verify_password("ops", _user(conn, "ops")["password_hash"])


@pytest.mark.parametrize(("username", "reason"), [("user", "管理者ではありません"), ("nobody", "見つかりません")])
def test_reset_password_refuses_non_admins_and_lists_admins(conn, username, reason):
    _add_user(conn, "ops", "admin")
    ok, message = recovery.reset_admin_password(conn, username)
    assert not ok and reason in message and "admin、ops" in message
    assert verify_password("", _user(conn, "user")["password_hash"])  # 何も変えていない


def test_recreates_admin_when_no_admin_exists(conn):
    conn.execute("DELETE FROM users WHERE username = 'admin'")
    conn.commit()
    ok, message = recovery.reset_admin_password(conn, "user")
    assert ok and "作り直しました" in message
    row = _user(conn, "admin")
    assert row["role"] == "admin" and verify_password("admin", row["password_hash"])
    assert _user(conn, "user")["role"] == "viewer"


def test_promotes_existing_admin_named_user_when_no_admin_exists(conn):
    conn.execute("UPDATE users SET role = 'viewer' WHERE username = 'admin'")
    conn.commit()
    ok, _ = recovery.reset_admin_password(conn, "admin")
    row = _user(conn, "admin")
    assert ok and row["role"] == "admin" and verify_password("admin", row["password_hash"])


def test_main_returns_error_code(tmp_path, capsys):
    db = str(tmp_path / "telemetry.db")
    assert recovery.main(["--db", db, "reset-port"]) == 0
    assert recovery.main(["--db", db, "reset-password"]) == 0
    assert recovery.main(["--db", db, "reset-password", "user"]) == 1
    assert "管理者ではありません" in capsys.readouterr().err


# ======================================================================
# WebGUIのポート番号が使えなかったときの切り替え(決定事項 D-029)
# ======================================================================


def test_open_listen_socket_uses_saved_port(caplog):
    port = free_tcp_port()
    sock, actual = open_listen_socket("127.0.0.1", port, logging.getLogger("test"), fallback_port=free_tcp_port())
    sock.close()
    assert actual == port


def test_open_listen_socket_falls_back_and_logs(caplog):
    busy_port, fallback = free_tcp_port(), free_tcp_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", busy_port))
        busy.listen()
        with caplog.at_level(logging.ERROR):
            sock, actual = open_listen_socket("127.0.0.1", busy_port, logging.getLogger("test"), fallback_port=fallback)
        sock.close()
    assert actual == fallback
    assert f"{busy_port} で待ち受けられませんでした" in caplog.text and f"既定値の {fallback} で起動します" in caplog.text


def test_open_listen_socket_fails_when_fallback_is_also_busy():
    port = free_tcp_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", port))
        busy.listen()
        with pytest.raises(OSError):
            open_listen_socket("127.0.0.1", port, logging.getLogger("test"), fallback_port=port)
