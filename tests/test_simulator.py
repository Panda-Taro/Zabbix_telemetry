"""模擬送信ツールの自動テスト(段階4、DL-08、要件定義書 12.1.2)。

段階3の collector に、このツールで実際にUDPで送り、要件どおりに扱われることも確かめる。
"""

import asyncio
import io
import json

import pytest

from collector.cisco_nexus.commands import COMMANDS, identify_command
from collector.cisco_nexus.decoder import InvalidDataError, decode_packet
from collector.stats import RECEIVED, SENT_OK, SKIPPED, dropped
from common.database import connect, transaction
from common.interfaces import InterfaceId
from tests.collector_support import FakeClock, free_udp_port, prepare_db, started_app, wait_until
from tools import telemetry_simulator as sim

FIXTURES = sim.load_fixtures(sim.DEFAULT_FIXTURES_DIR)


def payload(command: str, abnormal: str | None = None, header: bool = True, hostname: str = "NEXUS-TEST-07") -> bytes:
    return sim.build_payload(FIXTURES[command], command, hostname, 1234, abnormal, header, 1_800_000_000_000)


def args_for(*argv: str):  # noqa: ANN201
    return sim.build_parser().parse_args(list(argv))


# ---------------------------------------------------------------- 指定の読み取り
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("192.0.2.11", ["192.0.2.11"]),
        ("192.0.2.11,192.0.2.12", ["192.0.2.11", "192.0.2.12"]),
        ("192.0.2.11-192.0.2.13", ["192.0.2.11", "192.0.2.12", "192.0.2.13"]),
        ("192.0.2.11-13", ["192.0.2.11", "192.0.2.12", "192.0.2.13"]),
        ("192.0.2.11-12, 192.0.2.20", ["192.0.2.11", "192.0.2.12", "192.0.2.20"]),
    ],
)
def test_parse_source_ips(text, expected):
    assert sim.parse_source_ips(text) == expected


@pytest.mark.parametrize("text", ["192.0.2", "abc", "192.0.2.20-192.0.2.11", "192.0.2.1-x"])
def test_parse_source_ips_errors(text):
    with pytest.raises(sim.SimulatorError):
        sim.parse_source_ips(text)


def test_hostnames_and_sources():
    devices = sim.make_devices(args_for("--target", "192.0.2.100", "--devices", "3", "--source-ips", "192.0.2.11-13"))
    assert [(d.hostname, d.source_ip) for d in devices] == [
        ("NEXUS-TEST-01", "192.0.2.11"),
        ("NEXUS-TEST-02", "192.0.2.12"),
        ("NEXUS-TEST-03", "192.0.2.13"),
    ]
    devices = sim.make_devices(args_for("--target", "x", "--devices", "2", "--hostnames", "NEXUS-TEST-08,NEXUS-TEST-09"))
    assert [d.hostname for d in devices] == ["NEXUS-TEST-08", "NEXUS-TEST-09"]
    devices = sim.make_devices(args_for("--target", "x", "--devices", "2", "--start-number", "11"))
    assert [d.hostname for d in devices] == ["NEXUS-TEST-11", "NEXUS-TEST-12"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--devices", "3", "--source-ips", "192.0.2.11-12"],  # 送信元の数が足りない
        ["--devices", "2", "--hostnames", "NEXUS-TEST-01"],  # ホスト名の数が合わない
        ["--hostname-format", "NEXUS-{x}"],
    ],
)
def test_device_errors(argv):
    with pytest.raises(sim.SimulatorError):
        sim.make_devices(args_for("--target", "192.0.2.100", *argv))


def test_parse_commands():
    assert sim.parse_commands("ptp-clock, counters") == ["show ptp clock", "show interface counters brief"]
    with pytest.raises(sim.SimulatorError):
        sim.parse_commands("ptp")


# ---------------------------------------------------------------- 送るデータ
@pytest.mark.parametrize("command", list(sim.FIXTURE_FILES))
def test_normal_payload_is_accepted_by_collector(command):
    """通常のデータは、collector で正しく解析でき、値が取り出せる。"""
    message = decode_packet(payload(command))
    assert message.node_id == "NEXUS-TEST-07"
    assert identify_command(message.encoding_path).name == command
    targets = [InterfaceId(1, 1), InterfaceId(1, 3)]
    assert COMMANDS[command].extract(message.data, targets).values != ()


def test_payload_has_header_unless_disabled():
    assert payload("show ptp clock").startswith(sim.FAKE_HEADER)
    assert payload("show ptp clock", header=False).startswith(b"{")


def test_abnormal_broken():
    with pytest.raises(InvalidDataError):
        decode_packet(payload("show ptp clock", "broken"))


@pytest.mark.parametrize("command", list(sim.FIXTURE_FILES))
def test_abnormal_unknown_command(command):
    message = decode_packet(payload(command, "unknown-command"))
    assert identify_command(message.encoding_path) is None


@pytest.mark.parametrize("command", list(sim.FIXTURE_FILES))
def test_abnormal_missing_values(command):
    """値が欠けたデータからは、値が取り出されない(0で埋められない)。"""
    message = decode_packet(payload(command, "missing-values"))
    spec = identify_command(message.encoding_path)
    result = spec.extract(message.data, [InterfaceId(1, 1), InterfaceId(1, 3)])
    assert result.values == ()
    assert result.missing_interfaces == ()  # 行はあるので、不整合ではない


def test_abnormal_na_values():
    clock = decode_packet(payload("show ptp clock", "na-values"))
    values = {kv.key for kv in COMMANDS["show ptp clock"].extract(clock.data).values}
    assert values == {"show.ptp.clock.clock-state"}  # offset-from-master(N/A)は送られない
    counters = decode_packet(payload("show interface counters brief", "na-values"))
    assert COMMANDS["show interface counters brief"].extract(counters.data, [InterfaceId(1, 3)]).values == ()


def test_expand_ports():
    for command, table, row in (
        ("show interface counters brief", "TABLE_interface", "ROW_interface"),
        ("show ptp brief", "TABLE_ptp", "ROW_ptp"),
    ):
        document = json.loads(json.dumps(FIXTURES[command]))
        sim.expand_ports(document, command, 54)
        rows = document["data"][table][row]
        assert len(rows) == 54
        extraction = COMMANDS[command].extract(document["data"], [InterfaceId(1, p) for p in range(1, 55)])
        assert extraction.missing_interfaces == ()
    # 元のテストデータの値は保たれる
    document = json.loads(json.dumps(FIXTURES["show interface counters brief"]))
    sim.expand_ports(document, "show interface counters brief", 4)
    assert document["data"]["TABLE_interface"]["ROW_interface"][2]["eth_inrate1"] == 1319


def test_run_argument_errors():
    for argv in (["--devices", "0"], ["--interval", "0"], ["--port", "70000"], ["--ports", "300"]):
        with pytest.raises(sim.SimulatorError):
            sim.run(args_for("--target", "127.0.0.1", *argv), out=io.StringIO())


def test_main_reports_error(capsys):
    assert sim.main(["--target", "127.0.0.1", "--devices", "2", "--source-ips", "192.0.2.1"]) == 1
    assert "エラー" in capsys.readouterr().err


# ---------------------------------------------------------------- collector に送って確かめる
def _insert_devices(db, rows) -> None:  # noqa: ANN001
    """送信元 127.0.0.x の機器を直接登録する(画面からは登録できないアドレスのため、テストでのみ行う)。"""
    conn = connect(db)
    with transaction(conn):
        for hostname, source_ip, commands in rows:
            cursor = conn.execute(
                "INSERT INTO devices (hostname, source_ip, device_type, cmd_counters_brief, cmd_ptp_brief,"
                " cmd_ptp_clock, cmd_ptp_parent, created_at, updated_at)"
                " VALUES (?, ?, 'cisco_nexus', ?, ?, ?, ?, 'x', 'x')",
                (hostname, source_ip, *commands),
            )
            conn.execute(
                "INSERT INTO device_interfaces (device_id, purpose, slot, port) VALUES (?, 'counters', 1, 3)",
                (cursor.lastrowid,),
            )
            conn.execute(
                "INSERT INTO device_interfaces (device_id, purpose, slot, port) VALUES (?, 'ptp', 1, 1)",
                (cursor.lastrowid,),
            )
    conn.close()


def test_simulator_to_collector(tmp_path):
    """模擬送信ツールで2台分を送ると、collector が受信し、2台分の値をZabbixへ送る。"""
    db = tmp_path / "telemetry.db"
    port = free_udp_port()
    prepare_db(db, settings={"telemetry_udp_port": str(port)})
    _insert_devices(db, [("NEXUS-TEST-01", "127.0.0.1", (1, 1, 1, 1)), ("NEXUS-TEST-02", "127.0.0.2", (1, 1, 1, 0))])
    out = io.StringIO()

    async def scenario():
        async with started_app(db, clock=FakeClock(), use_sources=True, run_sender_task=True) as (app, sink, _):
            args = args_for(
                "--target", "127.0.0.1", "--port", str(port), "--devices", "2",
                "--source-ips", "127.0.0.1,127.0.0.2", "--count", "2", "--interval", "0.2", "--quiet",
            )  # fmt: skip
            summary = await asyncio.to_thread(sim.run, args, out)
            assert summary == {"sent": 16, "errors": 0}
            await wait_until(lambda: app.runtime.counters.recent()[RECEIVED] == 16)
            await wait_until(lambda: len(app.runtime.queue) == 0)
            counts = app.runtime.counters.recent()
            assert counts[dropped("unknown_source")] == 0
            assert counts[dropped("invalid_data")] == 0
            assert counts[SKIPPED] == 2  # NEXUS-TEST-02 は show ptp parent のチェックを外している
            assert counts[SENT_OK] == len(sink.values)
            keys = {(v.host, v.key) for v in sink.values}
            assert ("NEXUS-TEST-01", "show.ptp.parent.gm-id") in keys
            assert ("NEXUS-TEST-02", "show.ptp.parent.gm-id") not in keys
            assert ("NEXUS-TEST-02", "show.interface.counters.brief.inrate1[1/3]") in keys

    asyncio.run(scenario())
    assert "合計 16 パケット" in out.getvalue()


@pytest.mark.parametrize(
    ("abnormal", "reason"),
    [("broken", "invalid_data"), ("unknown-command", "unknown_command")],
)
def test_simulator_abnormal_to_collector(tmp_path, abnormal, reason):
    """異常なデータを送ると、collector が理由別に数えて破棄する。"""
    db = tmp_path / "telemetry.db"
    port = free_udp_port()
    prepare_db(db, settings={"telemetry_udp_port": str(port)})
    _insert_devices(db, [("NEXUS-TEST-01", "127.0.0.1", (1, 1, 1, 1))])

    async def scenario():
        async with started_app(db, use_sources=True) as (app, sink, _):
            args = args_for("--target", "127.0.0.1", "--port", str(port), "--count", "1", "--abnormal", abnormal, "--quiet")
            await asyncio.to_thread(sim.run, args, io.StringIO())
            await wait_until(lambda: app.runtime.counters.recent()[RECEIVED] == 4)
            assert app.runtime.counters.recent()[dropped(reason)] == 4

    asyncio.run(scenario())


def test_simulator_wrong_hostname_is_unknown_source(tmp_path):
    """登録と違うホスト名で送ると、未登録の送信元として扱われ、届いたホスト名が記録される。"""
    db = tmp_path / "telemetry.db"
    port = free_udp_port()
    prepare_db(db, settings={"telemetry_udp_port": str(port)})
    _insert_devices(db, [("NEXUS-TEST-01", "127.0.0.1", (1, 1, 1, 1))])

    async def scenario():
        async with started_app(db, use_sources=True) as (app, sink, _):
            args = args_for("--target", "127.0.0.1", "--port", str(port), "--count", "1", "--hostnames", "NEXUS-TEST-99", "--quiet")
            await asyncio.to_thread(sim.run, args, io.StringIO())
            await wait_until(lambda: app.runtime.counters.recent()[RECEIVED] == 4)
            assert app.runtime.counters.recent()[dropped("unknown_source")] == 4
            assert app.runtime.unknown_sources.entries()[0].node_id == "NEXUS-TEST-99"

    asyncio.run(scenario())
