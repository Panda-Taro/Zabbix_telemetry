"""Zabbixのキーと、LLD用データの組み立ての自動テスト(要件定義書 12.2.1 T1-14、T1-15)。"""

import json

from collector.cisco_nexus.commands import COMMANDS, COUNTERS_BRIEF, PTP_BRIEF, PTP_CLOCK, PTP_PARENT
from collector.cisco_nexus.lld import build_lld_json, build_lld_values
from collector.models import stamp_values
from common.interfaces import InterfaceId
from tests.conftest import TEST_HOSTNAME, fixture_document

IF_1_1 = InterfaceId(1, 1)
IF_1_3 = InterfaceId(1, 3)


# ---------------------------------------------------------------- T1-14
def test_t1_14_keys_match_table():
    """9.2.3の表のとおりのキーが作られる(TX-05)。"""
    assert PTP_CLOCK.item_keys() == ("show.ptp.clock.clock-state", "show.ptp.clock.offset-from-master")
    assert PTP_PARENT.item_keys() == ("show.ptp.parent.gm-id",)
    assert PTP_BRIEF.item_keys(IF_1_1) == ("show.ptp.brief.state[1/1]",)
    assert COUNTERS_BRIEF.item_keys(IF_1_3) == (
        "show.interface.counters.brief.inrate1[1/3]",
        "show.interface.counters.brief.outrate1[1/3]",
    )


def test_t1_14_lld_rule_keys():
    """LLDルールのキーが、9.2.4.1の表のとおりである。"""
    assert COUNTERS_BRIEF.lld_key == "show.interface.counters.brief.discovery"
    assert PTP_BRIEF.lld_key == "show.ptp.brief.discovery"
    assert PTP_CLOCK.lld_key is None
    assert PTP_PARENT.lld_key is None


def test_t1_14_extracted_keys_match_table_and_exclude_hostname():
    """実際に取り出した値のキーが表のとおりで、ホスト名を含まない(TX-05、9.2.2.2)。"""
    expected = {
        "show ptp clock": {"show.ptp.clock.clock-state", "show.ptp.clock.offset-from-master"},
        "show ptp parent": {"show.ptp.parent.gm-id"},
        "show ptp brief": {"show.ptp.brief.state[1/1]"},
        "show interface counters brief": {
            "show.interface.counters.brief.inrate1[1/1]",
            "show.interface.counters.brief.outrate1[1/1]",
        },
    }
    for name, spec in COMMANDS.items():
        document = fixture_document(name)
        result = spec.extract(document["data"], [IF_1_1])
        values = stamp_values(document["node_id_str"], result.values, 0)
        assert {v.key for v in values} == expected[name]
        for value in values:
            # どのホストのデータかは、キーではなく送信先のホスト名で判別する(4.3.3)
            assert value.host == TEST_HOSTNAME
            assert TEST_HOSTNAME not in value.key
            assert TEST_HOSTNAME.lower() not in value.key.lower()


# ---------------------------------------------------------------- T1-15
def test_t1_15_lld_json_format():
    """9.2.4.2の形式のJSONが作られる(LD-02)。"""
    text = build_lld_json([IF_1_3, IF_1_1])
    assert json.loads(text) == [
        {"{#IFNUM}": "1/1", "{#IFNAME}": "Ethernet1/1"},
        {"{#IFNUM}": "1/3", "{#IFNAME}": "Ethernet1/3"},
    ]


def test_t1_15_lld_json_empty():
    """0件の場合に空の一覧が作られる(LD-03)。"""
    assert build_lld_json([]) == "[]"
    assert json.loads(build_lld_json([])) == []


def test_t1_15_lld_values_per_rule():
    """LLDルールごとに、送信先ホスト名と時刻を付けて組み立てる(LD-01、LD-02)。"""
    values = build_lld_values(
        TEST_HOSTNAME,
        {"show interface counters brief": [IF_1_1, IF_1_3], "show ptp brief": [IF_1_1]},
        sent_at_ns=1_800_000_000_000_000_000,
    )
    by_key = {v.key: v for v in values}
    assert set(by_key) == {"show.interface.counters.brief.discovery", "show.ptp.brief.discovery"}
    assert json.loads(by_key["show.interface.counters.brief.discovery"].value) == [
        {"{#IFNUM}": "1/1", "{#IFNAME}": "Ethernet1/1"},
        {"{#IFNUM}": "1/3", "{#IFNAME}": "Ethernet1/3"},
    ]
    assert json.loads(by_key["show.ptp.brief.discovery"].value) == [{"{#IFNUM}": "1/1", "{#IFNAME}": "Ethernet1/1"}]
    for value in values:
        assert value.host == TEST_HOSTNAME
        assert value.clock == 1_800_000_000


def test_t1_15_unchecked_command_sends_empty_list():
    """チェックを外したコマンドは、空の一覧を送る(LD-03)。"""
    values = build_lld_values(TEST_HOSTNAME, {"show interface counters brief": [IF_1_3]}, sent_at_ns=0)
    by_key = {v.key: v.value for v in values}
    assert by_key["show.ptp.brief.discovery"] == "[]"
    assert json.loads(by_key["show.interface.counters.brief.discovery"]) == [
        {"{#IFNUM}": "1/3", "{#IFNAME}": "Ethernet1/3"}
    ]


def test_t1_15_zero_interfaces_sends_empty_list():
    """登録したインターフェースが0件のコマンドも、空の一覧を送る(LD-03)。"""
    values = build_lld_values(TEST_HOSTNAME, {"show interface counters brief": [], "show ptp brief": []}, sent_at_ns=0)
    assert {v.key: v.value for v in values} == {
        "show.interface.counters.brief.discovery": "[]",
        "show.ptp.brief.discovery": "[]",
    }
