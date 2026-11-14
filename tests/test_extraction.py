"""値の取り出しと変換の自動テスト(要件定義書 12.2.1 T1-01〜T1-11、CV-12)。"""

from typing import Any

import pytest

from collector.cisco_nexus.commands import COUNTERS_BRIEF, PTP_BRIEF, PTP_CLOCK, PTP_PARENT
from collector.cisco_nexus.values import ValueKind, convert_value
from collector.models import KeyValue, stamp_values
from common.interfaces import InterfaceId, parse_device_ifname
from tests.conftest import TEST_HOSTNAME, fixture_document


def ifs(*numbers: str) -> list[InterfaceId]:
    """「1/1」などの表記から、インターフェース番号の一覧を作る(テスト用)。"""
    result = []
    for number in numbers:
        slot, port = number.split("/")
        result.append(InterfaceId(int(slot), int(port)))
    return result


def as_dict(values: tuple[KeyValue, ...]) -> dict[str, Any]:
    return {kv.key: kv.value for kv in values}


def find_row(rows: list[dict[str, Any]], field: str, name: str) -> dict[str, Any]:
    return next(row for row in rows if row[field] == name)


# ---------------------------------------------------------------- T1-01
def test_t1_01_ptp_clock():
    """show ptp clock から clock-state と offset-from-master を取り出す(CV-01)。"""
    data = fixture_document("show ptp clock")["data"]
    result = PTP_CLOCK.extract(data)
    assert as_dict(result.values) == {
        "show.ptp.clock.clock-state": "Locked",
        "show.ptp.clock.offset-from-master": -4,
    }
    assert result.missing_interfaces == ()


# ---------------------------------------------------------------- T1-02
def test_t1_02_ptp_parent_takes_gm_id_not_clock_id():
    """show ptp parent から gm-id を取り出し、clock-id は取り出さない(CV-02)。

    テストデータでは clock-id と gm-id が同じ値のため、取り違えを見分けられるよう、
    clock-id だけを別の架空の値に書き換えて確かめる。
    """
    data = fixture_document("show ptp parent")["data"]
    data["clock-id"] = "00:00:5e:ff:fe:00:53:02"
    result = PTP_PARENT.extract(data)
    assert as_dict(result.values) == {"show.ptp.parent.gm-id": "00:00:5e:ff:fe:00:53:ff"}
    assert "00:00:5e:ff:fe:00:53:02" not in [kv.value for kv in result.values]


# ---------------------------------------------------------------- T1-03
def test_t1_03_ptp_brief_only_registered():
    """show ptp brief から、登録した番号の state のみを取り出す(CV-03、CV-06)。"""
    data = fixture_document("show ptp brief")["data"]
    result = PTP_BRIEF.extract(data, ifs("1/1"))
    assert as_dict(result.values) == {"show.ptp.brief.state[1/1]": "slave"}
    assert result.missing_interfaces == ()


def test_t1_03_ptp_brief_multiple():
    data = fixture_document("show ptp brief")["data"]
    result = PTP_BRIEF.extract(data, ifs("1/3", "1/9"))
    assert as_dict(result.values) == {
        "show.ptp.brief.state[1/3]": "master",
        "show.ptp.brief.state[1/9]": "disabled",
    }


def test_t1_03_ptp_brief_nothing_registered():
    data = fixture_document("show ptp brief")["data"]
    result = PTP_BRIEF.extract(data, [])
    assert result.values == ()
    assert result.missing_interfaces == ()


# ---------------------------------------------------------------- T1-04
def test_t1_04_counters_brief_only_registered():
    """show interface counters brief から、登録した番号の inrate1 と outrate1 のみを取り出す(CV-04)。"""
    data = fixture_document("show interface counters brief")["data"]
    result = COUNTERS_BRIEF.extract(data, ifs("1/3"))
    assert as_dict(result.values) == {
        "show.interface.counters.brief.inrate1[1/3]": 1319 * 8_000_000,
        "show.interface.counters.brief.outrate1[1/3]": 329 * 8_000_000,
    }
    assert result.missing_interfaces == ()


def test_t1_04_counters_brief_does_not_use_5min_average():
    """5分間平均(eth_inrate2 など)は使わない(9.1.6.2)。"""
    document = fixture_document("show interface counters brief")
    row = find_row(document["data"]["TABLE_interface"]["ROW_interface"], "interface", "Ethernet1/3")
    row["eth_inrate2"] = 9999
    row["eth_outrate2"] = 9999
    result = COUNTERS_BRIEF.extract(document["data"], ifs("1/3"))
    assert len(result.values) == 2
    assert 9999 * 8_000_000 not in [kv.value for kv in result.values]


# ---------------------------------------------------------------- T1-05
def test_t1_05_bandwidth_conversion():
    """1319 MB/秒が 10,552,000,000 bps に換算される(CV-05)。"""
    assert convert_value(1319, ValueKind.BANDWIDTH) == 10_552_000_000
    assert convert_value(0, ValueKind.BANDWIDTH) == 0


def test_t1_05_bandwidth_conversion_is_integer():
    """小数で届いた場合も、整数にして送る(段階0の質問2の回答)。"""
    value = convert_value(1319.5, ValueKind.BANDWIDTH)
    assert value == 10_556_000_000
    assert isinstance(value, int)
    assert convert_value(0.0000001, ValueKind.BANDWIDTH) == 1  # 0.8 bps → 1


# ---------------------------------------------------------------- T1-06
@pytest.mark.parametrize("name", ["Eth1/1", "Ethernet1/1"])
def test_t1_06_interface_name_variants(name):
    """「Eth1/1」と「Ethernet1/1」が、どちらも番号 1/1 と一致する(CV-06)。"""
    assert parse_device_ifname(name) == InterfaceId(1, 1)


def test_t1_06_both_commands_match_same_number():
    ptp = PTP_BRIEF.extract(fixture_document("show ptp brief")["data"], ifs("1/1"))
    counters = COUNTERS_BRIEF.extract(fixture_document("show interface counters brief")["data"], ifs("1/1"))
    assert "show.ptp.brief.state[1/1]" in as_dict(ptp.values)
    assert "show.interface.counters.brief.inrate1[1/1]" in as_dict(counters.values)


def test_t1_06_ifname_and_ifnum():
    interface = InterfaceId(1, 48)
    assert interface.ifnum == "1/48"
    assert interface.ifname == "Ethernet1/48"


# ---------------------------------------------------------------- T1-07
@pytest.mark.parametrize(
    "name",
    ["mgmt0", "port-channel1", "Ethernet1/49/1", "Eth1/49/1", "Vlan10", "loopback0", "", None, 12, "Ethernet1", "Eth 1/1"],
)
def test_t1_07_unsupported_interface_name(name):
    """形式に当てはまらない名前は、番号として読み取らない(CV-07)。"""
    assert parse_device_ifname(name) is None


def test_t1_07_unsupported_rows_are_ignored():
    """mgmt0、port-channel1、Ethernet1/49/1 の行が、エラーにならずに無視される(CV-07)。"""
    document = fixture_document("show interface counters brief")
    rows = document["data"]["TABLE_interface"]["ROW_interface"]
    rows[:] = [row for row in rows if row["interface"] != "Ethernet1/49"]
    template = dict(find_row(rows, "interface", "Ethernet1/3"))
    for name in ("mgmt0", "port-channel1", "Ethernet1/49/1"):
        rows.append({**template, "interface": name})

    result = COUNTERS_BRIEF.extract(document["data"], ifs("1/3", "1/49"))
    values = as_dict(result.values)
    # 正しい行の値は取り出せる
    assert values["show.interface.counters.brief.inrate1[1/3]"] == 10_552_000_000
    # Ethernet1/49/1 は 1/49 として扱わない
    assert "show.interface.counters.brief.inrate1[1/49]" not in values
    assert result.missing_interfaces == (InterfaceId(1, 49),)


def test_t1_07_ptp_brief_unsupported_rows_are_ignored():
    document = fixture_document("show ptp brief")
    document["data"]["TABLE_ptp"]["ROW_ptp"].append({"ptp-ifindex": "Eth1/1/1", "state": "master"})
    document["data"]["TABLE_ptp"]["ROW_ptp"].append({"ptp-ifindex": "port-channel1", "state": "master"})
    result = PTP_BRIEF.extract(document["data"], ifs("1/1"))
    assert as_dict(result.values) == {"show.ptp.brief.state[1/1]": "slave"}


# ---------------------------------------------------------------- T1-08
def test_t1_08_single_row_ptp_brief():
    """ROW_ptp が配列でなく単独で届いても、値が取り出される(CV-08)。"""
    document = fixture_document("show ptp brief")
    document["data"]["TABLE_ptp"]["ROW_ptp"] = {"ptp-ifindex": "Eth1/1", "state": "slave"}
    result = PTP_BRIEF.extract(document["data"], ifs("1/1"))
    assert as_dict(result.values) == {"show.ptp.brief.state[1/1]": "slave"}


def test_t1_08_single_row_counters_brief():
    """ROW_interface が配列でなく単独で届いても、値が取り出される(CV-08)。"""
    document = fixture_document("show interface counters brief")
    rows = document["data"]["TABLE_interface"]["ROW_interface"]
    document["data"]["TABLE_interface"]["ROW_interface"] = find_row(rows, "interface", "Ethernet1/3")
    result = COUNTERS_BRIEF.extract(document["data"], ifs("1/3"))
    assert as_dict(result.values) == {
        "show.interface.counters.brief.inrate1[1/3]": 10_552_000_000,
        "show.interface.counters.brief.outrate1[1/3]": 2_632_000_000,
    }


# ---------------------------------------------------------------- T1-09
def test_t1_09_missing_counters_value_is_not_sent():
    """項目のないデータで、その値が送信対象に含まれない。0が送られない(CV-09)。"""
    document = fixture_document("show interface counters brief")
    row = find_row(document["data"]["TABLE_interface"]["ROW_interface"], "interface", "Ethernet1/3")
    del row["eth_inrate1"]
    result = COUNTERS_BRIEF.extract(document["data"], ifs("1/3"))
    assert as_dict(result.values) == {"show.interface.counters.brief.outrate1[1/3]": 2_632_000_000}
    # 行そのものは存在するので、不整合ではない
    assert result.missing_interfaces == ()


def test_t1_09_missing_ptp_clock_value_is_not_sent():
    data = fixture_document("show ptp clock")["data"]
    del data["offset-from-master"]
    result = PTP_CLOCK.extract(data)
    assert as_dict(result.values) == {"show.ptp.clock.clock-state": "Locked"}


def test_t1_09_missing_all_values():
    data = fixture_document("show ptp clock")["data"]
    del data["offset-from-master"]
    del data["clock-state"]
    assert PTP_CLOCK.extract(data).values == ()
    parent = fixture_document("show ptp parent")["data"]
    del parent["gm-id"]
    assert PTP_PARENT.extract(parent).values == ()


def test_t1_09_missing_ptp_state_is_not_sent():
    document = fixture_document("show ptp brief")
    del document["data"]["TABLE_ptp"]["ROW_ptp"][0]["state"]
    result = PTP_BRIEF.extract(document["data"], ifs("1/1"))
    assert result.values == ()
    assert result.missing_interfaces == ()


@pytest.mark.parametrize("kind", list(ValueKind))
def test_t1_09_null_value_is_not_sent(kind):
    assert convert_value(None, kind) is None


# ---------------------------------------------------------------- T1-10
def test_t1_10_not_a_number_is_not_sent():
    """「N/A」が送信対象に含まれない(CV-10)。"""
    document = fixture_document("show interface counters brief")
    row = find_row(document["data"]["TABLE_interface"]["ROW_interface"], "interface", "Ethernet1/3")
    row["eth_inrate1"] = "N/A"
    result = COUNTERS_BRIEF.extract(document["data"], ifs("1/3"))
    assert as_dict(result.values) == {"show.interface.counters.brief.outrate1[1/3]": 2_632_000_000}


def test_t1_10_numeric_string_is_converted():
    """文字列の "1319" が数値として扱われる(CV-10)。"""
    document = fixture_document("show interface counters brief")
    row = find_row(document["data"]["TABLE_interface"]["ROW_interface"], "interface", "Ethernet1/3")
    row["eth_inrate1"] = "1319"
    result = COUNTERS_BRIEF.extract(document["data"], ifs("1/3"))
    assert as_dict(result.values)["show.interface.counters.brief.inrate1[1/3]"] == 10_552_000_000


def test_t1_10_offset_from_master():
    data = fixture_document("show ptp clock")["data"]
    data["offset-from-master"] = "N/A"
    assert as_dict(PTP_CLOCK.extract(data).values) == {"show.ptp.clock.clock-state": "Locked"}
    data["offset-from-master"] = "-4"
    assert as_dict(PTP_CLOCK.extract(data).values)["show.ptp.clock.offset-from-master"] == -4
    data["offset-from-master"] = "12.5"
    assert as_dict(PTP_CLOCK.extract(data).values)["show.ptp.clock.offset-from-master"] == 12.5


@pytest.mark.parametrize("raw", ["N/A", "", " ", "abc", "1,319", "0x10", "nan", "inf", True, False, [], {}, float("nan"), float("inf")])
@pytest.mark.parametrize("kind", [ValueKind.NUMBER, ValueKind.BANDWIDTH])
def test_t1_10_unreadable_numbers(raw, kind):
    assert convert_value(raw, kind) is None


def test_t1_10_text_value_must_be_string():
    """文字列の項目に、文字列以外や長すぎる文字列が届いた場合は送らない。"""
    assert convert_value("Locked", ValueKind.TEXT) == "Locked"
    assert convert_value(1, ValueKind.TEXT) is None
    assert convert_value({"a": 1}, ValueKind.TEXT) is None
    assert convert_value("x" * 256, ValueKind.TEXT) is None


# ---------------------------------------------------------------- T1-11
def test_t1_11_registered_interface_not_in_data():
    """登録した番号がデータにない場合、不整合として記録される(CV-11)。"""
    counters = COUNTERS_BRIEF.extract(fixture_document("show interface counters brief")["data"], ifs("1/3", "1/99"))
    assert counters.missing_interfaces == (InterfaceId(1, 99),)
    assert "show.interface.counters.brief.inrate1[1/99]" not in as_dict(counters.values)

    ptp = PTP_BRIEF.extract(fixture_document("show ptp brief")["data"], ifs("1/1", "1/2", "1/10"))
    assert ptp.missing_interfaces == (InterfaceId(1, 2), InterfaceId(1, 10))
    assert as_dict(ptp.values) == {"show.ptp.brief.state[1/1]": "slave"}


def test_t1_11_table_missing_means_all_missing():
    result = PTP_BRIEF.extract({"gptp-flag": 0}, ifs("1/1"))
    assert result.values == ()
    assert result.missing_interfaces == (InterfaceId(1, 1),)


# ---------------------------------------------------------------- CV-12
def test_cv_12_values_carry_receive_time():
    """取り出した値に、中間サーバーでの受信時刻を付ける(CV-12)。Nexus側の時刻は使わない。"""
    document = fixture_document("show ptp clock")
    received_at_ns = 1_800_000_000_123_456_789
    result = PTP_CLOCK.extract(document["data"])
    values = stamp_values(TEST_HOSTNAME, result.values, received_at_ns)
    assert len(values) == 2
    for value in values:
        assert value.host == TEST_HOSTNAME
        assert value.clock == 1_800_000_000
        assert value.ns == 123_456_789
        assert value.clock != int(document["msg_timestamp"]) // 1000
