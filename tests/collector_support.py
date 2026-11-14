"""collector の自動テストで使う部品(模擬の送信先、時計、模擬のZabbixサーバーなど)。"""

import asyncio
import json
import logging
import socket
import struct
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from collector.app import CollectorApp
from collector.cisco_nexus.handler import CiscoNexusHandler
from collector.models import ZabbixValue
from collector.zabbix_sink import SendResult, ZabbixRejected, ZabbixUnreachable
from common.config_writer import save_device, seed_initial_data, update_settings
from common.database import connect, initialize_schema
from common.validation import DeviceForm

ALL_COMMANDS = {"cmd_counters_brief": True, "cmd_ptp_brief": True, "cmd_ptp_clock": True, "cmd_ptp_parent": True}
START_NS = 1_800_000_000 * 1_000_000_000
NS = 1_000_000_000


class FakeClock:
    """進め方を自由に決められる時計。"""

    def __init__(self, start_ns: int = START_NS) -> None:
        self.value = start_ns

    def __call__(self) -> int:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += int(seconds * NS)


class FakeSink:
    """Zabbixの代わりに、送られた値を記録する模擬の送信先。"""

    def __init__(self) -> None:
        self.mode = "ok"  # ok / unreachable / rejected / block
        self.failed_per_call = 0
        self.batches: list[list[ZabbixValue]] = []
        self.calls = 0
        self.release = asyncio.Event()

    @property
    def values(self) -> list[ZabbixValue]:
        return [value for batch in self.batches for value in batch]

    def triples(self) -> set[tuple[str, str, Any]]:
        return {(v.host, v.key, v.value) for v in self.values}

    async def send(self, values: Sequence[ZabbixValue]) -> SendResult:
        self.calls += 1
        if self.mode == "unreachable":
            raise ZabbixUnreachable("テスト用:接続できない")
        if self.mode == "rejected":
            raise ZabbixRejected("テスト用:受け付けない")
        if self.mode == "block":
            await self.release.wait()
        self.batches.append(list(values))
        failed = min(self.failed_per_call, len(values))
        return SendResult(processed=len(values) - failed, failed=failed)


def prepare_db(path: Path, devices: Sequence[DeviceForm] = (), settings: dict[str, str] | None = None) -> None:
    """テスト用のデータベースを作り、機器と設定を登録する。"""
    conn = connect(path)
    try:
        initialize_schema(conn)
        seed_initial_data(conn)
        for device in devices:
            save_device(conn, device)
        update_settings(conn, {"zabbix_server": "192.0.2.100", **(settings or {})})
    finally:
        conn.close()


def device(
    hostname: str = "NEXUS-TEST-01",
    source_ip: str = "192.0.2.1",
    counters: str = "1/3",
    ptp: str = "1/1",
    **commands: bool,
) -> DeviceForm:
    return DeviceForm(hostname, source_ip, {**ALL_COMMANDS, **commands}, {"counters": counters, "ptp": ptp})


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def send_udp(port: int, payload: bytes, count: int = 1) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _ in range(count):
            sock.sendto(payload, ("127.0.0.1", port))


async def wait_until(condition, timeout: float = 5.0) -> None:  # noqa: ANN001
    """条件が成り立つまで待つ。時間内に成り立たなければ失敗にする。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not condition():
        if loop.time() > deadline:
            raise AssertionError("時間内に条件が成り立ちませんでした")
        await asyncio.sleep(0.01)


@asynccontextmanager
async def started_app(
    db_path: Path,
    *,
    sink: FakeSink | None = None,
    clock: FakeClock | None = None,
    use_sources: bool = False,
    run_sender_task: bool = False,
    logger: logging.Logger | None = None,
) -> AsyncIterator[tuple[CollectorApp, FakeSink, FakeClock]]:
    """collector を起動し、終わったら止める。"""
    sink = sink or FakeSink()
    clock = clock or FakeClock()
    created_targets: list[tuple[str, int]] = []

    def sink_factory(server: str, port: int) -> FakeSink:
        created_targets.append((server, port))
        return sink

    kwargs: dict[str, Any] = {}
    if not use_sources:
        kwargs["sources_factory"] = lambda runtime: []
    app = CollectorApp(
        db_path,
        logger or logging.getLogger("collector-test"),
        sink_factory=sink_factory,
        now_ns=clock,
        run_sender_task=run_sender_task,
        **kwargs,
    )
    app.sink_targets = created_targets  # type: ignore[attr-defined]
    await app.start()
    try:
        yield app, sink, clock
    finally:
        await app.stop()


def handler_of(app: CollectorApp) -> CiscoNexusHandler:
    return CiscoNexusHandler(app.runtime)


# ======================================================================
# 模擬のZabbixサーバー(Zabbix sender プロトコルで応答する)
# ======================================================================


class FakeZabbixServer:
    """Zabbix sender プロトコルの要求を受け、決めた応答を返す模擬のサーバー。"""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response: dict[str, Any] | None = None
        self.port = 0
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        header = await reader.readexactly(13)
        assert header[:4] == b"ZBXD"
        length = struct.unpack("<I", header[5:9])[0]
        request = json.loads(await reader.readexactly(length))
        self.requests.append(request)
        items = request.get("data", [])
        response = self.response or {
            "response": "success",
            "info": f"processed: {len(items)}; failed: 0; total: {len(items)}; seconds spent: 0.000100",
        }
        body = json.dumps(response).encode("utf-8")
        writer.write(b"ZBXD\x01" + struct.pack("<II", len(body), 0) + body)
        await writer.drain()
        writer.close()
