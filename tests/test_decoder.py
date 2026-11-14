"""JSONの取り出しとコマンドの判別の自動テスト(要件定義書 12.2.1 T1-12、T1-13)。"""

import pytest

from collector.cisco_nexus.commands import COMMANDS, PTP_BRIEF, PTP_CLOCK, identify_command
from collector.cisco_nexus.decoder import InvalidDataError, decode_packet
from common.constants import MAX_TELEMETRY_PAYLOAD_BYTES
from tests.conftest import FIXTURE_FILES, TEST_HOSTNAME, fixture_bytes, fixture_document, to_payload

# NX-OS が付けるヘッダーの代わり(中身は意味のない固定のバイト列)
FAKE_HEADER = bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x2A, 0x00, 0x10, 0x7F, 0xFF])


# ---------------------------------------------------------------- T1-12
@pytest.mark.parametrize("command", list(FIXTURE_FILES))
def test_t1_12_four_commands_identified(command):
    """4つのコマンド名が完全一致で判別される(RX-06)。"""
    message = decode_packet(fixture_bytes(command))
    spec = identify_command(message.encoding_path)
    assert spec is not None
    assert spec.name == command


def test_t1_12_surrounding_whitespace_is_ignored():
    """前後の空白は除いてから判別する(9.1.2.2)。"""
    assert identify_command("  show ptp clock\n") is PTP_CLOCK
    assert identify_command("\tshow ptp brief ") is PTP_BRIEF


@pytest.mark.parametrize(
    "encoding_path",
    [
        "show ptp clock detail",
        "show ptp",
        "ptp clock",
        "show ptp clocks",
        "Show PTP Clock",
        "show  ptp clock",
        "show interface counters",
        "show interface counters brief detail",
        "show nbm interface bandwidth-utilized",
        "sys/ptp",
        "",
    ],
)
def test_t1_12_similar_names_are_unknown(encoding_path):
    """似た名前は、4つ以外のコマンドとして扱われる(RX-06)。"""
    assert identify_command(encoding_path) is None


def test_t1_12_only_four_commands():
    assert set(COMMANDS) == set(FIXTURE_FILES)


# ---------------------------------------------------------------- T1-13
@pytest.mark.parametrize("command", list(FIXTURE_FILES))
def test_t1_13_json_after_header(command):
    """先頭にヘッダーが付いたデータから、JSONが取り出される(RX-02)。"""
    message = decode_packet(FAKE_HEADER + fixture_bytes(command))
    assert message.node_id == TEST_HOSTNAME
    assert message.encoding_path == command
    assert message.data == fixture_document(command)["data"]


def test_t1_13_without_header():
    message = decode_packet(fixture_bytes("show ptp clock"))
    assert message.data["clock-state"] == "Locked"


def _deep_nesting() -> bytes:
    return b'{"node_id_str":"x","encoding_path":"show ptp clock","data":' + b'{"a":' * 100_000 + b"1" + b"}" * 100_001


BROKEN_PAYLOADS = {
    "空": b"",
    "{がない": b"no json here",
    "ヘッダーだけ": FAKE_HEADER,
    "途中で切れている": fixture_bytes("show ptp clock")[:200],
    "末尾に余計なもの": fixture_bytes("show ptp clock") + b"garbage",
    "UTF-8でない": b'{"node_id_str":"\xff\xfe","encoding_path":"show ptp clock","data":{}}',
    "最上位が配列": b'[{"node_id_str":"x"}]',
    "最上位が文字列": b'{"a"',
    "node_id_strがない": b'{"encoding_path":"show ptp clock","data":{}}',
    "node_id_strが空": b'{"node_id_str":"","encoding_path":"show ptp clock","data":{}}',
    "node_id_strが数値": b'{"node_id_str":1,"encoding_path":"show ptp clock","data":{}}',
    "encoding_pathがない": b'{"node_id_str":"x","data":{}}',
    "encoding_pathが配列": b'{"node_id_str":"x","encoding_path":["show ptp clock"],"data":{}}',
    "dataがない": b'{"node_id_str":"x","encoding_path":"show ptp clock"}',
    "dataが配列": b'{"node_id_str":"x","encoding_path":"show ptp clock","data":[]}',
    "dataがnull": b'{"node_id_str":"x","encoding_path":"show ptp clock","data":null}',
    "入れ子が深すぎる": _deep_nesting(),
    "大きすぎる": b"{" + b" " * MAX_TELEMETRY_PAYLOAD_BYTES + b"}",
}


@pytest.mark.parametrize("payload", list(BROKEN_PAYLOADS.values()), ids=list(BROKEN_PAYLOADS))
def test_t1_13_broken_data_is_rejected(payload):
    """壊れたデータは InvalidDataError になり、それ以外のエラーで止まらない(RX-02、RX-09)。"""
    with pytest.raises(InvalidDataError):
        decode_packet(payload)


def test_t1_13_processing_continues_after_broken_data():
    """壊れたデータの後に届いた正常なデータを、続けて処理できる(RX-09)。"""
    packets = [
        b"garbage",
        FAKE_HEADER + fixture_bytes("show ptp clock"),
        fixture_bytes("show ptp parent")[:50],
        FAKE_HEADER + fixture_bytes("show ptp parent"),
    ]
    decoded = []
    invalid = 0
    for packet in packets:
        try:
            decoded.append(decode_packet(packet))
        except InvalidDataError:
            invalid += 1
    assert invalid == 2
    assert [m.encoding_path for m in decoded] == ["show ptp clock", "show ptp parent"]


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"TABLE_ptp": "x"},
        {"TABLE_ptp": {"ROW_ptp": 5}},
        {"TABLE_ptp": {"ROW_ptp": [1, "a", None, [], {"ptp-ifindex": 1}]}},
        {"TABLE_ptp": {"ROW_ptp": [{"ptp-ifindex": "Eth1/1", "state": {"x": 1}}]}},
        {"TABLE_interface": {"ROW_interface": [{"interface": "Ethernet1/1", "eth_inrate1": [1]}]}},
        {"clock-state": ["Locked"], "offset-from-master": {"v": 1}, "gm-id": 0},
    ],
)
@pytest.mark.parametrize("spec", list(COMMANDS.values()), ids=list(COMMANDS))
def test_t1_13_unexpected_shapes_do_not_raise(spec, data):
    """dataの形が想定と違っても、値の取り出しで例外を出さない(RX-09)。"""
    from common.interfaces import InterfaceId

    result = spec.extract(data, [InterfaceId(1, 1)])
    assert result.values == ()


def test_t1_13_payload_roundtrip_with_header():
    document = fixture_document("show ptp brief")
    message = decode_packet(to_payload(document, header=FAKE_HEADER))
    assert message.data == document["data"]
