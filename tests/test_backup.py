"""バックアップと復元の自動テスト(要件定義書 12.2.1 T1-20、BK-01〜BK-04)。"""

import copy
import json

import pytest

from common import backup as backup_module
from common.backup import BackupError, export_backup, export_backup_bytes, parse_backup, restore_backup_bytes
from common.config_reader import get_config_version, get_settings, list_devices
from common.config_writer import save_device, update_settings
from common.validation import DeviceForm

ALL_COMMANDS = {"cmd_counters_brief": True, "cmd_ptp_brief": True, "cmd_ptp_clock": True, "cmd_ptp_parent": True}


def add_devices(db) -> None:
    save_device(db, DeviceForm("NEXUS-TEST-01", "192.0.2.1", dict(ALL_COMMANDS), {"counters": "1/1-3", "ptp": "1/1"}))
    save_device(
        db,
        DeviceForm(
            "NEXUS-TEST-02",
            "192.0.2.2",
            dict(ALL_COMMANDS, cmd_ptp_brief=False, cmd_ptp_parent=False),
            {"counters": "1/48"},
        ),
    )
    update_settings(db, {"zabbix_server": "192.0.2.100", "stale_timeout_sec": "600", "webgui_port": "8081"})


def snapshot(db) -> tuple:
    """設定データの今の状態(比較用)。"""
    devices = [(d.hostname, d.source_ip, d.commands, d.interfaces) for d in list_devices(db)]
    users = db.execute("SELECT id, username, password_hash, role FROM users ORDER BY id").fetchall()
    return devices, get_settings(db), [tuple(u) for u in users]


@pytest.fixture
def valid_backup(db) -> dict:
    add_devices(db)
    return export_backup(db)


# ---------------------------------------------------------------- BK-01、BK-02
def test_bk_01_export_contents(valid_backup):
    """機器の登録内容と全体設定を、JSON形式の1つのファイルにする(BK-01)。"""
    assert valid_backup["format_version"] == 1
    assert valid_backup["exported_at"].endswith("+00:00")
    assert [d["hostname"] for d in valid_backup["devices"]] == ["NEXUS-TEST-01", "NEXUS-TEST-02"]
    first = valid_backup["devices"][0]
    assert first["cmd_counters_brief"] == 1
    assert {"purpose": "counters", "slot": 1, "port": 3} in first["interfaces"]
    assert {"purpose": "ptp", "slot": 1, "port": 1} in first["interfaces"]
    assert valid_backup["settings"]["zabbix_server"] == "192.0.2.100"
    assert valid_backup["settings"]["stale_timeout_sec"] == "600"


def test_bk_02_excluded_items(valid_backup, db):
    """ユーザー、セッション、変更履歴、状態データ、WebGUIのポート番号、設定の版番号を含めない(BK-02)。"""
    assert set(valid_backup) == {"format_version", "exported_at", "devices", "settings"}
    assert "webgui_port" not in valid_backup["settings"]
    assert "config_version" not in valid_backup["settings"]
    text = export_backup_bytes(db).decode("utf-8")
    for word in ("admin", "password", "scrypt", "token", "audit", "device_status"):
        assert word not in text
    json.loads(text)


# ---------------------------------------------------------------- T1-20 正しいファイル
def test_t1_20_valid_file_accepted(valid_backup):
    """正しいファイルが受け付けられる(BK-04)。"""
    content = parse_backup(json.dumps(valid_backup).encode("utf-8"))
    assert [d.hostname for d in content.devices] == ["NEXUS-TEST-01", "NEXUS-TEST-02"]
    assert content.settings["zabbix_server"] == "192.0.2.100"


def test_t1_20_valid_file_with_bom_accepted(valid_backup):
    raw = b"\xef\xbb\xbf" + json.dumps(valid_backup).encode("utf-8")
    assert len(parse_backup(raw).devices) == 2


def test_bk_03_restore_replaces_devices_and_settings(db, valid_backup):
    """バックアップを読み込み、機器の登録内容と全体設定を置き換える(BK-03)。"""
    raw = json.dumps(valid_backup).encode("utf-8")
    before_users = db.execute("SELECT id, username, password_hash FROM users ORDER BY id").fetchall()

    # 機器をすべて削除し、設定を変えてから復元する
    for device in list_devices(db):
        db.execute("DELETE FROM devices WHERE id = ?", (device.id,))
    update_settings(db, {"zabbix_server": "192.0.2.200", "stale_timeout_sec": "60"})
    save_device(db, DeviceForm("NEXUS-TEST-09", "192.0.2.9", {"cmd_ptp_clock": True}, {}))
    version_before = get_config_version(db)

    restore_backup_bytes(db, raw)

    devices = list_devices(db)
    assert [d.hostname for d in devices] == ["NEXUS-TEST-01", "NEXUS-TEST-02"]
    assert [i.ifnum for i in devices[0].interfaces["counters"]] == ["1/1", "1/2", "1/3"]
    assert devices[1].commands["cmd_ptp_brief"] is False
    settings = get_settings(db)
    assert settings["zabbix_server"] == "192.0.2.100"
    assert settings["stale_timeout_sec"] == "600"
    # WebGUIのポート番号とユーザーは変わらない(7.4.7.6)
    assert settings["webgui_port"] == "8081"
    assert db.execute("SELECT id, username, password_hash FROM users ORDER BY id").fetchall() == before_users
    # 設定の版番号は1増える(CF-01)
    assert get_config_version(db) == version_before + 1


# ---------------------------------------------------------------- T1-20 不正なファイル
def _broken_variants(valid: dict) -> dict[str, bytes]:
    def mutate(change) -> bytes:
        document = copy.deepcopy(valid)
        change(document)
        return json.dumps(document, ensure_ascii=False).encode("utf-8")

    def set_device(index: int, key: str, value):
        return lambda d: d["devices"][index].__setitem__(key, value)

    def set_setting(key: str, value):
        return lambda d: d["settings"].__setitem__(key, value)

    return {
        "JSONでない": b"not json",
        "途中で切れている": json.dumps(valid).encode("utf-8")[:100],
        "UTF-8でない": b'{"format_version": "\xff"}',
        "最上位が配列": b"[]",
        "空のオブジェクト": b"{}",
        "大きすぎる": b" " * (5 * 1024 * 1024 + 1),
        "版番号が違う": mutate(lambda d: d.__setitem__("format_version", 2)),
        "版番号が文字列": mutate(lambda d: d.__setitem__("format_version", "1")),
        "版番号が真偽値": mutate(lambda d: d.__setitem__("format_version", True)),
        "日時の形式が違う": mutate(lambda d: d.__setitem__("exported_at", "昨日")),
        "不明な最上位の項目": mutate(lambda d: d.__setitem__("users", [])),
        "devicesがない": mutate(lambda d: d.pop("devices")),
        "devicesが配列でない": mutate(lambda d: d.__setitem__("devices", {})),
        "ホスト名が空": mutate(set_device(0, "hostname", "")),
        "ホスト名に使えない文字": mutate(set_device(0, "hostname", "<script>")),
        "ホスト名がtelemetry": mutate(set_device(0, "hostname", "telemetry")),
        "ホスト名が重複": mutate(set_device(1, "hostname", "NEXUS-TEST-01")),
        "IPアドレスが不正": mutate(set_device(0, "source_ip", "192.0.2.999")),
        "IPアドレスが重複": mutate(set_device(1, "source_ip", "192.0.2.1")),
        "IPアドレスが数値": mutate(set_device(0, "source_ip", 3221225985)),
        "機器の種類が違う": mutate(set_device(0, "device_type", "arista")),
        "コマンドの値が2": mutate(set_device(0, "cmd_ptp_clock", 2)),
        "コマンドの値が真偽値": mutate(set_device(0, "cmd_ptp_clock", True)),
        "コマンドが全部0": mutate(
            lambda d: d["devices"][0].update(
                {"cmd_counters_brief": 0, "cmd_ptp_brief": 0, "cmd_ptp_clock": 0, "cmd_ptp_parent": 0, "interfaces": []}
            )
        ),
        "チェックしたコマンドの番号が空": mutate(
            lambda d: d["devices"][0].__setitem__(
                "interfaces", [i for i in d["devices"][0]["interfaces"] if i["purpose"] != "ptp"]
            )
        ),
        "チェックを外したコマンドに番号": mutate(
            lambda d: d["devices"][1]["interfaces"].append({"purpose": "ptp", "slot": 1, "port": 1})
        ),
        "スロット番号が範囲外": mutate(lambda d: d["devices"][0]["interfaces"][0].__setitem__("slot", 100)),
        "ポート番号が0": mutate(lambda d: d["devices"][0]["interfaces"][0].__setitem__("port", 0)),
        "ポート番号が文字列": mutate(lambda d: d["devices"][0]["interfaces"][0].__setitem__("port", "1")),
        "用途が不明": mutate(lambda d: d["devices"][0]["interfaces"][0].__setitem__("purpose", "nbm")),
        "番号が重複": mutate(lambda d: d["devices"][0]["interfaces"].append({"purpose": "ptp", "slot": 1, "port": 1})),
        "インターフェースの項目が足りない": mutate(lambda d: d["devices"][0]["interfaces"][0].pop("port")),
        "機器の項目が足りない": mutate(lambda d: d["devices"][0].pop("cmd_ptp_parent")),
        "機器の不明な項目": mutate(set_device(0, "id", 1)),
        "設定が足りない": mutate(lambda d: d["settings"].pop("zabbix_port")),
        "不明な設定": mutate(set_setting("unknown", "1")),
        "対象外の設定(webgui_port)": mutate(set_setting("webgui_port", "9090")),
        "対象外の設定(config_version)": mutate(set_setting("config_version", "99")),
        "設定の値が不正": mutate(set_setting("zabbix_port", "70000")),
        "設定の値が数値": mutate(set_setting("zabbix_port", 10051)),
        "ログレベルが不正": mutate(set_setting("log_level", "VERBOSE")),
        "ZabbixのIPアドレスが不正": mutate(set_setting("zabbix_server", "zabbix")),
    }


def test_t1_20_invalid_files_rejected_and_nothing_changes(db, valid_backup):
    """不正なファイルが拒否され、設定が何も変わらない(BK-04)。"""
    before = snapshot(db)
    version = get_config_version(db)
    for name, raw in _broken_variants(valid_backup).items():
        with pytest.raises(BackupError):
            restore_backup_bytes(db, raw)
        assert snapshot(db) == before, f"「{name}」で設定が変わりました"
        assert get_config_version(db) == version, f"「{name}」で版番号が変わりました"


def test_t1_20_one_bad_device_rejects_whole_file(db, valid_backup):
    """1か所でも不正があれば、正しい機器も含めて何も変更しない(8.6.4)。"""
    document = copy.deepcopy(valid_backup)
    document["devices"][0]["hostname"] = "NEXUS-TEST-05"
    document["devices"][1]["source_ip"] = "bad"
    before = snapshot(db)
    with pytest.raises(BackupError) as info:
        restore_backup_bytes(db, json.dumps(document).encode("utf-8"))
    assert any("2台目" in message for message in info.value.errors)
    assert snapshot(db) == before


def test_t1_20_failure_during_restore_rolls_back(db, valid_backup, monkeypatch):
    """書き込みの途中で失敗しても、途中まで置き換わった状態を作らない(8.6.4)。"""
    before = snapshot(db)
    original = backup_module.write_settings

    def failing_write_settings(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("テスト用の失敗")

    monkeypatch.setattr(backup_module, "write_settings", failing_write_settings)
    document = copy.deepcopy(valid_backup)
    document["devices"] = document["devices"][:1]
    document["settings"]["zabbix_server"] = "192.0.2.222"
    with pytest.raises(RuntimeError):
        restore_backup_bytes(db, json.dumps(document).encode("utf-8"))
    assert snapshot(db) == before


def test_t1_20_empty_device_list_is_valid(db, valid_backup):
    """機器が0台のバックアップも、正しいファイルとして受け付ける。"""
    document = copy.deepcopy(valid_backup)
    document["devices"] = []
    restore_backup_bytes(db, json.dumps(document).encode("utf-8"))
    assert list_devices(db) == []
