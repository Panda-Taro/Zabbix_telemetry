"""システム設定の画面の自動テスト(UI-14〜UI-16、OP-08、決定事項 D-029、D-030)。

WebGUI を実際に起動し、本体側の補助プログラム(tools/telemetry_hostctl.py)の処理も実際に動かす。
本体のコマンドは偽物に差し替えるため、この開発用PCの設定は何も変わらず、再起動もしない。
"""

import json
import socket

import pytest
from markupsafe import escape

from common.config_reader import get_settings
from common.constants import HOSTCTL_INBOX, HOSTCTL_NETPLAN_FILE
from common.database import connect
from tests.hostctl_support import CURRENT_ADDRESS, DEV, JSON_YAML, FakeRunner, HelperThread, make_hostctl
from tests.web_support import Browser, FakeNow, WebServer, free_tcp_port
from tools.telemetry_hostctl import write_state
from web.hostctl import HostCtlClient

KEEP_NETWORK = {"address": CURRENT_ADDRESS, "gateway": "192.0.2.1", "dns1": "192.0.2.53", "dns2": ""}


class Env:
    def __init__(self, tmp_path, *, with_helper_dirs: bool = True, netplan_ok: bool = True) -> None:
        self.db = tmp_path / "telemetry.db"
        self.runner = FakeRunner(netplan_ok=netplan_ok)
        self.ctl = make_hostctl(tmp_path, self.db, self.runner)
        write_state(self.ctl.base_dir, self.runner)  # 起動時に補助プログラムが書く、本体の状態
        base = self.ctl.base_dir if with_helper_dirs else None
        self.client = HostCtlClient(base, ack_timeout=1.0)
        self.server = WebServer(self.db, FakeNow(), hostctl=self.client)
        self.server.start()
        self.admin = Browser(self.server.base_url)
        self.admin.login("admin", "admin")

    @property
    def inbox(self):
        return sorted((self.ctl.base_dir / HOSTCTL_INBOX).iterdir())

    def settings(self) -> dict[str, str]:
        conn = connect(self.db)
        try:
            return get_settings(conn)
        finally:
            conn.close()

    def audit(self) -> list[dict]:
        conn = connect(self.db)
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM audit_log WHERE category = 'system' ORDER BY id")]
        finally:
            conn.close()

    def submit(self, path: str, **fields: str):
        page = self.admin.get("/system")
        data = {**KEEP_NETWORK, "webgui_port": "8080", **fields, "csrf": page.csrf()}
        return self.admin.post(path, data)

    def netplan_own(self):
        own = self.ctl.paths.netplan_dir / HOSTCTL_NETPLAN_FILE
        return JSON_YAML.load(own.read_text(encoding="utf-8")) if own.exists() else None


@pytest.fixture
def env(tmp_path):
    environment = Env(tmp_path)
    try:
        yield environment
    finally:
        environment.server.stop()


def test_system_page_shows_current_network(env):
    page = env.admin.get("/system")
    assert page.status == 200
    for text in (DEV, CURRENT_ADDRESS, "192.0.2.1", "192.0.2.53", "保存する", "再起動"):
        assert text in page.body
    assert "補助プログラムが見つかりません" not in page.body


def test_confirm_for_port_change_shows_required_items_and_saves_nothing(env):
    """保存の前に、画面の中に確認を出す(UI-16、D-029)。確認の時点では、何も保存しない。"""
    page = env.submit("/system/host", webgui_port="8443")
    assert page.status == 200
    body = page.body
    assert "すぐに中間サーバーを再起動します" in body
    assert "1〜2分程度" in body and "監視データが失われます" in body
    assert "http://127.0.0.1:8443/" in body
    assert "保存して再起動する" in body and "キャンセル" in body
    assert "全Nexusの送信先" not in body  # IPアドレスを変えないときは表示しない
    assert "confirm(" not in body  # ブラウザ標準の確認窓は使わない
    assert env.settings()["webgui_port"] == "8080"
    assert env.inbox == []  # 補助プログラムへの依頼もしていない
    assert not any(command[0] in ("ufw", "netplan", "systemctl") for command in env.runner.commands)


def test_confirm_for_ip_change_mentions_nexus_and_zabbix(env):
    page = env.submit("/system/host", address="192.0.2.20/24", gateway="192.0.2.254", dns1="192.0.2.53", dns2="192.0.2.54")
    assert page.status == 200
    assert "全Nexusの送信先" in page.body and "Zabbix側の受け付け設定" in page.body
    assert "http://192.0.2.20:" in page.body
    assert "192.0.2.254" in page.body
    assert env.netplan_own() is None and env.inbox == []


def test_cancel_changes_nothing(env):
    """「キャンセル」は何も保存しない(D-029)。"""
    page = env.submit("/system/host", webgui_port="8443", address="192.0.2.20/24")
    assert 'href="/system"' in page.body
    page = env.admin.get("/system")  # キャンセルのリンク先
    assert page.status == 200
    assert env.settings()["webgui_port"] == "8080"
    assert env.netplan_own() is None and env.inbox == [] and not env.runner.rebooted


def test_save_and_reboot_port_and_ip(env):
    """「保存して再起動する」:補助プログラムが受け付けてから保存し、本体を再起動する(UI-14、UI-15、OP-08)。"""
    with HelperThread(env.ctl):
        page = env.submit(
            "/system/host/confirm", webgui_port="8443", address="192.0.2.20/24", gateway="192.0.2.254", dns1="192.0.2.53"
        )
    assert page.status == 200, page.body
    assert "中間サーバーを再起動しています" in page.body
    assert "http://192.0.2.20:8443/" in page.body
    assert env.settings()["webgui_port"] == "8443"
    own = env.netplan_own()
    assert own["network"]["ethernets"][DEV]["addresses"] == ["192.0.2.20/24"]
    assert own["network"]["ethernets"][DEV]["routes"] == [{"to": "default", "via": "192.0.2.254"}]
    assert env.runner.ran("ufw", "allow", "8443/tcp", "comment", "telemetry")
    assert env.runner.rebooted
    audit = env.audit()
    assert [row["target"] for row in audit] == ["webgui_port", "本体のIPアドレス"]
    assert all(row["username"] == "admin" for row in audit)
    assert json.loads(audit[1]["after_value"])["address"] == "192.0.2.20/24"
    assert json.loads(audit[1]["before_value"])["address"] == CURRENT_ADDRESS


def test_save_and_reboot_port_only_does_not_touch_network(env):
    with HelperThread(env.ctl):
        page = env.submit("/system/host/confirm", webgui_port="8443")
    assert "中間サーバーを再起動しています" in page.body
    assert env.netplan_own() is None
    assert env.runner.rebooted and env.settings()["webgui_port"] == "8443"


def test_nothing_saved_when_helper_does_not_answer(env):
    """補助プログラムが応答しなければ、何も保存しない(「保存したが再起動していない」状態を作らない)。"""
    page = env.submit("/system/host/confirm", webgui_port="8443")
    assert page.status == 500
    assert "何も保存していません" in page.body
    assert env.settings()["webgui_port"] == "8080"
    assert env.inbox == []  # 依頼を取り下げた
    assert env.audit() == []


def test_nothing_saved_when_helper_rejects(tmp_path):
    environment = Env(tmp_path, netplan_ok=False)
    try:
        with HelperThread(environment.ctl):
            page = environment.submit("/system/host/confirm", webgui_port="8443", address="192.0.2.20/24", gateway="192.0.2.254")
        assert page.status == 500 and "何も保存していません" in page.body
        assert environment.settings()["webgui_port"] == "8080"
        assert environment.netplan_own() is None
        assert not environment.runner.rebooted
    finally:
        environment.server.stop()


@pytest.mark.parametrize(
    ("fields", "fragment"),
    [
        ({"webgui_port": "80"}, "1024〜65535"),
        ({"webgui_port": "abc"}, "半角の整数"),
        ({"address": "192.0.2.20/31"}, "8〜30"),
        ({"address": "192.0.2.20/24", "gateway": "198.51.100.1"}, "同じネットワーク"),
        ({"dns1": "", "dns2": ""}, "1つ以上"),
        ({"address": "<script>alert(1)</script>"}, "形式"),
    ],
)
def test_invalid_input_is_rejected(env, fields, fragment):
    for path in ("/system/host", "/system/host/confirm"):
        page = env.submit(path, **fields)
        assert page.status == 400
        assert fragment in page.body
        assert "<script>alert(1)</script>" not in page.body
    if "address" in fields and "<script>" in fields["address"]:
        assert str(escape("<script>alert(1)</script>")) in page.body
    assert env.settings()["webgui_port"] == "8080" and env.inbox == []


def test_no_change(env):
    page = env.submit("/system/host")
    assert page.status == 200 and "変更がありません" in page.body
    page = env.submit("/system/host", address="", gateway="", dns1="", dns2="")
    assert "変更がありません" in page.body


def test_port_in_use_is_rejected(env):
    port = free_tcp_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("0.0.0.0", port))
        busy.listen()
        page = env.submit("/system/host", webgui_port=str(port))
    assert page.status == 400 and "ほかのプログラムが使っています" in page.body


def test_viewer_and_csrf_are_rejected(env):
    viewer = Browser(env.server.base_url)
    viewer.login("user", "")
    csrf = viewer.get("/dashboard").csrf()
    for path in ("/system/host", "/system/host/confirm"):
        assert viewer.post(path, {**KEEP_NETWORK, "webgui_port": "8443", "csrf": csrf}).status == 403
        assert env.admin.post(path, {**KEEP_NETWORK, "webgui_port": "8443", "csrf": "wrong"}).status == 403
    assert env.settings()["webgui_port"] == "8080" and env.inbox == []


def test_without_helper_changes_are_refused(tmp_path):
    environment = Env(tmp_path, with_helper_dirs=False)
    try:
        page = environment.admin.get("/system")
        assert "補助プログラムが見つかりません" in page.body
        page = environment.submit("/system/host/confirm", webgui_port="8443")
        assert page.status == 400 and "補助プログラムが見つからない" in page.body
        assert environment.settings()["webgui_port"] == "8080"
    finally:
        environment.server.stop()


def test_fallback_port_is_shown(tmp_path):
    """保存されたポート番号が使えず、既定値で動いているときは、その旨を表示する(D-029)。"""
    db = tmp_path / "telemetry.db"
    server = WebServer(db, FakeNow(), listen_port=8080)
    server.start()
    try:
        conn = connect(db)
        from common.config_writer import update_settings

        update_settings(conn, {"webgui_port": "8443"})
        conn.close()
        admin = Browser(server.base_url)
        admin.login("admin", "admin")
        assert "既定値の 8080 で動いています" in admin.get("/system").body
    finally:
        server.stop()


def test_telemetry_port_change_asks_helper_to_sync_firewall(env):
    """全体設定の受信ポート番号の変更は再起動なしで反映し、ファイアウォールの調整だけを依頼する(D-029、D-030)。"""
    page = env.admin.get("/settings")
    data = {
        "zabbix_server": "192.0.2.100", "zabbix_port": "10051", "telemetry_udp_port": "57500",
        "stale_timeout_sec": "300", "self_monitor_interval_sec": "60", "lld_interval_sec": "3600",
        "queue_max_size": "100000", "log_level": "INFO", "confirm": "1", "csrf": page.csrf(),
    }  # fmt: skip
    assert env.admin.post("/settings", data).status == 303
    requests = [json.loads(path.read_text(encoding="utf-8")) for path in env.inbox]
    assert [request["action"] for request in requests] == ["sync_firewall"]
    assert env.settings()["telemetry_udp_port"] == "57500"
    assert not env.runner.rebooted
    data["telemetry_udp_port"] = "1023"
    page = env.admin.post("/settings", data)
    assert page.status == 400 and "1024〜65535" in page.body
