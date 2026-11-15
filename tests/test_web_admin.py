"""管理者用の画面の自動テスト(段階6)。

- 画面からの操作が、段階2で作った検査の処理を通ること
- 設定の変更が、変更履歴に記録されること
UI-07〜UI-13、UI-17、UI-18、UI-21〜UI-23、LG-05、LG-07〜LG-09、BK-01〜BK-05 を確かめる。
"""

import csv
import io
import json
import re
import socket
import struct
import threading
from pathlib import Path

import pytest

from common.backup import export_backup
from common.config_reader import get_config_version, get_settings, list_devices
from common.database import connect
from common.passwords import verify_password
from tests.web_support import Browser, FakeNow, WebServer

ALL_ON = {"cmd_counters_brief": "1", "cmd_ptp_brief": "1", "cmd_ptp_clock": "1", "cmd_ptp_parent": "1"}


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "telemetry.db"
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    now = FakeNow()
    server = WebServer(db, now, log_dir)
    server.start()
    admin = Browser(server.base_url)
    admin.login("admin", "admin")
    try:
        yield server, db, now, admin, log_dir
    finally:
        server.stop()


def query(db: Path, sql: str, params: tuple = ()) -> list[dict]:
    conn = connect(db)
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def audits(db: Path) -> list[dict]:
    return query(db, "SELECT * FROM audit_log WHERE category != 'auth' ORDER BY id")


def csrf_of(client: Browser, path: str = "/dashboard") -> str:
    return client.get(path).csrf()


def device_form(csrf: str, **overrides: str) -> dict[str, str]:
    data = {
        "csrf": csrf,
        "hostname": "NEXUS-TEST-01",
        "source_ip": "192.0.2.1",
        **ALL_ON,
        "interfaces_counters": "1/1-4, 1/48",
        "interfaces_ptp": "1/1",
    }
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def create_device(client: Browser, **overrides: str):  # noqa: ANN201
    return client.post("/devices/new", device_form(csrf_of(client, "/devices/new"), **overrides))


# ======================================================================
# 機器(UI-07〜UI-11、UI-21)
# ======================================================================


def test_ui_08_register_device(env):
    """機器を登録できる。4コマンドと2種類のインターフェース番号を別々に設定できる(UI-08、T2-04)。"""
    server, db, now, admin, _ = env
    page = create_device(admin, cmd_ptp_parent=None, interfaces_ptp="1/3")
    assert page.status == 303
    assert page.location.endswith("/devices?message=saved")
    devices = list_devices(connect(db))
    assert len(devices) == 1
    device = devices[0]
    assert device.hostname == "NEXUS-TEST-01"
    assert device.commands["cmd_ptp_parent"] is False
    assert [i.ifnum for i in device.interfaces["counters"]] == ["1/1", "1/2", "1/3", "1/4", "1/48"]
    assert [i.ifnum for i in device.interfaces["ptp"]] == ["1/3"]
    assert "保存しました" in admin.get("/devices?message=saved").body


@pytest.mark.parametrize(
    ("overrides", "expected_text"),
    [
        ({"hostname": ""}, "ホスト名を入力してください"),
        ({"hostname": "NEXUS/01"}, "ホスト名に使えるのは"),
        ({"hostname": "Telemetry"}, "本システム自身のZabbixホスト名"),
        ({"source_ip": "192.0.2.999"}, "IPアドレスの形式が正しくありません"),
        ({"source_ip": "127.0.0.1"}, "機器の送信元としては使えません"),
        ({"interfaces_counters": "1/49/1"}, "形式が正しくありません"),
        ({"interfaces_counters": "1/4-1"}, "小さい番号から大きい番号の順"),
        ({"interfaces_counters": "1/1, 1/1"}, "重複しています"),
        ({"interfaces_counters": "100/1"}, "スロット番号は 1〜99"),
        ({"interfaces_ptp": ""}, "インターフェース番号を入力してください"),
        ({"cmd_counters_brief": None, "cmd_ptp_brief": None, "cmd_ptp_clock": None, "cmd_ptp_parent": None}, "1つ以上チェック"),
    ],
)
def test_ui_11_invalid_device_is_rejected(env, overrides, expected_text):
    """画面からの機器の登録は、段階2の検査を通り、問題があればエラーを表示して保存しない(UI-11)。"""
    server, db, now, admin, _ = env
    version = get_config_version(connect(db))
    page = create_device(admin, **overrides)
    assert page.status == 400
    assert expected_text in page.body
    assert "保存はしていません" in page.body
    assert list_devices(connect(db)) == []
    assert get_config_version(connect(db)) == version
    assert audits(db) == []


def test_ui_11_duplicates_rejected(env):
    server, db, now, admin, _ = env
    create_device(admin)
    page = create_device(admin, hostname="nexus-test-01", source_ip="192.0.2.2")
    assert page.status == 400 and "他の機器で登録済み" in page.body
    page = create_device(admin, hostname="NEXUS-TEST-02", source_ip="192.0.2.1")
    assert page.status == 400 and "他の機器で登録済み" in page.body
    assert len(list_devices(connect(db))) == 1


def test_ui_11_input_kept_after_error(env):
    """エラーのときは、入力した内容を残して表示する。"""
    server, db, now, admin, _ = env
    page = create_device(admin, hostname="NEXUS-TEST-07", source_ip="bad")
    assert 'value="NEXUS-TEST-07"' in page.body
    assert 'value="1/1-4, 1/48"' in page.body


def test_ui_10_unchecked_inputs_are_disabled(env):
    """チェックを外したコマンドのインターフェース番号の入力欄は、入力できない状態にする(UI-10)。"""
    server, db, now, admin, _ = env
    create_device(admin, cmd_ptp_brief=None, interfaces_ptp="")
    device_id = list_devices(connect(db))[0].id
    body = admin.get(f"/devices/{device_id}/edit").body
    assert re.search(r'id="interfaces_ptp"[^>]*\sdisabled', body)
    assert not re.search(r'id="interfaces_counters"[^>]*\sdisabled', body)
    assert 'data-controls="interfaces_ptp"' in body
    assert '<script src="/static/app.js"' in body


def test_ui_08_edit_device_and_audit(env):
    """機器を編集でき、変更前と変更後の値が変更履歴に記録される(LG-05)。"""
    server, db, now, admin, _ = env
    create_device(admin)
    device_id = list_devices(connect(db))[0].id
    body = admin.get(f"/devices/{device_id}/edit").body
    assert 'value="1/1-4, 1/48"' in body  # 続いた番号は範囲指定にまとめて表示する
    page = admin.post(
        f"/devices/{device_id}/edit",
        device_form(csrf_of(admin), interfaces_counters="1/1-2", cmd_ptp_brief=None, interfaces_ptp="1/1"),
    )
    assert page.status == 303
    device = list_devices(connect(db))[0]
    assert [i.ifnum for i in device.interfaces["counters"]] == ["1/1", "1/2"]
    assert device.interfaces["ptp"] == ()  # チェックを外したコマンドの番号は消す(7.4.4.5)

    create, update = audits(db)
    assert (create["category"], create["action"], create["target"]) == ("device", "create", "NEXUS-TEST-01")
    assert create["before_value"] is None
    assert json.loads(create["after_value"])["interfaces_counters"] == "1/1-4, 1/48"
    assert create["username"] == "admin"
    assert (update["action"], update["target"]) == ("update", "NEXUS-TEST-01")
    assert json.loads(update["before_value"])["interfaces_ptp"] == "1/1"
    after = json.loads(update["after_value"])
    assert after["interfaces_counters"] == "1/1-2"
    assert after["cmd_ptp_brief"] == 0 and after["interfaces_ptp"] == ""


def test_ui_07_list_sort_and_filter(env):
    """機器一覧は、ホスト名で並べ替え・絞り込みができる(UI-07)。"""
    server, db, now, admin, _ = env
    for number, name in enumerate(["NEXUS-TEST-02", "NEXUS-TEST-10", "NEXUS-TEST-01"], start=1):
        create_device(admin, hostname=name, source_ip=f"192.0.2.{number}")
    body = admin.get("/devices").body
    assert re.findall(r"<td>(NEXUS-TEST-\d+)</td>", body) == ["NEXUS-TEST-01", "NEXUS-TEST-02", "NEXUS-TEST-10"]
    body = admin.get("/devices?sort=desc").body
    assert re.findall(r"<td>(NEXUS-TEST-\d+)</td>", body) == ["NEXUS-TEST-10", "NEXUS-TEST-02", "NEXUS-TEST-01"]
    body = admin.get("/devices?q=test-1").body
    assert re.findall(r"<td>(NEXUS-TEST-\d+)</td>", body) == ["NEXUS-TEST-10"]
    assert "帯域 5 / PTP 1" in body  # 監視対象インターフェース数
    assert "不明" in body  # 受信状態(collectorが書き出す前)


def test_ui_21_delete_device_with_confirmation(env):
    """削除するときは確認画面を表示し、確定したら削除して変更履歴に記録する(UI-21、7.4.3.3)。"""
    server, db, now, admin, _ = env
    create_device(admin)
    device_id = list_devices(connect(db))[0].id
    confirm = admin.get(f"/devices/{device_id}/delete")
    assert confirm.status == 200
    assert "削除すると元に戻せません" in confirm.body
    assert len(list_devices(connect(db))) == 1  # 確認画面を開いただけでは消えない
    page = admin.post(f"/devices/{device_id}/delete", {"csrf": confirm.csrf()})
    assert page.status == 303
    assert list_devices(connect(db)) == []
    deleted = audits(db)[-1]
    assert (deleted["action"], deleted["target"], deleted["after_value"]) == ("delete", "NEXUS-TEST-01", None)
    assert json.loads(deleted["before_value"])["source_ip"] == "192.0.2.1"


def test_device_not_found(env):
    server, db, now, admin, _ = env
    assert admin.get("/devices/999/edit").location.endswith("message=notfound")
    assert admin.get("/devices/999/delete").location.endswith("message=notfound")


def test_device_message_is_not_arbitrary_text(env):
    """一覧の上に出す文言は、決まったものだけ(URLに書いた任意の文言は表示しない)。"""
    server, db, now, admin, _ = env
    from urllib.parse import quote

    body = admin.get("/devices?message=" + quote("<b>偽の案内</b>")).body
    assert "偽の案内" not in body


# ======================================================================
# 全体設定と、Zabbix接続テスト(UI-12、UI-13)
# ======================================================================

SETTINGS_FORM = {
    "zabbix_server": "192.0.2.100",
    "zabbix_port": "10051",
    "telemetry_udp_port": "50051",
    "stale_timeout_sec": "300",
    "self_monitor_interval_sec": "60",
    "lld_interval_sec": "3600",
    "queue_max_size": "100000",
    "log_level": "INFO",
}


def post_settings(client: Browser, **overrides: str):  # noqa: ANN201
    return client.post("/settings", {"csrf": csrf_of(client, "/settings"), **SETTINGS_FORM, **overrides})


def test_ui_12_settings_saved_and_recorded(env):
    """全体設定の各項目を変更でき、変わった項目の変更前後が変更履歴に記録される(UI-12、LG-04、LG-05)。"""
    server, db, now, admin, _ = env
    page = admin.get("/settings")
    for label in ("ZabbixサーバーのIPアドレス", "Zabbixサーバーのポート番号", "Telemetryの受信ポート番号(UDP)",
                  "途絶判定時間", "自己監視値の送信間隔", "LLD用データの定期送信間隔", "送信待ちキューの上限", "ログレベル"):  # fmt: skip
        assert label in page.body
    assert "webgui_port" not in page.body  # WebGUIのポート番号はシステム設定の画面で扱う
    version = get_config_version(connect(db))
    page = post_settings(admin, stale_timeout_sec="600", log_level="WARNING")
    assert page.status == 303
    settings = get_settings(connect(db))
    assert settings["zabbix_server"] == "192.0.2.100"
    assert settings["stale_timeout_sec"] == "600"
    assert settings["log_level"] == "WARNING"
    assert get_config_version(connect(db)) == version + 1  # CF-01
    record = audits(db)[-1]
    assert (record["category"], record["action"]) == ("settings", "update")
    assert json.loads(record["before_value"]) == {"log_level": "INFO", "stale_timeout_sec": "300", "zabbix_server": ""}
    assert json.loads(record["after_value"]) == {"log_level": "WARNING", "stale_timeout_sec": "600", "zabbix_server": "192.0.2.100"}
    assert "保存しました" in admin.get(page.location.replace(server.base_url, "")).body


@pytest.mark.parametrize(
    ("overrides", "expected_text"),
    [
        ({"zabbix_server": ""}, "ZabbixサーバーのIPアドレスを入力してください"),
        ({"zabbix_server": "zabbix.example"}, "IPアドレスの形式が正しくありません"),
        ({"zabbix_port": "70000"}, "1〜65535"),
        ({"stale_timeout_sec": "29"}, "30〜3600"),
        ({"self_monitor_interval_sec": "121"}, "10〜120"),
        ({"lld_interval_sec": "299"}, "300〜86400"),
        ({"queue_max_size": "999"}, "1000〜1000000"),
        ({"queue_max_size": "abc"}, "半角の整数"),
        ({"log_level": "TRACE"}, "ログレベルは"),
    ],
)
def test_ui_12_invalid_settings_rejected(env, overrides, expected_text):
    """範囲外の値などは、エラーを表示して保存しない(7.4.5.3)。"""
    server, db, now, admin, _ = env
    before = get_settings(connect(db))
    page = post_settings(admin, **overrides)
    assert page.status == 400
    assert expected_text in page.body
    assert get_settings(connect(db)) == before
    assert audits(db) == []


def test_ui_12_port_change_needs_confirmation(env):
    """受信ポート番号を変えるときは、保存前に、数秒間受信が欠ける旨を警告する(6.7.2、T2-13)。"""
    server, db, now, admin, _ = env
    page = post_settings(admin, telemetry_udp_port="50052")
    assert page.status == 200
    assert "数秒間、受信が欠けます" in page.body
    assert get_settings(connect(db))["telemetry_udp_port"] == "50051"  # まだ保存しない
    hidden = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', page.body))
    assert hidden["confirm"] == "1"
    page = admin.post("/settings", hidden)
    assert page.status == 303
    assert get_settings(connect(db))["telemetry_udp_port"] == "50052"


class _FakeZabbix:
    """Zabbix sender プロトコルで「成功」と応答する、テスト用の小さなサーバー。"""

    def __init__(self) -> None:
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.requests: list[dict] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            header = conn.recv(13)
            length = struct.unpack("<I", header[5:9])[0]
            body = b""
            while len(body) < length:
                body += conn.recv(length - len(body))
            self.requests.append(json.loads(body))
            reply = json.dumps({"response": "success", "info": "processed: 0; failed: 0; total: 0; seconds spent: 0.000010"}).encode()
            conn.sendall(b"ZBXD\x01" + struct.pack("<II", len(reply), 0) + reply)

    def close(self) -> None:
        self.sock.close()


def test_ui_13_zabbix_test_success(env):
    """Zabbix接続テストで、接続できる場合を正しく表示する。値は送らない(UI-13、T2-14)。"""
    server, db, now, admin, _ = env
    fake = _FakeZabbix()
    try:
        page = admin.post(
            "/settings/zabbix-test", {"csrf": csrf_of(admin, "/settings"), "zabbix_server": "127.0.0.1", "zabbix_port": str(fake.port)}
        )
    finally:
        fake.close()
    assert page.status == 200
    assert "接続できました" in page.body and "message-ok" in page.body
    assert fake.requests == [{"request": "sender data", "data": []}]
    assert get_settings(connect(db))["zabbix_server"] == ""  # 接続テストでは保存しない


def test_ui_13_zabbix_test_failure(env):
    """Zabbix接続テストで、接続できない場合を正しく表示する(UI-13、T2-14)。"""
    server, db, now, admin, _ = env
    closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed.bind(("127.0.0.1", 0))
    port = closed.getsockname()[1]
    closed.close()
    page = admin.post("/settings/zabbix-test", {"csrf": csrf_of(admin, "/settings"), "zabbix_server": "127.0.0.1", "zabbix_port": str(port)})
    assert "接続できません" in page.body and "message-error" in page.body
    page = admin.post("/settings/zabbix-test", {"csrf": csrf_of(admin, "/settings"), "zabbix_server": "", "zabbix_port": "10051"})
    assert "入力してください" in page.body
    assert 'hx-post="/settings/zabbix-test"' in admin.get("/settings").body


# ======================================================================
# ユーザー管理(UI-17、UI-18、UI-21〜UI-23)
# ======================================================================


def users_csrf(client: Browser) -> str:
    return csrf_of(client, "/users")


def user_id(db: Path, username: str) -> int:
    return query(db, "SELECT id FROM users WHERE username = ?", (username,))[0]["id"]


def add_user(client: Browser, username: str, role: str, password: str):  # noqa: ANN201
    return client.post("/users/new", {"csrf": users_csrf(client), "username": username, "role": role, "password": password})


def test_ui_17_add_user(env):
    """ユーザーを追加できる。パスワードはその場で入力する(UI-17、UI-22)。"""
    server, db, now, admin, _ = env
    assert add_user(admin, "operator1", "viewer", "").status == 303
    assert add_user(admin, "admin2", "admin", "secret-2").status == 303
    assert Browser(server.base_url).login("operator1", "").status == 303
    assert Browser(server.base_url).login("admin2", "secret-2").status == 303
    rows = {r["username"]: r for r in query(db, "SELECT * FROM users")}
    assert rows["admin2"]["role"] == "admin" and rows["admin2"]["is_initial_password"] == 0
    created = [a for a in audits(db) if a["action"] == "create"]
    assert [json.loads(a["after_value"]) for a in created] == [
        {"role": "viewer", "username": "operator1"},
        {"role": "admin", "username": "admin2"},
    ]
    assert "secret-2" not in json.dumps(audits(db))


@pytest.mark.parametrize(
    ("username", "role", "password", "text"),
    [
        ("admin3", "admin", "", "管理者のパスワードは空欄にできません"),
        ("", "viewer", "", "ユーザー名は"),
        ("ユーザー", "viewer", "", "ユーザー名は"),
        ("a" * 33, "viewer", "", "ユーザー名は"),
        ("ADMIN", "viewer", "", "すでに使われています"),
        ("viewer9", "superuser", "x", "権限を選んでください"),
    ],
)
def test_ui_17_add_user_rejected(env, username, role, password, text):
    server, db, now, admin, _ = env
    page = add_user(admin, username, role, password)
    assert page.status == 400
    assert text in page.body
    assert len(query(db, "SELECT * FROM users")) == 2


def test_ui_17_change_role(env):
    server, db, now, admin, _ = env
    add_user(admin, "operator1", "viewer", "op-pass")
    uid = user_id(db, "operator1")
    page = admin.post(f"/users/{uid}/role", {"csrf": users_csrf(admin), "role": "admin"})
    assert page.status == 303
    assert query(db, "SELECT role FROM users WHERE id = ?", (uid,))[0]["role"] == "admin"
    record = audits(db)[-1]
    assert (json.loads(record["before_value"]), json.loads(record["after_value"])) == ({"role": "viewer"}, {"role": "admin"})


def test_ui_23_viewer_with_empty_password_cannot_become_admin(env):
    """パスワードが空欄の閲覧者を、管理者に変える操作は拒否する(UI-23、T2-33)。"""
    server, db, now, admin, _ = env
    uid = user_id(db, "user")
    page = admin.post(f"/users/{uid}/role", {"csrf": users_csrf(admin), "role": "admin"})
    assert page.status == 400
    assert "パスワードが空欄のユーザーは、管理者にできません" in page.body
    assert query(db, "SELECT role FROM users WHERE id = ?", (uid,))[0]["role"] == "viewer"


def test_ui_18_last_admin_protected(env):
    """最後の管理者の、閲覧者への変更と削除はできない(UI-18、T2-33)。"""
    server, db, now, admin, _ = env
    uid = user_id(db, "admin")
    page = admin.post(f"/users/{uid}/role", {"csrf": users_csrf(admin), "role": "viewer"})
    assert page.status == 400 and "管理者が1人もいなくなる" in page.body
    other = Browser(server.base_url)
    add_user(admin, "admin2", "admin", "secret-2")
    other.login("admin2", "secret-2")
    # admin2 から見て、admin を削除できる(管理者が残るため)が、その後 admin2 は最後の管理者になる
    page = other.post(f"/users/{uid}/delete", {"csrf": users_csrf(other)})
    assert page.status == 303
    uid2 = user_id(db, "admin2")
    page = other.post(f"/users/{uid2}/role", {"csrf": users_csrf(other), "role": "viewer"})
    assert page.status == 400 and "管理者が1人もいなくなる" in page.body


def test_ui_18_cannot_delete_self(env):
    """ログイン中の自分自身は削除できない(UI-18、T2-33)。"""
    server, db, now, admin, _ = env
    add_user(admin, "admin2", "admin", "secret-2")
    uid = user_id(db, "admin")
    page = admin.post(f"/users/{uid}/delete", {"csrf": users_csrf(admin)})
    assert page.status == 400 and "自分自身は、削除できません" in page.body
    assert f'href="/users/{uid}/delete"' not in admin.get("/users").body


def test_ui_21_delete_user_with_confirmation(env):
    server, db, now, admin, _ = env
    add_user(admin, "operator1", "viewer", "")
    uid = user_id(db, "operator1")
    operator = Browser(server.base_url)
    operator.login("operator1", "")
    confirm = admin.get(f"/users/{uid}/delete")
    assert "削除すると元に戻せません" in confirm.body
    assert admin.post(f"/users/{uid}/delete", {"csrf": confirm.csrf()}).status == 303
    assert query(db, "SELECT * FROM users WHERE id = ?", (uid,)) == []
    assert operator.get("/dashboard").status == 303  # 削除したユーザーのログインも無効になる
    record = audits(db)[-1]
    assert (record["action"], record["target"]) == ("delete", "operator1")


@pytest.mark.parametrize(("username", "role", "expected"), [("admin2", "admin", "admin2"), ("operator1", "viewer", "")])
def test_ui_22_reset_password(env, username, role, expected):
    """パスワードの初期化:管理者はユーザー名と同じ文字列、閲覧者は空欄。変更を促す表示を出す(UI-22、T2-33)。"""
    server, db, now, admin, _ = env
    add_user(admin, username, role, "secret-x")
    uid = user_id(db, username)
    target = Browser(server.base_url)
    target.login(username, "secret-x")
    confirm = admin.get(f"/users/{uid}/reset")
    assert "初期化後のパスワードは" in confirm.body
    assert admin.post(f"/users/{uid}/reset", {"csrf": confirm.csrf()}).status == 303
    row = query(db, "SELECT * FROM users WHERE id = ?", (uid,))[0]
    assert verify_password(expected, row["password_hash"])
    assert row["is_initial_password"] == 1
    assert target.get("/dashboard").status == 303  # ログイン中のセッションは無効になる(AU-10)
    relogin = Browser(server.base_url)
    assert relogin.login(username, expected).status == 303
    if role == "admin":
        assert "初期パスワードのまま" in relogin.get("/dashboard").body
    record = audits(db)[-1]
    assert json.loads(record["after_value"]) == {"password_reset": True}
    assert expected == "" or expected not in (record["after_value"] or "")


def test_ui_22_reset_unlocks_user(env):
    server, db, now, admin, _ = env
    for _ in range(5):
        Browser(server.base_url).login("user", "wrong")
    uid = user_id(db, "user")
    assert "ログインできない期間中" in admin.get("/users").body
    admin.post(f"/users/{uid}/reset", {"csrf": users_csrf(admin)})
    assert Browser(server.base_url).login("user", "").status == 303


# ======================================================================
# ログ画面(LG-07〜LG-09)
# ======================================================================


def write_log(log_dir: Path, name: str, entries: list[tuple[str, str, str, str]]) -> None:
    lines = [
        json.dumps({"timestamp": t, "level": level, "component": name.split(".")[0], "hostname": host, "message": msg}, ensure_ascii=False)
        for t, level, host, msg in entries
    ]
    (log_dir / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def logs_ready(env):
    server, db, now, admin, log_dir = env
    write_log(log_dir, "collector.log.1", [
        ("2027-01-14T00:00:00.000+00:00", "INFO", "", "collectorを起動しました"),
        ("2027-01-14T01:00:00.000+00:00", "WARNING", "NEXUS-TEST-01", "機器からの受信が途絶しました"),
    ])  # fmt: skip
    write_log(log_dir, "collector.log", [
        ("2027-01-14T02:00:00.000+00:00", "INFO", "NEXUS-TEST-01", "機器からの受信が復旧しました"),
        ("2027-01-14T03:00:00.000+00:00", "ERROR", "", "<script>alert(1)</script>=1+2"),
        ("2027-01-14T03:30:00.000+00:00", "DEBUG", "NEXUS-TEST-02", "詳しい情報"),
    ])  # fmt: skip
    (log_dir / "collector.log").open("a", encoding="utf-8").write("壊れた行\n")
    write_log(log_dir, "web.log", [("2027-01-14T02:30:00.000+00:00", "INFO", "", "WebGUIを起動します")])
    return env


def operation_rows(body: str) -> list[str]:
    return re.findall(r'<td class="pre">(.*?)</td>', body, re.S)


def test_lg_07_tabs(logs_ready):
    """ログ画面で、動作ログと変更履歴をタブで切り替えて表示する(LG-07)。"""
    server, db, now, admin, _ = logs_ready
    body = admin.get("/logs").body
    assert 'href="/logs"' in body and 'href="/logs/audit"' in body
    assert "tab-active" in body


def test_lg_08_operation_log_all_newest_first(logs_ready):
    """切り替え済みの古いファイルも含めて、新しいものから表示する。日時は日本時間(LG-01、LG-08)。"""
    server, db, now, admin, _ = logs_ready
    body = admin.get("/logs").body
    assert operation_rows(body) == [
        "詳しい情報",
        "&lt;script&gt;alert(1)&lt;/script&gt;=1+2",
        "WebGUIを起動します",
        "機器からの受信が復旧しました",
        "機器からの受信が途絶しました",
        "collectorを起動しました",
    ]
    assert "2027-01-14 12:30:00" in body  # 03:30 UTC = 12:30 日本時間
    assert "<script>alert(1)</script>" not in body  # ログの内容も無害化して表示する(11.3.3)


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ("level=WARNING", ["&lt;script&gt;alert(1)&lt;/script&gt;=1+2", "機器からの受信が途絶しました"]),
        ("hostname=nexus-test-01", ["機器からの受信が復旧しました", "機器からの受信が途絶しました"]),
        ("keyword=%E8%B5%B7%E5%8B%95", ["WebGUIを起動します", "collectorを起動しました"]),  # 「起動」
        # 日本時間 10:00〜11:30 = UTC 01:00〜02:30(境目の時刻も含む)
        ("date_from=2027-01-14T10:00&date_to=2027-01-14T11:30",
         ["WebGUIを起動します", "機器からの受信が復旧しました", "機器からの受信が途絶しました"]),
        ("date_from=2027-01-14T10:01&date_to=2027-01-14T11:29", ["機器からの受信が復旧しました"]),
    ],
)
def test_lg_08_operation_log_filters(logs_ready, params, expected):
    """日時、ログレベル、ホスト名、キーワードで絞り込める(LG-08、T2-36)。"""
    server, db, now, admin, _ = logs_ready
    assert operation_rows(admin.get(f"/logs?{params}").body) == expected


def test_lg_09_download_operation_log(logs_ready):
    """表示中のログを、ファイルとしてダウンロードできる(LG-09、T2-36)。"""
    server, db, now, admin, _ = logs_ready
    status, headers, content = admin.raw("/logs/download?level=WARNING")
    assert status == 200
    assert "attachment" in headers["content-disposition"] and ".csv" in headers["content-disposition"]
    assert content.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows[0] == ["日時(日本時間)", "ログレベル", "出力元", "ホスト名", "内容"]
    assert [r[4] for r in rows[1:]] == ["<script>alert(1)</script>=1+2", "機器からの受信が途絶しました"]
    assert 'href="/logs/download?level=WARNING"' in admin.get("/logs?level=WARNING").body


def test_lg_09_csv_formula_guard(env):
    """表計算ソフトで開いたときに、内容が計算式として動かないようにする。"""
    server, db, now, admin, log_dir = env
    write_log(log_dir, "collector.log", [("2027-01-14T00:00:00.000+00:00", "WARNING", "=HYPERLINK(1)", "@SUM(1)")])
    _, _, content = admin.raw("/logs/download")
    row = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))[1]
    assert row[3] == "'=HYPERLINK(1)" and row[4] == "'@SUM(1)"


def test_lg_08_audit_tab_and_download(env):
    """変更履歴のタブで、日時、ユーザー名、変更した画面、変更前後の値を表示し、ダウンロードできる(7.4.6.3、T2-37)。"""
    server, db, now, admin, _ = env
    create_device(admin, hostname="NEXUS-TEST-05", source_ip="192.0.2.5")
    body = admin.get("/logs/audit").body
    assert "機器" in body and "登録" in body and "NEXUS-TEST-05" in body
    assert "ログイン" in body  # ログインも記録されている
    assert "&#34;hostname&#34;: &#34;NEXUS-TEST-05&#34;" in body
    body = admin.get("/logs/audit?keyword=NEXUS-TEST-05").body
    assert len(re.findall(r'<td class="nowrap">', body)) == 1
    _, headers, content = admin.raw("/logs/audit/download?keyword=NEXUS-TEST-05")
    rows = list(csv.reader(io.StringIO(content.decode("utf-8-sig"))))
    assert rows[0] == ["日時(日本時間)", "ユーザー名", "変更した画面", "操作", "対象", "変更前の値", "変更後の値"]
    assert rows[1][1:5] == ["admin", "機器", "登録", "NEXUS-TEST-05"]


def test_lg_08_audit_keyword_with_wildcards(env):
    """キーワードの「%」や「_」は、文字そのものとして扱う。"""
    server, db, now, admin, _ = env
    create_device(admin)
    body = admin.get("/logs/audit?keyword=%25").body
    assert "条件に合うログはありません" in body


def test_logs_without_log_dir(tmp_path):
    from web.logs import LogFilter, read_operation_logs

    assert read_operation_logs(None, LogFilter()) == ([], False)
    assert read_operation_logs(tmp_path / "none", LogFilter()) == ([], False)


def test_logs_truncated(env):
    server, db, now, admin, log_dir = env
    write_log(log_dir, "collector.log", [(f"2027-01-14T00:{m:02d}:{s:02d}.000+00:00", "INFO", "", f"行{m * 60 + s}") for m in range(20) for s in range(60)])
    body = admin.get("/logs").body
    assert "1,000 件を超えた" in body
    assert len(operation_rows(body)) == 1000
    assert operation_rows(body)[0] == "行1199"


# ======================================================================
# バックアップと復元(BK-01〜BK-05、UI-21)
# ======================================================================


def test_bk_01_backup_download(env):
    """機器の登録内容と全体設定を、JSON形式の1つのファイルとして書き出す(BK-01、BK-02)。"""
    server, db, now, admin, _ = env
    create_device(admin)
    status, headers, content = admin.raw("/system/backup")
    assert status == 200
    assert 'filename="telemetry-backup-' in headers["content-disposition"]
    backup = json.loads(content)
    assert [d["hostname"] for d in backup["devices"]] == ["NEXUS-TEST-01"]
    assert "webgui_port" not in backup["settings"]
    assert b"admin" not in content and b"scrypt" not in content


def restore(client: Browser, content: bytes, filename: str = "backup.json"):  # noqa: ANN201
    return client.post_file("/system/restore", {"csrf": csrf_of(client, "/system")}, "file", filename, content)


def test_bk_03_restore_with_confirmation(env):
    """バックアップを読み込み、確認画面を表示してから、機器の登録内容と全体設定を置き換える。
    ユーザーとWebGUIのポート番号は変わらない。復元は変更履歴に記録される(BK-03、BK-05、UI-21、T2-39)。"""
    server, db, now, admin, _ = env
    create_device(admin)
    post_settings(admin, stale_timeout_sec="900")
    backup_bytes = admin.raw("/system/backup")[2]

    # 機器をすべて削除し、設定も変える
    device_id = list_devices(connect(db))[0].id
    admin.post(f"/devices/{device_id}/delete", {"csrf": csrf_of(admin)})
    post_settings(admin, stale_timeout_sec="60")
    add_user(admin, "operator1", "viewer", "")

    confirm = restore(admin, backup_bytes, "my-backup.json")
    assert confirm.status == 200
    assert "すべてこのファイルの内容に置き換わります" in confirm.body
    assert "NEXUS-TEST-01" in confirm.body and "my-backup.json" in confirm.body
    assert list_devices(connect(db)) == []  # 確認画面の時点では、まだ変えない

    token = re.search(r'name="restore_token" value="([^"]+)"', confirm.body).group(1)
    page = admin.post("/system/restore/confirm", {"csrf": confirm.csrf(), "restore_token": token})
    assert page.status == 200
    assert "設定を復元しました" in page.body
    assert [d.hostname for d in list_devices(connect(db))] == ["NEXUS-TEST-01"]
    settings = get_settings(connect(db))
    assert settings["stale_timeout_sec"] == "900" and settings["webgui_port"] == "8080"
    assert len(query(db, "SELECT * FROM users WHERE username = 'operator1'")) == 1

    record = audits(db)[-1]
    assert (record["category"], record["action"], record["target"]) == ("restore", "restore", "my-backup.json")
    assert json.loads(record["before_value"])["devices"] == []
    assert json.loads(record["after_value"])["devices"] == ["NEXUS-TEST-01"]

    # 同じ確認画面の預かり番号は、2回は使えない
    again = admin.post("/system/restore/confirm", {"csrf": confirm.csrf(), "restore_token": token})
    assert again.status == 400 and "有効期限が切れました" in again.body


def test_bk_04_invalid_backup_rejected(env):
    """不正なファイルは、エラーを表示して何も変更しない(BK-04)。"""
    server, db, now, admin, _ = env
    create_device(admin)
    backup = export_backup(connect(db))
    backup["devices"][0]["source_ip"] = "999.0.0.1"
    backup["settings"]["queue_max_size"] = "5"
    before = (list_devices(connect(db)), get_settings(connect(db)))
    page = restore(admin, json.dumps(backup).encode())
    assert page.status == 400
    assert "1台目の機器" in page.body and "送信待ちキューの上限" in page.body
    assert (list_devices(connect(db)), get_settings(connect(db))) == before
    for broken in (b"not json", b"", b"{}"):
        assert restore(admin, broken).status == 400
    assert (list_devices(connect(db)), get_settings(connect(db))) == before
    assert not [a for a in audits(db) if a["category"] == "restore"]


def test_bk_restore_token_is_per_user(env):
    """確認画面の預かり番号は、預けた本人しか使えない。"""
    server, db, now, admin, _ = env
    confirm = restore(admin, admin.raw("/system/backup")[2])
    token = re.search(r'name="restore_token" value="([^"]+)"', confirm.body).group(1)
    add_user(admin, "admin2", "admin", "secret-2")
    other = Browser(server.base_url)
    other.login("admin2", "secret-2")
    page = other.post("/system/restore/confirm", {"csrf": csrf_of(other), "restore_token": token})
    assert page.status == 400


def test_bk_restore_token_expires(env):
    server, db, now, admin, _ = env
    confirm = restore(admin, admin.raw("/system/backup")[2])
    token = re.search(r'name="restore_token" value="([^"]+)"', confirm.body).group(1)
    now.advance(minutes=11)
    page = admin.post("/system/restore/confirm", {"csrf": confirm.csrf(), "restore_token": token})
    assert page.status == 400


def test_restore_without_file(env):
    server, db, now, admin, _ = env
    page = admin.post_file("/system/restore", {"csrf": csrf_of(admin, "/system")}, "file", "", b"")
    assert page.status == 400 and "ファイルを選んでください" in page.body


# ======================================================================
# 権限とCSRF(T2-30、11.3.3)
# ======================================================================

ADMIN_POSTS = [
    ("/devices/new", {}),
    ("/devices/1/edit", {}),
    ("/devices/1/delete", {}),
    ("/settings", {}),
    ("/settings/zabbix-test", {}),
    ("/users/new", {"username": "x", "role": "admin", "password": "x"}),
    ("/users/1/role", {"role": "viewer"}),
    ("/users/1/delete", {}),
    ("/users/1/reset", {}),
    ("/system/restore/confirm", {"restore_token": "x"}),
]
ADMIN_GETS = ["/devices", "/devices/new", "/devices/1/edit", "/devices/1/delete", "/settings", "/users", "/users/1/delete",
              "/users/1/reset", "/logs", "/logs/download", "/logs/audit", "/logs/audit/download", "/system", "/system/backup"]  # fmt: skip


@pytest.mark.parametrize(("path", "data"), ADMIN_POSTS)
def test_viewer_cannot_send_admin_operations(env, path, data):
    """閲覧者が設定を変更する操作を直接送っても、拒否する(AU-04、T2-30)。"""
    server, db, now, admin, _ = env
    create_device(admin)
    viewer = Browser(server.base_url)
    viewer.login("user", "")
    before = (list_devices(connect(db)), get_settings(connect(db)), query(db, "SELECT id, username, role FROM users"))
    page = viewer.post(path, {"csrf": csrf_of(viewer), **device_form(""), **SETTINGS_FORM, **data})
    assert page.status == 403
    after = (list_devices(connect(db)), get_settings(connect(db)), query(db, "SELECT id, username, role FROM users"))
    assert after == before


def test_viewer_cannot_upload_restore(env):
    server, db, now, admin, _ = env
    viewer = Browser(server.base_url)
    viewer.login("user", "")
    page = viewer.post_file("/system/restore", {"csrf": csrf_of(viewer)}, "file", "b.json", b"{}")
    assert page.status == 403


@pytest.mark.parametrize("path", ADMIN_GETS)
def test_viewer_cannot_open_admin_pages(env, path):
    server, db, now, admin, _ = env
    viewer = Browser(server.base_url)
    viewer.login("user", "")
    assert viewer.get(path).status == 403


@pytest.mark.parametrize(("path", "data"), ADMIN_POSTS)
def test_admin_operations_require_csrf(env, path, data):
    """管理者の操作でも、不正な操作を防ぐための印がなければ拒否する(11.3.3)。"""
    server, db, now, admin, _ = env
    create_device(admin)
    before = (list_devices(connect(db)), get_settings(connect(db)), query(db, "SELECT id, username, role FROM users"))
    page = admin.post(path, {"csrf": "forged", **device_form(""), **SETTINGS_FORM, **data})
    assert page.status == 403
    after = (list_devices(connect(db)), get_settings(connect(db)), query(db, "SELECT id, username, role FROM users"))
    assert after == before
