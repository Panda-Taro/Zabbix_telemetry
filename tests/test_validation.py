"""入力内容の検査の自動テスト(要件定義書 12.2.1 T1-16〜T1-18)。"""

import pytest

from common.interfaces import InterfaceId
from common.validation import (
    DeviceForm,
    DeviceIdentity,
    ValidationError,
    parse_interface_list,
    validate_device_form,
    validate_settings,
)

ALL_COMMANDS = {"cmd_counters_brief": True, "cmd_ptp_brief": True, "cmd_ptp_clock": True, "cmd_ptp_parent": True}
NO_COMMANDS = {column: False for column in ALL_COMMANDS}

EXISTING = [
    DeviceIdentity(id=1, hostname="NEXUS-TEST-01", source_ip="192.0.2.1"),
    DeviceIdentity(id=2, hostname="NEXUS-TEST-02", source_ip="192.0.2.2"),
]


def form(**overrides) -> DeviceForm:
    values = {
        "hostname": "NEXUS-TEST-03",
        "source_ip": "192.0.2.3",
        "commands": dict(ALL_COMMANDS),
        "interfaces_text": {"counters": "1/1, 1/2", "ptp": "1/1"},
    }
    values.update(overrides)
    return DeviceForm(**values)


def errors_of(device_form: DeviceForm, self_id: int | None = None) -> dict[str, list[str]]:
    with pytest.raises(ValidationError) as info:
        validate_device_form(device_form, EXISTING, self_id)
    return info.value.errors


# ---------------------------------------------------------------- T1-16
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("1/1", ["1/1"]),
        ("1/1, 1/2", ["1/1", "1/2"]),
        ("1/1-4", ["1/1", "1/2", "1/3", "1/4"]),
        ("1/1,1/2", ["1/1", "1/2"]),
        ("  1/48 , 1/1 ", ["1/1", "1/48"]),
        ("1/1-4, 1/48", ["1/1", "1/2", "1/3", "1/4", "1/48"]),
        ("2/1, 1/1", ["1/1", "2/1"]),
        ("99/256", ["99/256"]),
        ("1/5-5", ["1/5"]),
        ("", []),
        ("   ", []),
    ],
)
def test_t1_16_accepted(text, expected):
    """「1/1」「1/1, 1/2」「1/1-4」などが受け付けられる(UI-09)。"""
    assert [i.ifnum for i in parse_interface_list(text)] == expected


@pytest.mark.parametrize(
    "text",
    [
        "1",
        "Eth1/1",
        "Ethernet1/1",
        "1/49/1",
        "1/4-1",
        "a/b",
        "0/1",
        "1/0",
        "100/1",
        "1/257",
        "1/1-257",
        "1/0-4",
        "1/1,",
        ",1/1",
        "1/1,,1/2",
        "1/1 1/2",
        "1/1-",
        "-1/1",
        "1 / 1",
        "1/1;1/2",
        "１/１",  # 全角の数字(1/1)
        "1/1、1/2",  # 全角の読点(1/1、1/2)
        "1/+1",
        "1/1" + ", 1/1" * 2000,  # 長すぎる入力
    ],
)
def test_t1_16_rejected(text):
    """「1」「Eth1/1」「1/49/1」「1/4-1」「a/b」「0/1」「1/0」「100/1」「1/257」などが拒否される(UI-09)。"""
    with pytest.raises(ValueError):
        parse_interface_list(text)


@pytest.mark.parametrize("text", ["1/1, 1/1", "1/1-4, 1/3", "1/1-4, 1/2-3", "1/2, 1/1-2"])
def test_t1_16_duplicates_rejected(text):
    """重複が拒否される(UI-09)。"""
    with pytest.raises(ValueError) as info:
        parse_interface_list(text)
    assert any("重複" in message for message in info.value.args[0])


def test_t1_16_all_problems_are_reported():
    """複数の誤りがあれば、まとめて知らせる。"""
    with pytest.raises(ValueError) as info:
        parse_interface_list("1, 0/1, 1/2, 1/2")
    assert len(info.value.args[0]) == 3


# ---------------------------------------------------------------- T1-17
def test_t1_17_range_expansion():
    """「1/1-4」が、1/1、1/2、1/3、1/4 の4件に展開される(UI-09)。"""
    assert parse_interface_list("1/1-4") == (
        InterfaceId(1, 1),
        InterfaceId(1, 2),
        InterfaceId(1, 3),
        InterfaceId(1, 4),
    )


def test_t1_17_full_range():
    assert len(parse_interface_list("1/1-256")) == 256


# ---------------------------------------------------------------- T1-18
def test_t1_18_valid_device_is_accepted():
    data = validate_device_form(form(), EXISTING)
    assert data.hostname == "NEXUS-TEST-03"
    assert data.source_ip == "192.0.2.3"
    assert data.device_type == "cisco_nexus"
    assert data.commands == ALL_COMMANDS
    assert [i.ifnum for i in data.interfaces["counters"]] == ["1/1", "1/2"]
    assert [i.ifnum for i in data.interfaces["ptp"]] == ["1/1"]


@pytest.mark.parametrize("hostname", ["", "   "])
def test_t1_18_empty_hostname(hostname):
    """ホスト名の空欄が拒否される。"""
    assert "hostname" in errors_of(form(hostname=hostname))


def test_t1_18_duplicate_hostname():
    """ホスト名の重複が拒否される。"""
    assert "hostname" in errors_of(form(hostname="NEXUS-TEST-01"))


@pytest.mark.parametrize(
    "hostname",
    ["NEXUS/01", "NEXUS@01", "ネクサス", "NEXUS-TEST-01", "a\tb", "a<b>", "x" * 129, "NEXUS;01"],
)
def test_t1_18_invalid_hostname_characters(hostname):
    """使えない文字を含むホスト名、129文字以上のホスト名が拒否される。"""
    assert "hostname" in errors_of(form(hostname=hostname))


@pytest.mark.parametrize("hostname", ["NEXUS-TEST-03", "nexus.test_03", "NEXUS TEST 03", "x" * 128])
def test_t1_18_valid_hostname_characters(hostname):
    assert validate_device_form(form(hostname=hostname), EXISTING).hostname == hostname


def test_t1_18_hostname_surrounding_spaces_are_removed():
    assert validate_device_form(form(hostname="  NEXUS-TEST-03  "), EXISTING).hostname == "NEXUS-TEST-03"


def test_t1_18_reserved_hostname():
    """「telemetry」が拒否される。"""
    assert "hostname" in errors_of(form(hostname="telemetry"))


@pytest.mark.parametrize(
    "source_ip",
    ["", "192.0.2", "192.0.2.256", "192.0.2.1.5", "abc", "192.0.2.01", "2001:db8::1", "192.0.2.1/24", "192.0.2.3 "  * 20],
)
def test_t1_18_invalid_source_ip(source_ip):
    """送信元IPアドレスの形式の誤りが拒否される(IPv4のみ、11.5.5)。"""
    assert "source_ip" in errors_of(form(source_ip=source_ip))


def test_t1_18_duplicate_source_ip():
    """送信元IPアドレスの重複が拒否される。"""
    assert "source_ip" in errors_of(form(source_ip="192.0.2.1"))
    assert "source_ip" in errors_of(form(source_ip=" 192.0.2.2 "))


def test_t1_18_editing_self_is_not_duplicate():
    """編集中の機器自身とは、重複と見なさない。"""
    data = validate_device_form(form(hostname="NEXUS-TEST-01", source_ip="192.0.2.1"), EXISTING, self_id=1)
    assert data.hostname == "NEXUS-TEST-01"
    assert "hostname" in errors_of(form(hostname="NEXUS-TEST-02"), self_id=1)


@pytest.mark.parametrize(("column", "purpose"), [("cmd_counters_brief", "counters"), ("cmd_ptp_brief", "ptp")])
def test_t1_18_empty_interfaces_for_checked_command(column, purpose):
    """チェックしたコマンドのインターフェース番号の空欄が拒否される。"""
    texts = {"counters": "1/1", "ptp": "1/1"}
    texts[purpose] = "  "
    assert f"interfaces.{purpose}" in errors_of(form(interfaces_text=texts))


def test_t1_18_invalid_interfaces_for_checked_command():
    errors = errors_of(form(interfaces_text={"counters": "1/49/1", "ptp": "1/1"}))
    assert "interfaces.counters" in errors


def test_t1_18_no_command_checked():
    """4つのコマンドがすべて未チェックの状態が拒否される。"""
    assert "commands" in errors_of(form(commands=dict(NO_COMMANDS), interfaces_text={}))


def test_t1_18_unchecked_command_interfaces_are_ignored():
    """チェックを外したコマンドのインターフェース番号は、検査も保存もしない。"""
    commands = dict(ALL_COMMANDS, cmd_counters_brief=False)
    data = validate_device_form(form(commands=commands, interfaces_text={"counters": "でたらめ", "ptp": "1/1"}), EXISTING)
    assert data.interfaces["counters"] == ()
    assert [i.ifnum for i in data.interfaces["ptp"]] == ["1/1"]


def test_t1_18_device_level_commands_only():
    """機器単位のコマンドだけをチェックした場合は、インターフェース番号は不要。"""
    commands = dict(NO_COMMANDS, cmd_ptp_clock=True)
    data = validate_device_form(form(commands=commands, interfaces_text={}), EXISTING)
    assert data.interfaces == {"counters": (), "ptp": ()}


def test_t1_18_all_errors_reported_together():
    errors = errors_of(form(hostname="telemetry", source_ip="x", commands=dict(NO_COMMANDS)))
    assert {"hostname", "source_ip", "commands"} <= set(errors)


# ---------------------------------------------------------------- 全体設定の検査
def test_settings_valid():
    result = validate_settings(
        {"zabbix_server": " 192.0.2.100 ", "zabbix_port": "10051", "log_level": "warning", "stale_timeout_sec": "0300"}
    )
    assert result == {"zabbix_server": "192.0.2.100", "zabbix_port": "10051", "log_level": "WARNING", "stale_timeout_sec": "300"}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("zabbix_server", "zabbix.example"),
        ("zabbix_port", "0"),
        ("zabbix_port", "65536"),
        ("telemetry_udp_port", "abc"),
        ("stale_timeout_sec", "0"),
        ("stale_timeout_sec", "-1"),
        ("queue_max_size", "1.5"),
        ("lld_interval_sec", "99999999999"),
        ("log_level", "TRACE"),
        ("webgui_port", ""),
        ("unknown_key", "1"),
    ],
)
def test_settings_invalid(key, value):
    with pytest.raises(ValidationError):
        validate_settings({key: value})


@pytest.mark.parametrize(
    ("key", "minimum", "maximum"),
    [
        ("stale_timeout_sec", 30, 3_600),
        ("self_monitor_interval_sec", 10, 120),
        ("lld_interval_sec", 300, 86_400),
        ("queue_max_size", 1_000, 1_000_000),
    ],
)
def test_settings_ranges(key, minimum, maximum):
    """時間・件数の設定は、決められた範囲だけを受け付ける(決定事項 D-005)。"""
    assert validate_settings({key: str(minimum)}) == {key: str(minimum)}
    assert validate_settings({key: str(maximum)}) == {key: str(maximum)}
    for value in (minimum - 1, maximum + 1):
        with pytest.raises(ValidationError):
            validate_settings({key: str(value)})


def test_settings_defaults_are_within_ranges():
    from common.constants import SETTING_DEFAULTS

    defaults = {key: value for key, value in SETTING_DEFAULTS.items()}
    assert validate_settings(defaults).keys() == defaults.keys()


@pytest.mark.parametrize(
    "source_ip",
    ["0.0.0.0", "127.0.0.1", "127.255.255.255", "127.1.2.3", "255.255.255.255", "224.0.0.1", "239.255.255.255", "232.1.1.1"],
)
def test_t1_18_special_source_ip_rejected(source_ip):
    """機器には使わない特別なIPアドレスが拒否される(決定事項 D-007)。"""
    assert "source_ip" in errors_of(form(source_ip=source_ip))


@pytest.mark.parametrize("source_ip", ["0.0.0.1", "126.255.255.255", "128.0.0.1", "223.255.255.255", "240.0.0.1", "198.51.100.1"])
def test_t1_18_ordinary_source_ip_accepted(source_ip):
    assert validate_device_form(form(source_ip=source_ip), EXISTING).source_ip == source_ip


@pytest.mark.parametrize("hostname", ["Telemetry", "TELEMETRY", "teLEmetry"])
def test_t1_18_reserved_hostname_case_insensitive(hostname):
    """「telemetry」の禁止は、大文字・小文字を区別しない(決定事項 D-008)。"""
    assert "hostname" in errors_of(form(hostname=hostname))


@pytest.mark.parametrize("hostname", ["nexus-test-01", "Nexus-Test-01", "NEXUS-test-02"])
def test_t1_18_duplicate_hostname_case_insensitive(hostname):
    """機器どうしのホスト名の重複は、大文字・小文字を区別せずに判定する(決定事項 D-008)。"""
    assert "hostname" in errors_of(form(hostname=hostname))


def test_t1_18_changing_case_of_own_hostname():
    data = validate_device_form(form(hostname="nexus-test-01", source_ip="192.0.2.1"), EXISTING, self_id=1)
    assert data.hostname == "nexus-test-01"


def test_settings_zabbix_server_required_on_screen():
    """全体設定の画面では、ZabbixサーバーのIPアドレスは必須(7.4.5.1)。"""
    assert validate_settings({"zabbix_server": ""}) == {"zabbix_server": ""}
    with pytest.raises(ValidationError):
        validate_settings({"zabbix_server": ""}, require_zabbix_server=True)
