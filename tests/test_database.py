"""データベースと設定データの読み書きの自動テスト(要件定義書 12.2 T1-19、T1-23、CF-01、CF-06、AU-07)。"""

import ast
import sqlite3
from pathlib import Path

import pytest

from common.config_reader import get_config_version, get_device, get_settings, list_devices
from common.config_writer import (
    delete_device,
    insert_audit,
    record_audit,
    save_device,
    seed_initial_data,
    update_settings,
)
from common.constants import AUDIT_LOG_MAX_ROWS, SETTING_DEFAULTS
from common.database import CONFIG_TABLES, STATUS_TABLES, transaction
from common.passwords import hash_password, verify_password
from common.validation import DeviceForm, ValidationError

REPO_ROOT = Path(__file__).resolve().parent.parent

ALL_COMMANDS = {"cmd_counters_brief": True, "cmd_ptp_brief": True, "cmd_ptp_clock": True, "cmd_ptp_parent": True}


def device_form(hostname: str = "NEXUS-TEST-01", source_ip: str = "192.0.2.1", **overrides) -> DeviceForm:
    values = {
        "hostname": hostname,
        "source_ip": source_ip,
        "commands": dict(ALL_COMMANDS),
        "interfaces_text": {"counters": "1/1-4", "ptp": "1/1"},
    }
    values.update(overrides)
    return DeviceForm(**values)


# ---------------------------------------------------------------- テーブルの作成
def test_all_tables_are_created(db):
    """⑧のすべてのテーブルが、⑧のとおりの名前で作られる。"""
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert set(CONFIG_TABLES) | set(STATUS_TABLES) <= names
    assert set(CONFIG_TABLES) | set(STATUS_TABLES) == {
        "devices", "device_interfaces", "settings", "users", "sessions", "audit_log",
        "device_status", "system_status", "unknown_sources",
    }  # fmt: skip


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("devices", ["id", "hostname", "source_ip", "device_type", "cmd_counters_brief", "cmd_ptp_brief",
                     "cmd_ptp_clock", "cmd_ptp_parent", "created_at", "updated_at"]),
        ("device_interfaces", ["id", "device_id", "purpose", "slot", "port"]),
        ("settings", ["key", "value", "updated_at"]),
        ("users", ["id", "username", "password_hash", "role", "is_initial_password", "failed_count",
                   "locked_until", "created_at", "updated_at"]),
        ("sessions", ["token_hash", "user_id", "created_at"]),
        ("audit_log", ["id", "timestamp", "username", "category", "action", "target", "before_value", "after_value"]),
        ("device_status", ["device_id", "status", "last_received_at", "last_counters_brief_at", "last_ptp_brief_at",
                           "last_ptp_clock_at", "last_ptp_parent_at", "mismatch_interfaces", "updated_at"]),
        ("system_status", ["id", "collector_heartbeat_at", "collector_started_at", "applied_config_version",
                           "config_error", "zabbix_connected", "received_per_min", "sent_ok_per_min",
                           "sent_failed_per_min", "dropped_per_min", "skipped_per_min", "queue_size"]),
        ("unknown_sources", ["source_ip", "node_id", "count", "last_received_at"]),
    ],
)  # fmt: skip
def test_column_names_match_requirements(db, table, columns):
    """列名が⑧のとおりである(1文字も変えない)。"""
    assert [row["name"] for row in db.execute(f"PRAGMA table_info({table})")] == columns


def test_wal_mode(db):
    """SQLiteをWALモードで使う(4.2)。"""
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_settings_defaults(db):
    """全体設定の既定値が登録される(8.3.3.2)。"""
    assert get_settings(db) == SETTING_DEFAULTS
    assert get_config_version(db) == 1


def test_seed_does_not_overwrite(db):
    """2回目以降の初期化で、登録済みの設定やユーザーを変えない(4.5.4)。"""
    update_settings(db, {"zabbix_port": "10052"})
    db.execute("UPDATE users SET is_initial_password = 0 WHERE username = 'admin'")
    seed_initial_data(db)
    assert get_settings(db)["zabbix_port"] == "10052"
    assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    assert db.execute("SELECT is_initial_password FROM users WHERE username = 'admin'").fetchone()[0] == 0


# ---------------------------------------------------------------- AU-07
def test_au_07_initial_accounts(db):
    """初回起動時に、初期アカウント(admin/admin、user/空欄)を作る(AU-07)。"""
    rows = {row["username"]: row for row in db.execute("SELECT * FROM users")}
    assert set(rows) == {"admin", "user"}
    assert rows["admin"]["role"] == "admin"
    assert rows["user"]["role"] == "viewer"
    assert verify_password("admin", rows["admin"]["password_hash"])
    assert verify_password("", rows["user"]["password_hash"])
    for row in rows.values():
        assert row["is_initial_password"] == 1
        assert row["failed_count"] == 0
        assert row["locked_until"] is None


# ---------------------------------------------------------------- T1-19
def test_t1_19_password_is_not_stored_as_is(db):
    """パスワードが、入力されたままの文字列で保存されていない(AU-09)。"""
    for username, password in (("admin", "admin"), ("user", "")):
        stored = db.execute("SELECT password_hash FROM users WHERE username = ?", (username,)).fetchone()[0]
        assert stored != password
        assert stored.startswith("scrypt$")
        if password:
            assert password not in stored.split("$", 4)[-1]
        assert verify_password(password, stored)
        assert not verify_password(password + "x", stored)


def test_t1_19_hash_uses_salt():
    """同じパスワードでも、ハッシュ化した結果は毎回異なる(ソルトを使う)。"""
    first, second = hash_password("secret"), hash_password("secret")
    assert first != second
    assert verify_password("secret", first) and verify_password("secret", second)


@pytest.mark.parametrize("broken", ["", "admin", "scrypt$1$2$3", "plain$16384$8$1$AAAA$AAAA", "scrypt$16384$8$1$!!$!!",
                                    "scrypt$1048576$8$1$AAAA$AAAA"])
def test_t1_19_broken_hash_is_rejected(broken):
    assert verify_password("admin", broken) is False


# ---------------------------------------------------------------- 機器の保存と CF-01
def test_save_and_read_device(db):
    device_id = save_device(db, device_form())
    record = get_device(db, device_id)
    assert record is not None
    assert record.hostname == "NEXUS-TEST-01"
    assert record.device_type == "cisco_nexus"
    assert record.commands == ALL_COMMANDS
    # 範囲指定は1件ずつに展開して保存する(8.3.2.2)
    assert [i.ifnum for i in record.interfaces["counters"]] == ["1/1", "1/2", "1/3", "1/4"]
    assert [i.ifnum for i in record.interfaces["ptp"]] == ["1/1"]
    assert db.execute("SELECT COUNT(*) FROM device_interfaces WHERE device_id = ?", (device_id,)).fetchone()[0] == 5


def test_cf_01_version_increments_on_each_change(db):
    """設定データを変更するたびに、設定の版番号を1増やす(CF-01)。"""
    assert get_config_version(db) == 1
    device_id = save_device(db, device_form())
    assert get_config_version(db) == 2
    save_device(db, device_form(interfaces_text={"counters": "1/1", "ptp": "1/2"}), device_id=device_id)
    assert get_config_version(db) == 3
    update_settings(db, {"zabbix_server": "192.0.2.100"})
    assert get_config_version(db) == 4
    assert delete_device(db, device_id) is True
    assert get_config_version(db) == 5


def test_cf_01_version_unchanged_when_validation_fails(db):
    save_device(db, device_form())
    with pytest.raises(ValidationError):
        save_device(db, device_form(hostname="NEXUS-TEST-01", source_ip="192.0.2.9"))
    assert get_config_version(db) == 2
    assert len(list_devices(db)) == 1


def test_edit_keeps_purposes_independent(db):
    """一方の用途の変更が、他方に影響しない(6.3.4、8.3.2.4)。"""
    device_id = save_device(db, device_form())
    save_device(db, device_form(interfaces_text={"counters": "1/10", "ptp": "1/1"}), device_id=device_id)
    record = get_device(db, device_id)
    assert [i.ifnum for i in record.interfaces["counters"]] == ["1/10"]
    assert [i.ifnum for i in record.interfaces["ptp"]] == ["1/1"]


def test_delete_device_removes_interfaces(db):
    """機器を削除したら、監視対象インターフェースも一緒に削除する(8.3.2)。"""
    device_id = save_device(db, device_form())
    delete_device(db, device_id)
    assert db.execute("SELECT COUNT(*) FROM device_interfaces").fetchone()[0] == 0
    assert delete_device(db, device_id) is False


def test_device_ids_are_not_reused(db):
    first = save_device(db, device_form())
    delete_device(db, first)
    second = save_device(db, device_form())
    assert second != first


def test_edit_missing_device(db):
    with pytest.raises(LookupError):
        save_device(db, device_form(), device_id=999)


def test_failed_transaction_changes_nothing(db):
    """途中で失敗した書き込みは、すべて取り消される。"""
    with pytest.raises(RuntimeError):
        with transaction(db):
            db.execute("UPDATE settings SET value = '1' WHERE key = 'zabbix_port'")
            raise RuntimeError("テスト用の失敗")
    assert get_settings(db)["zabbix_port"] == "10051"


def test_config_version_cannot_be_written_directly(db):
    with pytest.raises(ValueError):
        update_settings(db, {"config_version": "100"})
    assert get_config_version(db) == 1


# ---------------------------------------------------------------- T1-23
def test_t1_23_audit_log_trimmed(db):
    """変更履歴が上限を超えたら、古いものから削除される(LG-06)。少ない件数(5件)に置き換えて確かめる。"""
    for number in range(1, 9):
        record_audit(db, username="admin", category="settings", action="update", target=f"item{number}", max_rows=5)
    targets = [row[0] for row in db.execute("SELECT target FROM audit_log ORDER BY id")]
    assert targets == ["item4", "item5", "item6", "item7", "item8"]


def test_t1_23_default_limit_is_100000():
    assert AUDIT_LOG_MAX_ROWS == 100_000


def test_audit_log_with_change(db):
    """設定の変更と同じトランザクションで、変更履歴を記録できる。"""
    with transaction(db):
        insert_audit(
            db, username="admin", category="device", action="create", target="NEXUS-TEST-01",
            before=None, after={"hostname": "NEXUS-TEST-01"},
        )  # fmt: skip
    row = db.execute("SELECT * FROM audit_log").fetchone()
    assert row["before_value"] is None
    assert row["after_value"] == '{"hostname": "NEXUS-TEST-01"}'
    assert row["timestamp"].endswith("+00:00")


@pytest.mark.parametrize("field", ["password", "password_hash"])
def test_audit_log_never_contains_password(db, field):
    """パスワードやハッシュ値は、変更履歴に含めない(8.3.6.1)。"""
    with pytest.raises(ValueError):
        record_audit(db, username="admin", category="user", action="update", target="user", after={field: "x"})
    assert db.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0


@pytest.mark.parametrize(("category", "action"), [("unknown", "update"), ("device", "unknown")])
def test_audit_log_rejects_unknown_kinds(db, category, action):
    with pytest.raises(ValueError):
        record_audit(db, username="admin", category=category, action=action)


# ---------------------------------------------------------------- CF-06
def _imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_cf_06_collector_does_not_use_config_writer():
    """collector は、設定データを書き込む部品(config_writer、backup)を使わない(CF-06)。"""
    forbidden = {"common.config_writer", "common.backup"}
    for path in (REPO_ROOT / "collector").rglob("*.py"):
        assert not (_imported_modules(path) & forbidden), f"{path} が設定データを書き込む部品を読み込んでいます"


def test_cf_06_config_reader_has_no_writes():
    """設定データを読む部品(config_reader)には、書き込む処理がない(CF-06)。"""
    source = (REPO_ROOT / "common" / "config_reader.py").read_text(encoding="utf-8").upper()
    for keyword in ("INSERT ", "UPDATE ", "DELETE ", "REPLACE "):
        assert keyword not in source


def test_cf_06_web_writer_does_not_touch_status_tables():
    """設定データを書き込む部品は、状態データのテーブルに書き込まない(CF-06)。"""
    for name in ("config_writer.py", "backup.py"):
        source = (REPO_ROOT / "common" / name).read_text(encoding="utf-8")
        for table in STATUS_TABLES:
            assert table not in source, f"{name} が状態データのテーブル {table} に触れています"


def test_cf_06_deleting_device_does_not_touch_status(db):
    """機器を削除しても、状態データ(device_status)の行は web からは消えない(collector が片付ける)。"""
    device_id = save_device(db, device_form())
    db.execute(
        "INSERT INTO device_status (device_id, status, updated_at) VALUES (?, 'waiting', '2026-01-01T00:00:00+00:00')",
        (device_id,),
    )
    delete_device(db, device_id)
    assert db.execute("SELECT COUNT(*) FROM device_status").fetchone()[0] == 1


def test_foreign_keys_enabled(db):
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO device_interfaces (device_id, purpose, slot, port) VALUES (999, 'ptp', 1, 1)")
