"""本体のIPアドレスの入力の検査と、本体側の補助プログラムの自動テスト(UI-14、OP-08、決定事項 D-029、D-030)。

本体のコマンドは偽物に差し替えるため、この開発用PCで実行しても、本体の設定は何も変わらない。
"""

import json
import os

import pytest

from common.config_writer import seed_initial_data, update_settings
from common.constants import HOSTCTL_INBOX, HOSTCTL_NETPLAN_FILE, HOSTCTL_RESULTS, HOSTCTL_STATE
from common.database import connect, initialize_schema
from common.netconfig import (
    ACTION_REBOOT,
    ACTION_SYNC_FIREWALL,
    HostRequest,
    NetworkConfig,
    NetworkInputError,
    RequestError,
    parse_request,
    validate_network,
)
from common.validation import ValidationError, validate_settings
from tests.hostctl_support import DEV, JSON_YAML, FakeRunner, make_hostctl
from tools.telemetry_hostctl import (
    CLOUD_INIT_DISABLE_FILE,
    FIREWALL_STATE_FILE,
    HostCtlError,
    Ports,
    plan_netplan_change,
    read_ports,
    sync_firewall,
    write_netplan,
    write_state,
)

NEW = NetworkConfig(address="192.0.2.20/24", gateway="192.0.2.254", dns=("192.0.2.53", "192.0.2.54"))
REQUEST_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "telemetry.db"
    conn = connect(path)
    initialize_schema(conn)
    seed_initial_data(conn)
    conn.close()
    return path


# ======================================================================
# 本体のIPアドレスの入力の検査(決定事項 D-030)
# ======================================================================


def test_validate_network_accepts_and_normalizes():
    config = validate_network(" 192.0.2.20/24 ", "192.0.2.254", ["192.0.2.53", " "])
    assert config.address == "192.0.2.20/24" and config.gateway == "192.0.2.254"
    assert config.dns == ("192.0.2.53",)
    assert validate_network("192.0.2.20/24", "192.0.2.1", ["192.0.2.53", "192.0.2.54"]).dns == ("192.0.2.53", "192.0.2.54")
    assert validate_network("192.0.2.3/8", "192.0.0.1", ["", "192.0.2.53"]).ip == "192.0.2.3"  # プレフィックス長 8
    assert validate_network("192.0.2.5/30", "192.0.2.6", ["192.0.2.53"]).address == "192.0.2.5/30"


@pytest.mark.parametrize(
    ("address", "gateway", "dns", "field", "fragment"),
    [
        ("", "192.0.2.1", ["192.0.2.53"], "address", "入力してください"),
        ("192.0.2.20", "192.0.2.1", ["192.0.2.53"], "address", "形式"),  # プレフィックス長がない
        ("192.0.2.20/255.255.255.0", "192.0.2.1", ["192.0.2.53"], "address", "形式"),
        ("192.0.2.20/7", "192.0.2.1", ["192.0.2.53"], "address", "8〜30"),
        ("192.0.2.20/31", "192.0.2.1", ["192.0.2.53"], "address", "8〜30"),
        ("192.0.2.256/24", "192.0.2.1", ["192.0.2.53"], "address", "形式"),
        ("192.0.2.020/24", "192.0.2.1", ["192.0.2.53"], "address", "形式"),  # 先頭の0
        ("１９２.0.2.20/24", "192.0.2.1", ["192.0.2.53"], "address", "形式"),  # 全角の数字
        ("127.0.0.5/24", "127.0.0.1", ["192.0.2.53"], "address", "使えません"),
        ("192.0.2.0/24", "192.0.2.1", ["192.0.2.53"], "address", "先頭と末尾"),
        ("192.0.2.255/24", "192.0.2.1", ["192.0.2.53"], "address", "先頭と末尾"),
        ("192.0.2.20/24", "", ["192.0.2.53"], "gateway", "入力してください"),
        ("192.0.2.20/24", "198.51.100.1", ["192.0.2.53"], "gateway", "同じネットワーク"),
        ("192.0.2.20/24", "192.0.2.20", ["192.0.2.53"], "gateway", "別のアドレス"),
        ("192.0.2.20/24", "192.0.2.255", ["192.0.2.53"], "gateway", "先頭と末尾"),
        ("192.0.2.20/24", "abc", ["192.0.2.53"], "gateway", "形式"),
        ("192.0.2.20/24", "192.0.2.1", [], "dns", "1つ以上"),
        ("192.0.2.20/24", "192.0.2.1", ["", " "], "dns", "1つ以上"),
        ("192.0.2.20/24", "192.0.2.1", ["192.0.2.53", "192.0.2.54", "192.0.2.55"], "dns", "2 つまで"),
        ("192.0.2.20/24", "192.0.2.1", ["192.0.2.53", "192.0.2.53"], "dns", "2回"),
        ("192.0.2.20/24", "192.0.2.1", ["127.0.0.53"], "dns", "使えません"),
        ("192.0.2.20/24", "192.0.2.1", ["224.0.0.1"], "dns", "使えません"),
        ("192.0.2.20/24", "192.0.2.1", ["dns.example"], "dns", "形式"),
        ("1" * 100, "192.0.2.1", ["192.0.2.53"], "address", "形式"),  # 長すぎる入力
    ],
)
def test_validate_network_rejects(address, gateway, dns, field, fragment):
    with pytest.raises(NetworkInputError) as excinfo:
        validate_network(address, gateway, dns)
    assert any(fragment in message for message in excinfo.value.errors.get(field, [])), excinfo.value.errors


@pytest.mark.parametrize("key", ["telemetry_udp_port", "webgui_port"])
def test_listen_port_range_is_1024_to_65535(key):
    """待ち受けるポート番号は 1024〜65535(決定事項 D-030)。接続先の Zabbix のポート番号は 1〜65535 のまま。"""
    assert validate_settings({key: "1024"})[key] == "1024"
    assert validate_settings({key: "65535"})[key] == "65535"
    for bad in ("1023", "80", "65536"):
        with pytest.raises(ValidationError) as excinfo:
            validate_settings({key: bad})
        assert "1024〜65535" in excinfo.value.messages()[0]
    assert validate_settings({"zabbix_port": "1"})["zabbix_port"] == "1"


# ======================================================================
# 依頼の形の検査(web コンテナが書くものを、補助プログラムは信用しない)
# ======================================================================


def _raw(**data):
    base = {"id": REQUEST_ID, "created_at": 1.0, "action": ACTION_REBOOT, "webgui_port": 8081}
    base.update(data)
    return json.dumps({k: v for k, v in base.items() if v is not None}).encode()


def test_parse_request_round_trip():
    request = HostRequest(id=REQUEST_ID, created_at=5.0, action=ACTION_REBOOT, network=NEW, webgui_port=9000)
    assert parse_request(request.to_json_bytes()) == request
    firewall = HostRequest(id=REQUEST_ID, created_at=5.0, action=ACTION_SYNC_FIREWALL)
    assert parse_request(firewall.to_json_bytes()) == firewall


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not json",
        b"[]",
        b"x" * 5000,
        _raw(id="../../etc/passwd"),
        _raw(id="ABCDEF0123456789abcdef0123456789"),
        _raw(action="shell"),
        _raw(created_at="now"),
        _raw(created_at=True),
        _raw(webgui_port=80),
        _raw(webgui_port="8081"),
        _raw(webgui_port=True),
        _raw(webgui_port=None),  # 再起動の依頼に、変更する内容がない
        _raw(command="rm -rf /"),
        _raw(network={"address": "192.0.2.20/31", "gateway": "192.0.2.1", "dns": ["192.0.2.53"]}),
        _raw(network={"address": "192.0.2.20/24", "gateway": "192.0.2.1", "dns": "192.0.2.53"}),
        _raw(network={"address": "192.0.2.20/24", "gateway": "192.0.2.1", "dns": ["192.0.2.53"], "extra": 1}),
        _raw(action=ACTION_SYNC_FIREWALL),  # ファイアウォールの依頼に、余計な項目がある
    ],
)
def test_parse_request_rejects(raw):
    with pytest.raises(RequestError):
        parse_request(raw)


# ======================================================================
# netplan の設定ファイルの書き換え
# ======================================================================


def test_plan_moves_interface_into_own_file_and_keeps_others():
    """対象のLAN端子の設定は自分のファイルだけに書き、ほかのLAN端子の設定は元のファイルに残す。"""
    files = {
        "00-installer-config.yaml": {
            "network": {
                "version": 2,
                "ethernets": {
                    DEV: {
                        "addresses": ["192.0.2.10/24", "2001:db8::10/64"],
                        "gateway4": "192.0.2.1",
                        "routes": [{"to": "198.51.100.0/24", "via": "192.0.2.2"}, {"to": "::/0", "via": "2001:db8::1"}],
                        "nameservers": {"addresses": ["192.0.2.53", "2001:db8::53"], "search": ["example.test"]},
                        "mtu": 9000,
                    },
                    "eth1": {"dhcp4": True},
                },
            }
        },
        "01-other.yaml": {"network": {"version": 2, "ethernets": {DEV: {"optional": True}}}},
    }
    plan = plan_netplan_change(files, DEV, NEW)
    own = plan[HOSTCTL_NETPLAN_FILE]["network"]["ethernets"][DEV]
    assert own["dhcp4"] is False
    assert own["addresses"] == ["192.0.2.20/24", "2001:db8::10/64"]
    assert "gateway4" not in own
    assert own["routes"] == [
        {"to": "198.51.100.0/24", "via": "192.0.2.2"},
        {"to": "::/0", "via": "2001:db8::1"},
        {"to": "default", "via": "192.0.2.254"},
    ]
    assert own["nameservers"] == {"addresses": ["192.0.2.53", "192.0.2.54", "2001:db8::53"], "search": ["example.test"]}
    assert own["mtu"] == 9000 and own["optional"] is True  # ほかの設定は残す
    # 元のファイルからは対象のLAN端子だけを取り除き、eth1 は残す
    assert plan["00-installer-config.yaml"]["network"]["ethernets"] == {"eth1": {"dhcp4": True}}
    # LAN端子の設定が何も残らないファイルは、削除する
    assert plan["01-other.yaml"] is None


def test_plan_replaces_own_file_on_second_change_and_handles_renamed_interface():
    files = {
        "50-cloud-init.yaml": {"network": {"version": 2, "ethernets": {"lan": {"match": {"macaddress": "00:00:5e:00:53:01"}, "set-name": DEV, "dhcp4": True}}}},
    }
    plan = plan_netplan_change(files, DEV, NEW)
    own = plan[HOSTCTL_NETPLAN_FILE]["network"]["ethernets"]["lan"]
    assert own["set-name"] == DEV and own["match"] == {"macaddress": "00:00:5e:00:53:01"}
    assert plan["50-cloud-init.yaml"] is None
    # 2回目の変更:自分のファイルだけを書き換える
    second = plan_netplan_change({HOSTCTL_NETPLAN_FILE: plan[HOSTCTL_NETPLAN_FILE]}, DEV, validate_network("192.0.2.30/24", "192.0.2.1", ["192.0.2.53"]))
    assert list(second) == [HOSTCTL_NETPLAN_FILE]
    assert second[HOSTCTL_NETPLAN_FILE]["network"]["ethernets"]["lan"]["addresses"] == ["192.0.2.30/24"]


def test_plan_refuses_unsupported_layouts():
    with pytest.raises(HostCtlError, match="自動で変更できない"):
        plan_netplan_change({"a.yaml": {"network": {"version": 2, "bonds": {DEV: {"interfaces": ["eth1"]}}}}}, DEV, NEW)
    with pytest.raises(HostCtlError, match="形が正しくない"):
        plan_netplan_change({"a.yaml": {"something": 1}}, DEV, NEW)


def test_write_netplan_writes_files_backs_up_and_disables_cloud_init(tmp_path, db):
    runner = FakeRunner()
    ctl = make_hostctl(tmp_path, db, runner)
    original = (ctl.paths.netplan_dir / "50-cloud-init.yaml").read_text(encoding="utf-8")
    assert write_netplan(NEW, runner, JSON_YAML, ctl.paths) == DEV
    own = ctl.paths.netplan_dir / HOSTCTL_NETPLAN_FILE
    data = JSON_YAML.load(own.read_text(encoding="utf-8"))
    assert data["network"]["ethernets"][DEV]["addresses"] == ["192.0.2.20/24"]
    assert not (ctl.paths.netplan_dir / "50-cloud-init.yaml").exists()
    backups = list((ctl.paths.state_dir / "netplan-backup").glob("*/50-cloud-init.yaml"))
    assert len(backups) == 1 and backups[0].read_text(encoding="utf-8") == original
    assert (ctl.paths.cloud_init_dir / CLOUD_INIT_DISABLE_FILE).exists()
    assert runner.ran("netplan", "generate")
    assert not runner.rebooted  # 書き換えるだけで、反映(再起動)はしない
    if os.name != "nt":
        assert oct(own.stat().st_mode & 0o777) == "0o600"


def test_write_netplan_restores_everything_when_netplan_rejects(tmp_path, db):
    runner = FakeRunner(netplan_ok=False)
    ctl = make_hostctl(tmp_path, db, runner)
    original = (ctl.paths.netplan_dir / "50-cloud-init.yaml").read_text(encoding="utf-8")
    with pytest.raises(HostCtlError, match="netplan generate"):
        write_netplan(NEW, runner, JSON_YAML, ctl.paths)
    assert (ctl.paths.netplan_dir / "50-cloud-init.yaml").read_text(encoding="utf-8") == original
    assert not (ctl.paths.netplan_dir / HOSTCTL_NETPLAN_FILE).exists()


def test_write_state(tmp_path, db):
    ctl = make_hostctl(tmp_path, db, FakeRunner())
    write_state(ctl.base_dir, ctl.runner)
    state = json.loads((ctl.base_dir / HOSTCTL_STATE / "network.json").read_text(encoding="utf-8"))
    assert (state["interface"], state["address"], state["gateway"], state["dns"]) == (DEV, "192.0.2.10/24", "192.0.2.1", ["192.0.2.53"])
    write_state(ctl.base_dir, FakeRunner(has_default_route=False))
    state = json.loads((ctl.base_dir / HOSTCTL_STATE / "network.json").read_text(encoding="utf-8"))
    assert "デフォルトゲートウェイが見つかりません" in state["error"]


# ======================================================================
# ファイアウォール(決定事項 D-030)
# ======================================================================


def test_read_ports_uses_saved_settings_or_defaults(tmp_path, db):
    assert read_ports(tmp_path / "none.db") == Ports(8080, 50051)
    conn = connect(db)
    update_settings(conn, {"webgui_port": "8443", "telemetry_udp_port": "57500"})
    conn.close()
    assert read_ports(db) == Ports(8443, 57500)


def test_sync_firewall_opens_new_and_closes_old_only_when_active(tmp_path):
    state_dir = tmp_path / "state"
    inactive = FakeRunner(ufw_active=False)
    assert "無効" in sync_firewall(inactive, state_dir, Ports(8080, 50051).rules(), close_old=True)
    assert not any(cmd[:2] == ("ufw", "allow") for cmd in inactive.commands)

    runner = FakeRunner()
    sync_firewall(runner, state_dir, Ports(8080, 50051).rules(), close_old=True)
    assert runner.ran("ufw", "allow", "8080/tcp", "comment", "telemetry")
    assert runner.ran("ufw", "allow", "50051/udp", "comment", "telemetry")
    # 再起動の依頼:新しい番号を開けるだけで、閉じない
    runner = FakeRunner()
    sync_firewall(runner, state_dir, Ports(8443, 50051).rules(), close_old=False)
    assert runner.ran("ufw", "allow", "8443/tcp", "comment", "telemetry")
    assert not any(cmd[:2] == ("ufw", "delete") for cmd in runner.commands)
    # 再起動のあと:保存された番号に合わせ、このプログラムが開けた古い番号を閉じる。8080 は常に開けておく
    runner = FakeRunner()
    message = sync_firewall(runner, state_dir, Ports(8443, 57500).rules(), close_old=True)
    assert runner.ran("ufw", "delete", "allow", "50051/udp")
    assert not runner.ran("ufw", "delete", "allow", "8080/tcp")
    assert not runner.ran("ufw", "delete", "allow", "8443/tcp")
    assert "閉じたポート" in message
    assert set(json.loads((state_dir / FIREWALL_STATE_FILE).read_text())["rules"]) == {"8080/tcp", "8443/tcp", "57500/udp"}


# ======================================================================
# 依頼の処理(web → 補助プログラム)
# ======================================================================


def _put_request(ctl, raw: bytes, request_id: str = REQUEST_ID) -> None:
    (ctl.base_dir / HOSTCTL_INBOX / f"{request_id}.json").write_bytes(raw)


def _result(ctl, request_id: str = REQUEST_ID) -> dict:
    return json.loads((ctl.base_dir / HOSTCTL_RESULTS / f"{request_id}.json").read_text(encoding="utf-8"))


def test_process_reboot_request_with_network_and_port(tmp_path, db):
    runner = FakeRunner()
    ctl = make_hostctl(tmp_path, db, runner)
    request = HostRequest(id=REQUEST_ID, created_at=ctl.clock(), action=ACTION_REBOOT, network=NEW, webgui_port=8443)
    _put_request(ctl, request.to_json_bytes())
    ctl.process_inbox()
    result = _result(ctl)
    assert result["ok"] is True and "再起動します" in result["message"]
    assert not any((ctl.base_dir / HOSTCTL_INBOX).iterdir())  # 依頼のファイルは消す
    assert (ctl.paths.netplan_dir / HOSTCTL_NETPLAN_FILE).exists()
    assert runner.ran("ufw", "allow", "8443/tcp", "comment", "telemetry")
    assert runner.rebooted
    # 再起動は、結果を書いたあと(web が画面を返したあと)に行う
    assert runner.commands[-1] == ("systemctl", "reboot")


def test_process_does_not_reboot_when_change_fails(tmp_path, db):
    runner = FakeRunner(netplan_ok=False)
    ctl = make_hostctl(tmp_path, db, runner)
    _put_request(ctl, HostRequest(id=REQUEST_ID, created_at=ctl.clock(), action=ACTION_REBOOT, network=NEW).to_json_bytes())
    ctl.process_inbox()
    assert _result(ctl)["ok"] is False
    assert not runner.rebooted


@pytest.mark.parametrize("age", [-60.0, 60.0])
def test_process_ignores_old_requests(tmp_path, db, age):
    runner = FakeRunner()
    ctl = make_hostctl(tmp_path, db, runner)
    _put_request(ctl, HostRequest(id=REQUEST_ID, created_at=ctl.clock() - age, action=ACTION_REBOOT, webgui_port=8443).to_json_bytes())
    ctl.process_inbox()
    assert _result(ctl)["ok"] is False and "古い" in _result(ctl)["message"]
    assert not runner.rebooted


def test_process_discards_invalid_requests(tmp_path, db):
    runner = FakeRunner()
    ctl = make_hostctl(tmp_path, db, runner)
    _put_request(ctl, b'{"id": "x", "action": "reboot"}', "bad-name")
    _put_request(ctl, _raw(action="shell"))
    other = "f" * 32
    _put_request(ctl, HostRequest(id=other, created_at=ctl.clock(), action=ACTION_REBOOT, webgui_port=8443).to_json_bytes(), REQUEST_ID[:-1] + "0")
    (ctl.base_dir / HOSTCTL_INBOX / "sub").mkdir()
    ctl.process_inbox()
    assert not runner.rebooted
    assert not any((ctl.base_dir / HOSTCTL_INBOX).glob("*.json"))
    assert _result(ctl)["ok"] is False
    assert _result(ctl, REQUEST_ID[:-1] + "0")["ok"] is False  # ファイル名と番号が一致しない


def test_process_sync_firewall_request(tmp_path, db):
    runner = FakeRunner()
    ctl = make_hostctl(tmp_path, db, runner)
    conn = connect(db)
    update_settings(conn, {"telemetry_udp_port": "57500"})
    conn.close()
    _put_request(ctl, HostRequest(id=REQUEST_ID, created_at=ctl.clock(), action=ACTION_SYNC_FIREWALL).to_json_bytes())
    ctl.process_inbox()
    assert _result(ctl)["ok"] is True
    assert runner.ran("ufw", "allow", "57500/udp", "comment", "telemetry")
    assert not runner.rebooted
