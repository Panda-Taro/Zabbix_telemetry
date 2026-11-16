"""collector の途絶検知、自己監視、LLD、設定の反映、UDP受信、ログの自動テスト(段階3)。"""

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest

from collector.stats import RECEIVED, dropped
from collector.zabbix_sink import ZabbixRejected, ZabbixUnreachable, ZabbixUtilsSink
from collector.models import ZabbixValue
from common.config_writer import delete_device, save_device, update_settings
from common.database import connect, transaction
from common.logging_setup import JsonLineFormatter, LogAggregator, setup_logging
from tests.collector_support import (
    NS,
    FakeClock,
    FakeSink,
    FakeZabbixServer,
    device,
    free_udp_port,
    handler_of,
    prepare_db,
    send_udp,
    started_app,
    wait_until,
)
from tests.conftest import fixture_bytes, fixture_document, to_payload

REPO = Path(__file__).resolve().parent.parent
LLD_KEYS = {"show.interface.counters.brief.discovery", "show.ptp.brief.discovery"}


def run(coro):  # noqa: ANN001, ANN201
    return asyncio.run(coro)


def values_for(sink: FakeSink, host: str, prefix: str = "telemetry.") -> dict:
    """最後に送られた、指定したホストの自己監視値。"""
    result = {}
    for value in sink.values:
        if value.host == host and value.key.startswith(prefix):
            result[value.key] = value.value
    return result


async def send_monitor(app, sink: FakeSink) -> None:  # noqa: ANN001
    sink.batches.clear()
    from collector.monitor import build_monitor_values

    app.runtime.enqueue(build_monitor_values(app.runtime))
    await app.sender.drain()


# ---------------------------------------------------------------- 途絶検知
def test_waiting_then_stale_then_ok(tmp_path):
    """受信待ち → 途絶 → 正常 の判定と、Zabbixへ送る値(MN-02、MN-03、6.6.2.1、6.6.2.2)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(cmd_ptp_parent=False)])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            # 受信待ちの間は、telemetry.status、telemetry.age、コマンドごとの状態を送らない
            await send_monitor(app, sink)
            assert values_for(sink, "NEXUS-TEST-01") == {}
            assert app.runtime.tracker.runtimes()[0].status(clock(), 300) == "waiting"

            # 受信待ちのまま判定時間(300秒)が過ぎると途絶。経過秒数は起動からの秒数
            clock.advance(301)
            await send_monitor(app, sink)
            assert values_for(sink, "NEXUS-TEST-01") == {
                "telemetry.status": 0,
                "telemetry.age": 301,
                "telemetry.status[show interface counters brief]": 0,
                "telemetry.status[show ptp brief]": 0,
                "telemetry.status[show ptp clock]": 0,
                # チェックを外した show ptp parent は送らない(6.6.2.3)
            }

            # 1つのコマンドを受信すると、機器全体は正常、そのコマンドも正常、他は途絶のまま
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            clock.advance(2)
            await send_monitor(app, sink)
            assert values_for(sink, "NEXUS-TEST-01") == {
                "telemetry.status": 1,
                "telemetry.age": 2,
                "telemetry.status[show interface counters brief]": 0,
                "telemetry.status[show ptp brief]": 0,
                "telemetry.status[show ptp clock]": 1,
            }

            # そのコマンドも止まって判定時間が過ぎると、再び途絶
            clock.advance(300)
            await send_monitor(app, sink)
            monitor = values_for(sink, "NEXUS-TEST-01")
            assert monitor["telemetry.status"] == 0
            assert monitor["telemetry.age"] == 302
            assert monitor["telemetry.status[show ptp clock]"] == 0

    run(scenario())


def test_stale_timeout_setting_is_used(tmp_path):
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()], settings={"stale_timeout_sec": "30"})

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            clock.advance(29)
            assert app.runtime.tracker.runtimes()[0].status(clock(), 30) == "ok"
            clock.advance(1)
            await send_monitor(app, sink)
            assert values_for(sink, "NEXUS-TEST-01")["telemetry.status"] == 0

    run(scenario())


def test_collector_monitor_values(tmp_path):
    """「telemetry」ホストへ、前回からの件数などの自己監視値を送る(MN-04、9.2.5.2)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(), device("NEXUS-TEST-02", "192.0.2.2", cmd_ptp_clock=False)])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler = handler_of(app)
            handler.handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())  # 正常
            handler.handle(fixture_bytes("show ptp clock"), "192.0.2.99", clock())  # 未登録
            handler.handle(b"x{", "192.0.2.1", clock())  # 壊れたデータ
            document = fixture_document("show ptp clock")
            document["node_id_str"] = "NEXUS-TEST-02"
            handler.handle(to_payload(document), "192.0.2.2", clock())  # 対象外
            await app.sender.drain()
            clock.advance(400)  # NEXUS-TEST-02 は受信待ちのまま途絶、NEXUS-TEST-01 も途絶
            await send_monitor(app, sink)
            monitor = values_for(sink, "telemetry")
            assert monitor == {
                "telemetry.collector.received": 4,
                "telemetry.collector.dropped[unknown_source]": 1,
                "telemetry.collector.dropped[invalid_data]": 1,
                "telemetry.collector.dropped[unknown_command]": 0,
                "telemetry.collector.dropped[queue_full]": 0,
                "telemetry.collector.dropped[zabbix_unreachable]": 0,
                "telemetry.collector.skipped": 1,
                "telemetry.collector.sent.ok": 2 * 2 + 2,  # LLD用データ(2台×2件)+ 値2件
                "telemetry.collector.sent.failed": 0,
                "telemetry.collector.queue.size": len(app.runtime.queue),
                "telemetry.collector.devices.total": 2,
                "telemetry.collector.devices.stale": 2,
            }
            # 件数は、送るたびに0に戻る
            await send_monitor(app, sink)
            assert values_for(sink, "telemetry")["telemetry.collector.received"] == 0

    run(scenario())


def test_monitor_sent_by_tick_at_interval(tmp_path):
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()], settings={"self_monitor_interval_sec": "10"})

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            await app.sender.drain()
            sink.batches.clear()
            clock.advance(9)
            await app.tick()
            await app.sender.drain()
            assert values_for(sink, "telemetry") == {}
            clock.advance(1)
            await app.tick()
            await app.sender.drain()
            assert "telemetry.collector.received" in values_for(sink, "telemetry")

    run(scenario())


def test_stale_transitions_are_logged(tmp_path, caplog):
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="collector-test"):
                handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
                await app.write_status_now()
                clock.advance(301)
                await app.write_status_now()
                handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
                await app.write_status_now()
            messages = [r.getMessage() for r in caplog.records]
            assert "機器からの受信を開始しました" in messages
            assert any("途絶しました" in m for m in messages)
            assert "機器からの受信が復旧しました" in messages
            assert any(getattr(r, "hostname", "") == "NEXUS-TEST-01" for r in caplog.records)

    run(scenario())


# ---------------------------------------------------------------- LLD
def test_lld_sent_on_start_and_periodically(tmp_path):
    """LLD用データは、起動時と定期的に送る(LD-01〜LD-04)。チェックを外したコマンドは空の一覧。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(counters="1/1, 1/3", ptp="1/1"), device("NEXUS-TEST-02", "192.0.2.2", cmd_ptp_brief=False)])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            await app.sender.drain()
            lld = {(v.host, v.key): json.loads(v.value) for v in sink.values if v.key in LLD_KEYS}
            assert lld[("NEXUS-TEST-01", "show.interface.counters.brief.discovery")] == [
                {"{#IFNUM}": "1/1", "{#IFNAME}": "Ethernet1/1"},
                {"{#IFNUM}": "1/3", "{#IFNAME}": "Ethernet1/3"},
            ]
            assert lld[("NEXUS-TEST-01", "show.ptp.brief.discovery")] == [{"{#IFNUM}": "1/1", "{#IFNAME}": "Ethernet1/1"}]
            assert lld[("NEXUS-TEST-02", "show.ptp.brief.discovery")] == []

            sink.batches.clear()
            clock.advance(3600)
            await app.tick()
            await app.sender.drain()
            assert {(v.host, v.key) for v in sink.values if v.key in LLD_KEYS} == set(lld)

    run(scenario())


# ---------------------------------------------------------------- 設定の反映
def test_config_change_applied_without_restart(tmp_path):
    """設定を変えると、再起動なしで反映される。登録内容が変わった機器のLLD用データを送り直す(CF-02、CF-03、LD-04)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(counters="1/3")])
    conn = connect(db)

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            await app.sender.drain()
            sink.batches.clear()
            device_id = app.runtime.config.devices[0].id

            # インターフェース番号を追加し、機器を1台追加する
            save_device(conn, device(counters="1/3, 1/4"), device_id=device_id)
            save_device(conn, device("NEXUS-TEST-02", "192.0.2.2"))
            version = app.runtime.config.version

            clock.advance(2)  # 設定の確認は2秒ごと(保存から10秒以内に反映)
            await app.tick()
            assert app.runtime.config.version == version + 2

            await app.sender.drain()
            lld = {(v.host, v.key): json.loads(v.value) for v in sink.values if v.key in LLD_KEYS}
            assert {"{#IFNUM}": "1/4", "{#IFNAME}": "Ethernet1/4"} in lld[
                ("NEXUS-TEST-01", "show.interface.counters.brief.discovery")
            ]
            assert ("NEXUS-TEST-02", "show.ptp.brief.discovery") in lld

            sink.batches.clear()
            handler = handler_of(app)
            handler.handle(fixture_bytes("show interface counters brief"), "192.0.2.1", clock())
            document = fixture_document("show ptp clock")
            document["node_id_str"] = "NEXUS-TEST-02"
            handler.handle(to_payload(document), "192.0.2.2", clock())
            await app.sender.drain()
            keys = {(v.host, v.key) for v in sink.values}
            assert ("NEXUS-TEST-01", "show.interface.counters.brief.inrate1[1/4]") in keys
            assert ("NEXUS-TEST-02", "show.ptp.clock.clock-state") in keys

            # 機器を削除すると、その送信元は未登録として扱う
            delete_device(conn, device_id)
            clock.advance(2)
            await app.tick()
            handler.handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            assert app.runtime.counters.recent()[dropped("unknown_source")] == 1

    run(scenario())
    conn.close()


def test_registering_device_clears_unknown_source(tmp_path):
    """機器を登録すると、その送信元は未登録の送信元の一覧から消える(決定事項 D-035)。

    登録と関係のない設定の変更では消えない。ホスト名を直した場合も消え、
    一致しないデータが届けば、また一覧に表示される。
    """
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])
    conn = connect(db)

    async def reload(app, clock) -> None:  # noqa: ANN001
        clock.advance(2)
        await app.tick()

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            handler = handler_of(app)
            listed = lambda: {e.source_ip for e in app.runtime.unknown_sources.entries()}  # noqa: E731
            document = fixture_document("show ptp clock")
            document["node_id_str"] = "NEXUS-TEST-02"
            handler.handle(to_payload(document), "192.0.2.2", clock())  # 未登録の送信元
            handler.handle(to_payload(document), "192.0.2.3", clock())  # 未登録のまま残す送信元
            mismatch = fixture_document("show ptp clock")
            mismatch["node_id_str"] = "NEXUS-TEST-99"
            handler.handle(to_payload(mismatch), "192.0.2.1", clock())  # ホスト名の不一致
            assert listed() == {"192.0.2.1", "192.0.2.2", "192.0.2.3"}

            # 登録と関係のない設定の変更では、一覧は変わらない
            update_settings(conn, {"log_level": "DEBUG"})
            await reload(app, clock)
            assert listed() == {"192.0.2.1", "192.0.2.2", "192.0.2.3"}

            # 機器を登録すると、その送信元だけが一覧から消える(状態データにも反映される)
            save_device(conn, device("NEXUS-TEST-02", "192.0.2.2"))
            await reload(app, clock)
            assert listed() == {"192.0.2.1", "192.0.2.3"}
            await app.write_status_now()
            rows = {row[0] for row in conn.execute("SELECT source_ip FROM unknown_sources")}
            assert rows == {"192.0.2.1", "192.0.2.3"}

            # 登録したあとは、その送信元のデータを受け付け、一覧には戻らない
            discarded = app.runtime.counters.recent()[dropped("unknown_source")]
            handler.handle(to_payload(document), "192.0.2.2", clock())
            assert "192.0.2.2" not in listed()
            assert app.runtime.counters.recent()[dropped("unknown_source")] == discarded

            # ホスト名の登録を直すと、その送信元も一覧から消える
            device_id = next(d.id for d in app.runtime.config.devices if d.source_ip == "192.0.2.1")
            save_device(conn, device("NEXUS-TEST-98", "192.0.2.1"), device_id=device_id)
            await reload(app, clock)
            assert listed() == {"192.0.2.3"}
            # まだ一致しない場合は、次に届いたデータで、また一覧に表示される
            handler.handle(to_payload(mismatch), "192.0.2.1", clock())
            entry = {e.source_ip: e for e in app.runtime.unknown_sources.entries()}["192.0.2.1"]
            assert (entry.node_id, entry.count) == ("NEXUS-TEST-99", 1)

    run(scenario())
    conn.close()


def test_settings_change_applied(tmp_path):
    """全体設定の変更(Zabbixの送信先、キューの上限、ログレベル)が反映される。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])
    conn = connect(db)
    logger = logging.getLogger("collector-test-level")

    async def scenario():
        async with started_app(db, logger=logger) as (app, sink, clock):
            await app.sender.drain()
            update_settings(conn, {"zabbix_server": "192.0.2.101", "zabbix_port": "10052", "queue_max_size": "2000", "log_level": "ERROR"})
            clock.advance(2)
            await app.tick()
            assert app.runtime.queue.max_size == 2000
            assert logger.level == logging.ERROR
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            await app.sender.drain()
            assert app.sink_targets[-1] == ("192.0.2.101", 10052)

    run(scenario())
    conn.close()


def test_hostname_change_resets_to_waiting_but_interface_change_does_not(tmp_path):
    """ホスト名・送信元を変えたら受信待ちに戻す。インターフェース番号の変更では状態を保つ(6.7.4)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])
    conn = connect(db)

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            device_id = app.runtime.config.devices[0].id
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            save_device(conn, device(counters="1/3, 1/5"), device_id=device_id)
            clock.advance(2)
            await app.tick()
            assert app.runtime.tracker.get(device_id).status(clock(), 300) == "ok"

            save_device(conn, device(hostname="NEXUS-TEST-05", counters="1/3, 1/5"), device_id=device_id)
            clock.advance(2)
            await app.tick()
            assert app.runtime.tracker.get(device_id).status(clock(), 300) == "waiting"

    run(scenario())
    conn.close()


def test_mismatch_cleared_when_interface_removed(tmp_path):
    """監視対象から外した番号は、不整合の表示からも消える。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(counters="1/3, 1/99")])
    conn = connect(db)

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            device_id = app.runtime.config.devices[0].id
            handler_of(app).handle(fixture_bytes("show interface counters brief"), "192.0.2.1", clock())
            assert app.runtime.tracker.get(device_id).mismatch_list() == [
                {"command": "show interface counters brief", "ifnum": "1/99"}
            ]
            save_device(conn, device(counters="1/3"), device_id=device_id)
            clock.advance(2)
            await app.tick()
            assert app.runtime.tracker.get(device_id).mismatch_list() == []

    run(scenario())
    conn.close()


def test_config_load_failure_keeps_previous(tmp_path, caplog):
    """新しい設定を読み込めない場合は、直前の設定のまま動作を続け、エラーをログとWebGUIに表示する(CF-05)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])
    conn = connect(db)

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            version = app.runtime.config.version
            # 不正な値を直接書き込み、版番号を上げる(画面からは保存できない値)
            with transaction(conn):
                conn.execute("UPDATE settings SET value = 'abc' WHERE key = 'stale_timeout_sec'")
                conn.execute("UPDATE settings SET value = CAST(value + 1 AS TEXT) WHERE key = 'config_version'")
            with caplog.at_level(logging.ERROR, logger="collector-test"):
                clock.advance(2)
                await app.tick()
                clock.advance(2)
                await app.tick()
            assert app.runtime.config.version == version
            assert app.runtime.config.stale_timeout_sec == 300
            assert "途絶判定時間" in app.config_error
            assert sum("直前の設定のまま" in r.getMessage() for r in caplog.records) == 1  # 同じエラーは繰り返し出さない
            await app.write_status_now()
            assert "途絶判定時間" in conn.execute("SELECT config_error FROM system_status").fetchone()[0]
            # 受信は続く
            handler_of(app).handle(fixture_bytes("show ptp clock"), "192.0.2.1", clock())
            assert app.runtime.tracker.runtimes()[0].last_received_ns() is not None

            # 正しい値に直すと、エラーが消えて新しい設定になる
            update_settings(conn, {"stale_timeout_sec": "600"})
            clock.advance(2)
            await app.tick()
            assert app.runtime.config.stale_timeout_sec == 600
            assert app.config_error is None

    run(scenario())
    conn.close()


def test_start_before_web_initialized(tmp_path):
    """web が初期データを登録する前に起動しても、既定値で動き始め、登録後に読み込む。"""
    from common.config_writer import seed_initial_data
    from common.database import initialize_schema

    db = tmp_path / "telemetry.db"
    conn = connect(db)
    initialize_schema(conn)

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            assert app.runtime.config.version == 0
            assert app.runtime.config.telemetry_udp_port == 50051
            seed_initial_data(conn)
            save_device(conn, device())
            clock.advance(2)
            await app.tick()
            assert len(app.runtime.config.devices) == 1

    run(scenario())
    conn.close()


# ---------------------------------------------------------------- 実際のUDP受信
def _insert_loopback_device(db: Path) -> None:
    """テスト用に、送信元 127.0.0.1 の機器を直接登録する(画面からは登録できないアドレス)。"""
    conn = connect(db)
    with transaction(conn):
        conn.execute(
            "INSERT INTO devices (hostname, source_ip, device_type, cmd_counters_brief, cmd_ptp_brief,"
            " cmd_ptp_clock, cmd_ptp_parent, created_at, updated_at)"
            " VALUES ('NEXUS-TEST-01', '127.0.0.1', 'cisco_nexus', 1, 0, 1, 1, 'x', 'x')"
        )
        device_id = conn.execute("SELECT id FROM devices").fetchone()[0]
        conn.execute("INSERT INTO device_interfaces (device_id, purpose, slot, port) VALUES (?, 'counters', 1, 3)", (device_id,))
    conn.close()


def test_udp_receiving_continues_while_zabbix_is_slow(tmp_path):
    """Zabbixの応答が遅い間も受信が続き、キューは上限を超えない(RX-01、RX-08、TX-01、TX-02)。"""
    db = tmp_path / "telemetry.db"
    port = free_udp_port()
    prepare_db(db, settings={"telemetry_udp_port": str(port), "queue_max_size": "1000"})
    _insert_loopback_device(db)
    sink = FakeSink()
    sink.mode = "block"  # Zabbixが応答しない

    async def scenario():
        async with started_app(db, sink=sink, clock=FakeClock(), use_sources=True, run_sender_task=True) as (app, _, clock):
            await wait_until(lambda: sink.calls == 1)  # 起動時のLLD用データの送信で止まっている
            payload = fixture_bytes("show interface counters brief")
            await asyncio.to_thread(send_udp, port, payload, 30)
            await wait_until(lambda: app.runtime.counters.recent()[RECEIVED] == 30)
            assert len(app.runtime.queue) == 30 * 2  # 1パケットにつき2件(1/3 の inrate1、outrate1)
            await asyncio.to_thread(send_udp, port, payload, 600)
            await wait_until(lambda: app.runtime.counters.recent()[RECEIVED] == 630)
            assert len(app.runtime.queue) == 1000
            assert app.runtime.counters.recent()[dropped("queue_full")] == 630 * 2 - 1000
            sink.release.set()  # Zabbixが応答を返すと、送信が進む
            await wait_until(lambda: len(app.runtime.queue) == 0)

    run(scenario())


def test_udp_port_change_reopens_receiver(tmp_path):
    """受信ポート番号を変えると、再起動せずに新しいポート番号で受信する(CF-04)。"""
    db = tmp_path / "telemetry.db"
    first, second = free_udp_port(), free_udp_port()
    while second == first:
        second = free_udp_port()
    prepare_db(db, settings={"telemetry_udp_port": str(first)})
    _insert_loopback_device(db)
    conn = connect(db)

    async def scenario():
        async with started_app(db, use_sources=True) as (app, sink, clock):
            payload = fixture_bytes("show ptp clock")
            await asyncio.to_thread(send_udp, first, payload)
            await wait_until(lambda: app.runtime.counters.recent()[RECEIVED] == 1)

            update_settings(conn, {"telemetry_udp_port": str(second)})
            clock.advance(2)
            await app.tick()
            assert app.sources[0].port == second
            await asyncio.to_thread(send_udp, second, payload)
            await wait_until(lambda: app.runtime.counters.recent()[RECEIVED] == 2)
            await asyncio.to_thread(send_udp, first, payload)  # 古いポート番号では受信しない
            await asyncio.sleep(0.2)
            assert app.runtime.counters.recent()[RECEIVED] == 2

    run(scenario())
    conn.close()


def test_udp_port_in_use_keeps_previous(tmp_path):
    """新しいポート番号で受信を開始できない場合は、今のポート番号のまま受信を続け、エラーを表示する。"""
    import socket

    db = tmp_path / "telemetry.db"
    first = free_udp_port()
    prepare_db(db, settings={"telemetry_udp_port": str(first)})
    _insert_loopback_device(db)
    conn = connect(db)
    blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    blocker.bind(("0.0.0.0", 0))
    busy_port = blocker.getsockname()[1]

    async def scenario():
        async with started_app(db, use_sources=True) as (app, sink, clock):
            update_settings(conn, {"telemetry_udp_port": str(busy_port)})
            clock.advance(2)
            await app.tick()
            assert app.sources[0].port == first
            assert str(busy_port) in app.config_error
            await asyncio.to_thread(send_udp, first, fixture_bytes("show ptp clock"))
            await wait_until(lambda: app.runtime.counters.recent()[RECEIVED] == 1)

    try:
        run(scenario())
    finally:
        blocker.close()
        conn.close()


# ---------------------------------------------------------------- zabbix_utils による実際の送信
def test_zabbix_utils_sink_with_fake_server():
    """zabbix_utils で、Zabbix sender プロトコルの要求を送り、応答を読み取る(9.2.1)。"""

    async def scenario():
        server = FakeZabbixServer()
        await server.start()
        try:
            sink = ZabbixUtilsSink("127.0.0.1", server.port)
            values = [
                ZabbixValue("NEXUS-TEST-01", "show.ptp.clock.offset-from-master", -4, 1_800_000_000, 123),
                ZabbixValue("NEXUS-TEST-01", "show.ptp.clock.clock-state", "Locked", 1_800_000_000, 123),
            ]
            result = await sink.send(values)
            assert (result.processed, result.failed) == (2, 0)
            data = server.requests[0]["data"]
            assert server.requests[0]["request"] == "sender data"
            assert data[0] == {
                "host": "NEXUS-TEST-01",
                "key": "show.ptp.clock.offset-from-master",
                "value": "-4",
                "clock": 1_800_000_000,
                "ns": 123,
            }

            server.response = {"response": "success", "info": "processed: 1; failed: 1; total: 2; seconds spent: 0.0001"}
            result = await sink.send(values)
            assert (result.processed, result.failed) == (1, 1)

            server.response = {"response": "failed", "info": "processed: 0; failed: 2; total: 2; seconds spent: 0.0001"}
            with pytest.raises(ZabbixRejected):
                await sink.send(values)
        finally:
            await server.stop()

        # 誰も待ち受けていないポートへは、接続できない
        with pytest.raises(ZabbixUnreachable):
            await ZabbixUtilsSink("127.0.0.1", server.port).send(values)

    run(scenario())


# ---------------------------------------------------------------- 動作ログ
def test_log_lines_are_json(tmp_path):
    """動作ログは、1行1件のJSON形式で書き出す(LG-01)。"""
    logger = setup_logging("collector-logtest", tmp_path)
    logger.warning("テストのメッセージ", extra={"hostname": "NEXUS-TEST-01"})
    logger.info("ホスト名なし")
    for handler in logger.handlers:
        handler.flush()
    lines = (tmp_path / "collector-logtest.log").read_text(encoding="utf-8").splitlines()
    first, second = (json.loads(line) for line in lines)
    assert set(first) == {"timestamp", "level", "component", "hostname", "message"}
    assert first["level"] == "WARNING"
    assert first["component"] == "collector-logtest"
    assert first["hostname"] == "NEXUS-TEST-01"
    assert first["message"] == "テストのメッセージ"
    assert re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00", first["timestamp"])
    assert second["hostname"] == ""
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)


def test_log_rotation_settings(tmp_path):
    """ログファイルは10MBごとに切り替え、10世代まで残す(LG-02)。"""
    from logging.handlers import RotatingFileHandler

    logger = setup_logging("collector-rotation", tmp_path)
    handler = next(h for h in logger.handlers if isinstance(h, RotatingFileHandler))
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 10
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)


def test_log_rotation_keeps_10_generations(tmp_path, monkeypatch):
    """実際に切り替えたとき、古いファイルが10世代を超えない(上限を小さく置き換えて確かめる)。"""
    import common.logging_setup as logging_setup

    monkeypatch.setattr(logging_setup, "LOG_MAX_BYTES", 500)
    logger = setup_logging("collector-gen", tmp_path)
    for number in range(400):
        logger.info("ローテーションのテスト %d %s", number, "x" * 50)
    files = sorted(p.name for p in tmp_path.glob("collector-gen.log*"))
    assert "collector-gen.log" in files
    assert len(files) == 11  # 今のファイル + 10世代
    for h in list(logger.handlers):
        h.close()
        logger.removeHandler(h)


def test_log_level_change():
    """ログレベルを変えられる(LG-04)。"""
    from common.logging_setup import set_log_level

    logger = logging.getLogger("collector-level")
    set_log_level(logger, "WARNING")
    assert logger.level == logging.WARNING
    set_log_level(logger, "debug")
    assert logger.level == logging.DEBUG


def test_repeated_logs_are_aggregated(tmp_path, caplog):
    """同じ内容のログが大量に出る場合は、1分ごとに件数をまとめて出力する(LG-03)。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device()])

    async def scenario():
        async with started_app(db) as (app, sink, clock):
            caplog.clear()
            with caplog.at_level(logging.INFO, logger="collector-test"):
                handler = handler_of(app)
                for _ in range(500):
                    handler.handle(fixture_bytes("show ptp clock"), "192.0.2.99", clock())
                clock.advance(30)
                await app.tick()
                unknown_logs = [r for r in caplog.records if "未登録の送信元" in r.getMessage()]
                assert unknown_logs == []  # 1件ごとには出力しない
                clock.advance(30)
                await app.tick()
                unknown_logs = [r for r in caplog.records if "未登録の送信元" in r.getMessage()]
                assert len(unknown_logs) == 1
                assert "500件" in unknown_logs[0].getMessage()

    run(scenario())


def test_aggregator_groups_by_content():
    clock = FakeClock()
    records: list[str] = []

    class ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(JsonLineFormatter("collector").format(record))

    logger = logging.getLogger("collector-aggregator")
    logger.addHandler(ListHandler())
    logger.setLevel(logging.INFO)
    aggregator = LogAggregator(logger, interval_sec=60, now_ns=clock)
    for _ in range(3):
        aggregator.add(logging.WARNING, "同じ内容", hostname="NEXUS-TEST-01")
    aggregator.add(logging.WARNING, "別の内容")
    aggregator.flush_if_due()
    assert records == []
    clock.advance(60)
    aggregator.flush_if_due()
    entries = [json.loads(r) for r in records]
    assert len(entries) == 2
    assert entries[0]["hostname"] == "NEXUS-TEST-01"
    assert "3件" in entries[0]["message"]


# ---------------------------------------------------------------- 構造の確認
def test_cf_06_collector_writes_only_status_tables():
    """collector がSQLで書き込むのは、状態データのテーブルだけ(CF-06)。"""
    from common.database import CONFIG_TABLES

    pattern = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.IGNORECASE)
    for path in (REPO / "collector").rglob("*.py"):
        for _, table in pattern.findall(path.read_text(encoding="utf-8")):
            assert table not in CONFIG_TABLES, f"{path.name} が設定データのテーブル {table} に書き込んでいます"


def test_rx_10_sources_are_independent():
    """受信部はメーカー・方式ごとに独立した部品で、共通部分はNexus用の部品を読み込まない(RX-10)。"""
    import ast

    generic = ["app.py", "config.py", "device_state.py", "runtime.py", "sender.py", "send_queue.py",
               "stats.py", "status_writer.py", "unknown_sources.py", "zabbix_sink.py", "transport/udp.py"]  # fmt: skip
    for name in generic:
        tree = ast.parse((REPO / "collector" / name).read_text(encoding="utf-8"))
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        nexus = {m for m in modules if m.startswith("collector.cisco_nexus")}
        allowed = {"collector.cisco_nexus.lld", "collector.cisco_nexus.commands"} if name in ("app.py", "config.py") else set()
        assert nexus <= allowed, f"{name} がNexus用の部品 {nexus - allowed} を読み込んでいます"
