"""collector の受信から送信までの流れの自動テスト(段階3)。

Zabbixへの送信部分は、模擬の送信先(FakeSink)に差し替えて確かめる。
"""

import asyncio
import json

from collector.stats import RECEIVED, SENT_FAILED, SENT_OK, SKIPPED, dropped
from tests.collector_support import NS, START_NS, FakeSink, device, handler_of, prepare_db, started_app
from tests.conftest import fixture_bytes, fixture_document, to_payload

COMMANDS = ["show interface counters brief", "show ptp brief", "show ptp clock", "show ptp parent"]
LLD_KEYS = {"show.interface.counters.brief.discovery", "show.ptp.brief.discovery"}


def monitoring_values(sink: FakeSink) -> set[tuple]:
    """LLD用データと自己監視値を除いた、監視データの値。"""
    return {t for t in sink.triples() if t[1] not in LLD_KEYS and not t[1].startswith("telemetry.")}


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


# ---------------------------------------------------------------- 4つのコマンド
def test_four_commands_are_sent_with_host_key_value(tmp_path):
    """4つのコマンドを受信すると、正しいホスト名・キー・値が送信される(TX-03〜TX-05、CV-01〜CV-05)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(counters="1/3", ptp="1/1")])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler = handler_of(app)
            received_ns = clock() + 123_456_789
            for command in COMMANDS:
                handler.handle(fixture_bytes(command), "192.0.2.1", received_ns)
            await app.sender.drain()

            assert monitoring_values(sink) == {
                ("NEXUS-TEST-01", "show.ptp.clock.clock-state", "Locked"),
                ("NEXUS-TEST-01", "show.ptp.clock.offset-from-master", -4),
                ("NEXUS-TEST-01", "show.ptp.parent.gm-id", "00:00:5e:ff:fe:00:53:ff"),
                ("NEXUS-TEST-01", "show.ptp.brief.state[1/1]", "slave"),
                ("NEXUS-TEST-01", "show.interface.counters.brief.inrate1[1/3]", 10_552_000_000),
                ("NEXUS-TEST-01", "show.interface.counters.brief.outrate1[1/3]", 2_632_000_000),
            }
            # 記録する時刻は、中間サーバーでの受信時刻(CV-12)
            for value in sink.values:
                if value.key.startswith("show.") and value.key not in LLD_KEYS:
                    assert (value.clock, value.ns) == divmod(received_ns, NS)
            counts = app.runtime.counters.recent()
            assert counts[RECEIVED] == 4
            assert counts[SENT_OK] == len(sink.values)
            assert app.sender.connected is True

    run(scenario())


def test_values_go_to_registered_hostname(tmp_path):
    """送信先のZabbixホスト名には、登録したホスト名を使う(TX-04)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(), device("NEXUS-TEST-02", "192.0.2.2")])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            document = fixture_document("show ptp clock")
            document["node_id_str"] = "NEXUS-TEST-02"
            handler_of(app).handle(to_payload(document), "192.0.2.2", clock())
            await app.sender.drain()
            assert {v.host for v in sink.values if v.key.startswith("show.ptp.clock")} == {"NEXUS-TEST-02"}

    run(scenario())


# ---------------------------------------------------------------- 破棄と対象外
def test_unknown_source(tmp_path):
    """未登録の送信元からのデータは破棄し、送信元とホスト名と件数を記録する(RX-03、RX-05)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler = handler_of(app)
            handler.handle(fixture_bytes("show ptp clock"), "192.0.2.99", clock())
            handler.handle(fixture_bytes("show ptp clock"), "192.0.2.99", clock() + NS)
            handler.handle(b"broken", "192.0.2.98", clock())
            await app.sender.drain()

            counts = app.runtime.counters.recent()
            assert counts[RECEIVED] == 3
            assert counts[dropped("unknown_source")] == 3
            assert monitoring_values(sink) == set()
            entries = {e.source_ip: e for e in app.runtime.unknown_sources.entries()}
            assert entries["192.0.2.99"].node_id == "NEXUS-TEST-01"
            assert entries["192.0.2.99"].count == 2
            assert entries["192.0.2.99"].last_received_ns == clock() + NS
            # 壊れたデータでホスト名が読み取れない場合は、空にする
            assert entries["192.0.2.98"].node_id is None

    run(scenario())


def test_hostname_mismatch(tmp_path):
    """送信元は登録済みでも、データ内のホスト名が違えば未登録の送信元として破棄する(RX-04)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            document = fixture_document("show ptp clock")
            document["node_id_str"] = "NEXUS-TEST-99"
            handler_of(app).handle(to_payload(document), "192.0.2.1", clock())
            await app.sender.drain()
            assert app.runtime.counters.recent()[dropped("unknown_source")] == 1
            assert monitoring_values(sink) == set()
            entry = app.runtime.unknown_sources.entries()[0]
            assert (entry.source_ip, entry.node_id) == ("192.0.2.1", "NEXUS-TEST-99")

    run(scenario())


def test_invalid_data_and_unknown_command(tmp_path):
    """壊れたデータと4つ以外のコマンドは、理由別に数えて破棄し、続けて正常なデータを処理できる(RX-02、RX-06、RX-09、MN-06)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler = handler_of(app)
            handler.handle(b"\x00\x01{broken", "192.0.2.1", clock())
            document = fixture_document("show ptp clock")
            document["encoding_path"] = "show ptp clock detail"
            handler.handle(to_payload(document), "192.0.2.1", clock())
            handler.handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            await app.sender.drain()

            counts = app.runtime.counters.recent()
            assert counts[dropped("invalid_data")] == 1
            assert counts[dropped("unknown_command")] == 1
            assert counts[RECEIVED] == 3
            assert ("NEXUS-TEST-01", "show.ptp.clock.clock-state", "Locked") in sink.triples()

    run(scenario())


def test_unchecked_command_is_skipped(tmp_path):
    """チェックを外したコマンドは送らず、「対象外」として件数だけ数える(RX-07)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(cmd_ptp_clock=False)])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            await app.sender.drain()
            counts = app.runtime.counters.recent()
            assert counts[SKIPPED] == 1
            assert all(counts[dropped(r)] == 0 for r in ("invalid_data", "unknown_command", "unknown_source"))
            assert monitoring_values(sink) == set()
            # 対象外のデータは、受信状態の記録にも使わない(6.6.1)
            runtime = app.runtime.tracker.runtimes()[0]
            assert runtime.last_received_ns() is None

    run(scenario())


def test_only_registered_interfaces_are_sent(tmp_path):
    """登録していないインターフェース番号の値は送らない(CV-03、CV-04)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(counters="1/3", ptp="1/3")])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler = handler_of(app)
            handler.handle(fixture_bytes("show interface counters brief"), "192.0.2.1", clock())
            handler.handle(fixture_bytes("show ptp brief"), "192.0.2.1", clock())
            await app.sender.drain()
            assert {t[1] for t in monitoring_values(sink)} == {
                "show.interface.counters.brief.inrate1[1/3]",
                "show.interface.counters.brief.outrate1[1/3]",
                "show.ptp.brief.state[1/3]",
            }

    run(scenario())


# ---------------------------------------------------------------- Zabbixの応答
def test_zabbix_unreachable_then_recovers(tmp_path):
    """接続できない間は破棄して数え、一定間隔の後に自動で送信を再開する。停止中の値は再送しない(TX-08)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            await app.sender.drain()  # 起動時のLLD用データ
            sink.mode = "unreachable"
            handler = handler_of(app)
            handler.handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            await app.sender.drain()
            assert app.sender.connected is False
            assert app.runtime.counters.recent()[dropped("zabbix_unreachable")] == 2
            calls = sink.calls

            # 再接続の間隔が過ぎるまでは、接続を試みずに破棄する
            handler.handle(fixture_bytes("show ptp parent"), "192.0.2.1", clock())
            await app.sender.drain()
            assert sink.calls == calls
            assert app.runtime.counters.recent()[dropped("zabbix_unreachable")] == 3

            # Zabbixが復旧し、間隔が過ぎると、何もしなくても送信が再開する
            sink.mode = "ok"
            clock.advance(6)
            sent_before = len(sink.values)
            handler.handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            await app.sender.drain()
            assert app.sender.connected is True
            new_values = sink.values[sent_before:]
            assert {v.key for v in new_values} - LLD_KEYS == {
                "show.ptp.clock.clock-state",
                "show.ptp.clock.offset-from-master",
            }
            # 停止中の値(show ptp parent)は再送されない
            assert "show.ptp.parent.gm-id" not in {v.key for v in sink.values}
            # 送信できるようになった時点で、LLD用データを送り直す(決定事項 D-014)
            assert {v.key for v in new_values} & LLD_KEYS == LLD_KEYS

    run(scenario())


def test_lld_resent_when_zabbix_was_down_at_start(tmp_path):
    """起動時にZabbixが止まっていた場合、送信できるようになった時点で全機器分のLLD用データを送り直す(D-014)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(), device("NEXUS-TEST-02", "192.0.2.2")])
    sink = FakeSink()
    sink.mode = "unreachable"

    async def scenario():
        async with started_app(db, sink=sink) as (app, _, clock):
            await app.sender.drain()  # 起動時のLLD用データは届かない
            assert sink.values == []
            sink.mode = "ok"
            clock.advance(6)
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            await app.sender.drain()
            lld = {(v.host, v.key) for v in sink.values if v.key in LLD_KEYS}
            assert lld == {(host, key) for host in ("NEXUS-TEST-01", "NEXUS-TEST-02") for key in LLD_KEYS}
            # 送り直しは1回だけ
            await app.sender.drain()
            assert len([v for v in sink.values if v.key in LLD_KEYS]) == 4

    run(scenario())


def test_lld_sent_when_zabbix_server_set_later(tmp_path):
    """ZabbixサーバーのIPアドレスを後から設定した場合も、全機器分のLLD用データを送る(D-014)。"""
    from common.config_writer import update_settings
    from common.database import connect

    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(), device("NEXUS-TEST-02", "192.0.2.2")], settings={"zabbix_server": ""})

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            await app.sender.drain()
            assert sink.calls == 0
            conn = connect(db)
            update_settings(conn, {"zabbix_server": "192.0.2.100"})
            conn.close()
            clock.advance(2)
            await app.tick()
            await app.sender.drain()
            lld = [(v.host, v.key) for v in sink.values if v.key in LLD_KEYS]
            assert sorted(lld) == sorted((h, k) for h in ("NEXUS-TEST-01", "NEXUS-TEST-02") for k in LLD_KEYS)
            assert app.sink_targets == [("192.0.2.100", 10051)]

    run(scenario())


def test_lld_sent_to_new_zabbix_server(tmp_path):
    """Zabbixの送信先を変えた場合は、新しい送信先へ全機器分のLLD用データを送る(D-014)。"""
    from common.config_writer import update_settings
    from common.database import connect

    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            await app.sender.drain()
            sink.batches.clear()
            conn = connect(db)
            update_settings(conn, {"zabbix_server": "192.0.2.101"})
            conn.close()
            clock.advance(2)
            await app.tick()
            await app.sender.drain()
            assert len([v for v in sink.values if v.key in LLD_KEYS]) == 2
            assert app.sink_targets[-1] == ("192.0.2.101", 10051)

    run(scenario())


def test_lld_not_duplicated_on_normal_start(tmp_path):
    """Zabbixが正常なときの起動では、LLD用データは1回だけ送る。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            await app.sender.drain()
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            await app.sender.drain()
            assert len([v for v in sink.values if v.key in LLD_KEYS]) == 2

    run(scenario())


def test_zabbix_failed_values_are_counted_not_resent(tmp_path):
    """Zabbixが「失敗」と応答した値は、失敗件数として数え、再送しない(TX-06、TX-07)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            await app.sender.drain()
            ok_before = app.runtime.counters.recent()[SENT_OK]
            sink.failed_per_call = 1
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            await app.sender.drain()
            counts = app.runtime.counters.recent()
            assert counts[SENT_FAILED] == 1
            assert counts[SENT_OK] == ok_before + 1
            calls = sink.calls
            await app.sender.drain()
            assert sink.calls == calls  # 再送しない

    run(scenario())


def test_zabbix_rejected_whole_batch(tmp_path):
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            sink.mode = "rejected"
            await app.sender.drain()  # LLD用データ2件
            assert app.runtime.counters.recent()[SENT_FAILED] == 2
            assert app.sender.connected is True

    run(scenario())


def test_zabbix_server_not_set(tmp_path):
    """ZabbixサーバーのIPアドレスが未設定の間は、受信を続け、値は接続できないとして破棄する(6.8)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()], settings={"zabbix_server": ""})

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            await app.sender.drain()
            counts = app.runtime.counters.recent()
            assert counts[RECEIVED] == 1
            assert counts[dropped("zabbix_unreachable")] == 4  # LLD用データ2件 + 値2件
            assert sink.calls == 0
            assert app.sink_targets == []
            assert app.sender.connected is False

    run(scenario())


def test_batches_are_at_most_250(tmp_path):
    """最大250件をまとめて送る(TX-03)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(counters="1/1-54")])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler = handler_of(app)
            for _ in range(5):
                handler.handle(fixture_bytes("show interface counters brief"), "192.0.2.1", clock())
            await app.sender.drain()
            assert max(len(batch) for batch in sink.batches) == 250
            assert len(sink.values) == 2 + 5 * 108

    run(scenario())


# ---------------------------------------------------------------- キューの上限
def test_queue_never_exceeds_limit(tmp_path):
    """キューが満杯の場合は、古い値から破棄して数える。キューは上限を超えない(TX-01、TX-02)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(counters="1/1-54")], settings={"queue_max_size": "1000"})

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler = handler_of(app)
            for _ in range(20):
                handler.handle(fixture_bytes("show interface counters brief"), "192.0.2.1", clock())
                assert len(app.runtime.queue) <= 1000
            total = 2 + 20 * 108
            assert len(app.runtime.queue) == 1000
            assert app.runtime.counters.recent()[dropped("queue_full")] == total - 1000
            # 残っているのは新しい値(古いものから破棄された)
            await app.sender.drain()
            assert not any(v.key.endswith(".discovery") for v in sink.values)

    run(scenario())


# ---------------------------------------------------------------- 状態データ
def test_status_tables_reset_on_start_and_written(tmp_path):
    """状態データは起動時に消去して作り直し、現在の状態を書き出す(OP-06、MN-05)。"""
    from common.database import connect

    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(counters="1/3, 1/99")])
    conn = connect(db)
    conn.execute("INSERT INTO unknown_sources VALUES ('192.0.2.200', 'OLD', 5, '2026-01-01T00:00:00+00:00')")
    conn.execute("INSERT INTO device_status (device_id, status, updated_at) VALUES (999, 'ok', 'x')")

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            assert conn.execute("SELECT COUNT(*) FROM unknown_sources").fetchone()[0] == 0
            assert conn.execute("SELECT device_id FROM device_status").fetchall()[0][0] != 999

            handler = handler_of(app)
            handler.handle(fixture_bytes("show interface counters brief"), "192.0.2.1", clock())
            handler.handle(fixture_bytes("show ptp clock"), "192.0.2.77", clock())
            await app.sender.drain()
            await app.write_status_now()

            system = conn.execute("SELECT * FROM system_status").fetchone()
            assert system["id"] == 1
            assert system["applied_config_version"] == app.runtime.config.version
            assert system["zabbix_connected"] == 1
            assert system["received_per_min"] == 2
            assert json.loads(system["dropped_per_min"])["unknown_source"] == 1
            assert system["collector_heartbeat_at"].startswith("2027-01-15")
            assert system["config_error"] is None

            row = conn.execute("SELECT * FROM device_status").fetchone()
            assert row["status"] == "ok"
            assert row["last_counters_brief_at"] is not None
            assert row["last_ptp_clock_at"] is None
            # 登録したがデータにない番号は、不整合として記録する(CV-11)
            assert json.loads(row["mismatch_interfaces"]) == [{"command": "show interface counters brief", "ifnum": "1/99"}]

            unknown = conn.execute("SELECT * FROM unknown_sources").fetchone()
            assert (unknown["source_ip"], unknown["node_id"], unknown["count"]) == ("192.0.2.77", "NEXUS-TEST-01", 1)

    run(scenario())
    conn.close()


def test_status_written_every_5_seconds_by_tick(tmp_path):
    from common.database import connect

    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            conn = connect(db)
            first = conn.execute("SELECT collector_heartbeat_at FROM system_status").fetchone()[0]
            clock.advance(4)
            await app.tick()
            assert conn.execute("SELECT collector_heartbeat_at FROM system_status").fetchone()[0] == first
            clock.advance(1)
            await app.tick()
            assert conn.execute("SELECT collector_heartbeat_at FROM system_status").fetchone()[0] != first
            conn.close()

    run(scenario())


def test_unknown_sources_limited_to_100():
    from collector.unknown_sources import UnknownSourceTracker

    tracker = UnknownSourceTracker()
    for number in range(150):
        tracker.record(f"198.51.100.{number % 250}" if number < 250 else "x", "N", START_NS + number)
    entries = tracker.entries()
    assert len(entries) == 100
    # 古いもの(最初の50件)から削除される
    assert "198.51.100.0" not in {e.source_ip for e in entries}
    assert "198.51.100.149" in {e.source_ip for e in entries}


def test_long_node_id_is_truncated(tmp_path):
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            document = fixture_document("show ptp clock")
            document["node_id_str"] = "<script>" + "A" * 1000
            handler_of(app).handle(to_payload(document), "192.0.2.50", clock())
            entry = app.runtime.unknown_sources.entries()[0]
            assert len(entry.node_id) == 255

    run(scenario())
