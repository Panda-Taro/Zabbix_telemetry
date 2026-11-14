"""collector の全体の動き(6.4〜6.8)。

起動すると、次のものを並行して動かす。
  - 受信部(メーカー・方式ごと。受信したら値を送信待ちキューに入れる)
  - Zabbixへの送信(送信待ちキューから取り出して送る)
  - 定期処理(1秒ごとに見回る)
      ・設定の版番号の確認と、読み込み直し(2秒ごと、CF-02)
      ・状態データの書き出し(5秒ごと、MN-05)と、途絶の発生・復旧のログ
      ・自己監視値の送信(既定値60秒ごと、MN-03、MN-04)
      ・LLD用データの定期送信(既定値1時間ごと、LD-04)
      ・まとめて出すログの出力(1分ごと、LG-03)
"""

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from collector.cisco_nexus.lld import build_lld_values
from collector.config import CollectorConfig, ConfigLoadError, DeviceConfig, build_config, load_config, read_config_version
from collector.monitor import build_monitor_values, build_status_snapshot
from collector.runtime import Runtime
from collector.sender import ZabbixSender
from collector.sources import TelemetrySource, create_sources
from collector.stats import dropped
from collector.status_writer import reset_status, write_status
from collector.zabbix_sink import ZabbixSink, ZabbixUtilsSink
from common.constants import (
    CONFIG_POLL_INTERVAL_SEC,
    DEVICE_TYPE_CISCO_NEXUS,
    DROP_QUEUE_FULL,
    HOUSEKEEPING_TICK_SEC,
    STATUS_OK,
    STATUS_STALE,
    STATUS_WAITING,
    STATUS_WRITE_INTERVAL_SEC,
)
from common.database import connect, initialize_schema
from common.logging_setup import HOSTNAME_FIELD, set_log_level

_NS_PER_SEC = 1_000_000_000

SinkFactory = Callable[[str, int], ZabbixSink]
SourcesFactory = Callable[[Runtime], list[TelemetrySource]]


class CollectorApp:
    """collector 本体。"""

    def __init__(
        self,
        db_path: str | Path,
        logger: logging.Logger,
        *,
        sink_factory: SinkFactory = ZabbixUtilsSink,
        sources_factory: SourcesFactory = create_sources,
        now_ns: Callable[[], int] = time.time_ns,
        run_sender_task: bool = True,
    ) -> None:
        self.db_path = db_path
        self.logger = logger
        self._sink_factory = sink_factory
        self._sources_factory = sources_factory
        self._now_ns = now_ns
        self._run_sender_task = run_sender_task

        self._conn: sqlite3.Connection | None = None
        self._db_lock = asyncio.Lock()
        self.runtime: Runtime
        self.sender: ZabbixSender
        self.sources: list[TelemetrySource] = []
        self._sender_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        self._sink: ZabbixSink | None = None
        self._sink_target: tuple[str, int] | None = None
        self._load_error: str | None = None
        self._failed_version: int | None = None
        self._source_error: str | None = None
        self.started_ns = 0

        self._next_config_poll_ns = 0
        self._next_status_ns = 0
        self._next_monitor_ns = 0
        self._next_lld_ns = 0

    # ------------------------------------------------------------------
    # データベース(受信を止めないよう、別のスレッドで行う)
    # ------------------------------------------------------------------
    async def _db(self, func: Callable[..., Any], *args: Any) -> Any:
        async with self._db_lock:
            return await asyncio.to_thread(func, self._conn, *args)

    @property
    def config_error(self) -> str | None:
        """WebGUIに表示する、設定の読み込みや反映のエラー(6.7.3、CF-05)。"""
        errors = [e for e in (self._load_error, self._source_error) if e]
        return " / ".join(errors) if errors else None

    # ------------------------------------------------------------------
    # 起動と停止
    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._conn = connect(self.db_path, check_same_thread=False)
        await self._db(initialize_schema)
        await self._db(reset_status)  # 状態データは起動時にすべて消去して作り直す(OP-06)

        try:
            config = await self._db(load_config)
        except ConfigLoadError as exc:
            # 読み込めない場合は既定値で動き始め、正しい設定が保存されるのを待つ
            self._load_error = str(exc)
            self.logger.error("設定を読み込めないため、既定値で起動します:%s", exc)
            config = build_config({}, ())
            try:
                self._failed_version = await self._db(read_config_version)
            except ConfigLoadError:
                self._failed_version = None

        now = self._now_ns()
        self.started_ns = now
        self.runtime = Runtime(config, self.logger, self._now_ns)
        set_log_level(self.logger, config.log_level)
        self.runtime.tracker.apply_config(config.devices, now)

        self.sender = ZabbixSender(self.runtime, self._current_sink, on_recovered=self._resend_lld_after_recovery)
        self.sources = self._sources_factory(self.runtime)
        await self._apply_sources(config)
        if self._run_sender_task:
            self._sender_task = asyncio.create_task(self.sender.run(), name="zabbix-sender")

        # LLD用データは、起動時に全機器分を送る(LD-04)
        self.send_lld(config.devices)

        self._next_config_poll_ns = now + CONFIG_POLL_INTERVAL_SEC * _NS_PER_SEC
        self._next_monitor_ns = now + config.self_monitor_interval_sec * _NS_PER_SEC
        self._next_lld_ns = now + config.lld_interval_sec * _NS_PER_SEC
        self._next_status_ns = now + STATUS_WRITE_INTERVAL_SEC * _NS_PER_SEC
        await self.write_status_now()
        self.logger.info(
            "collectorを起動しました(設定の版番号 %d、登録機器 %d 台)", config.version, len(config.devices)
        )

    async def stop(self) -> None:
        self._stop_event.set()
        if self._sender_task is not None:
            self._sender_task.cancel()
            try:
                await self._sender_task
            except asyncio.CancelledError:
                pass
            self._sender_task = None
        for source in self.sources:
            await source.stop()
        if self._conn is not None:
            self.runtime.aggregator.flush()
            self._conn.close()
            self._conn = None
        self.logger.info("collectorを停止しました")

    def request_stop(self) -> None:
        self._stop_event.set()

    async def run_forever(self) -> None:
        """起動し、止められるまで定期処理を続ける。"""
        await self.start()
        try:
            while not self._stop_event.is_set():
                await self.tick()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=HOUSEKEEPING_TICK_SEC)
                except TimeoutError:
                    pass
        finally:
            await self.stop()

    # ------------------------------------------------------------------
    # Zabbixの送信先
    # ------------------------------------------------------------------
    def _current_sink(self) -> ZabbixSink | None:
        """今の設定の送信先を返す。ZabbixサーバーのIPアドレスが未設定なら None(6.8)。"""
        config = self.runtime.config
        if not config.zabbix_server:
            return None
        target = (config.zabbix_server, config.zabbix_port)
        if self._sink is None or self._sink_target != target:
            self._sink = self._sink_factory(*target)
            self._sink_target = target
        return self._sink

    # ------------------------------------------------------------------
    # 設定の読み込み直し(6.7、CF-02〜CF-05)
    # ------------------------------------------------------------------
    async def _apply_sources(self, config: CollectorConfig) -> None:
        errors = []
        for source in self.sources:
            error = await source.apply_config(config)
            if error:
                errors.append(error)
        self._source_error = " / ".join(errors) if errors else None

    async def poll_config(self) -> None:
        """設定の版番号を確かめ、変わっていたら設定を読み込み直す(CF-02)。"""
        try:
            version = await self._db(read_config_version)
        except ConfigLoadError as exc:
            self._report_load_error(str(exc))
            return
        if version == self.runtime.config.version and self._load_error is None:
            return
        if version == self._failed_version:
            return  # 同じ版の設定は読み込みに失敗済み。次の変更を待つ
        try:
            config = await self._db(load_config)
        except ConfigLoadError as exc:
            self._failed_version = version
            self._report_load_error(str(exc))
            return
        self._load_error = None
        self._failed_version = None
        await self.apply_config(config)

    def _report_load_error(self, message: str) -> None:
        # 直前の設定のまま動作を続け、エラーをログとWebGUIに表示する(6.7.3)
        if message != self._load_error:
            self.logger.error("新しい設定を読み込めませんでした。直前の設定のまま動作を続けます:%s", message)
        self._load_error = message

    async def apply_config(self, config: CollectorConfig) -> None:
        """新しい設定を、再起動せずに反映する(CF-02、CF-04)。"""
        runtime = self.runtime
        old = runtime.config
        now = self._now_ns()
        runtime.set_config(config)

        if config.log_level != old.log_level:
            set_log_level(self.logger, config.log_level)
            self.logger.info("ログレベルを %s に変更しました", config.log_level)
        if config.queue_max_size != old.queue_max_size:
            discarded = runtime.queue.resize(config.queue_max_size)
            runtime.counters.add(dropped(DROP_QUEUE_FULL), discarded)
        target_changed = (config.zabbix_server, config.zabbix_port) != (old.zabbix_server, old.zabbix_port)
        if target_changed:
            self.sender.reset_connection()
            self.logger.info("Zabbixの送信先を変更しました(%s:%d)", config.zabbix_server or "未設定", config.zabbix_port)
        if config.self_monitor_interval_sec != old.self_monitor_interval_sec:
            self._next_monitor_ns = min(self._next_monitor_ns, now + config.self_monitor_interval_sec * _NS_PER_SEC)
        if config.lld_interval_sec != old.lld_interval_sec:
            self._next_lld_ns = min(self._next_lld_ns, now + config.lld_interval_sec * _NS_PER_SEC)

        needs_lld = runtime.tracker.apply_config(config.devices, now)
        await self._apply_sources(config)
        if target_changed and config.zabbix_server:
            # 新しい送信先(後から設定した場合を含む)へ、全機器分のLLD用データを送る(決定事項 D-014)
            self.send_lld(config.devices)
        else:
            # 機器の登録内容が変わった場合は、LLD用データを送り直す(6.7.1、LD-04)
            self.send_lld(needs_lld)
        self.logger.info("設定を読み込み直しました(設定の版番号 %d)", config.version)

    # ------------------------------------------------------------------
    # LLD用データ(6.5、LD-01〜LD-04)
    # ------------------------------------------------------------------
    def _resend_lld_after_recovery(self) -> None:
        """Zabbixへ送信できるようになった時点で、全機器分のLLD用データを送り直す(決定事項 D-014)。"""
        devices = self.runtime.config.devices
        if devices:
            self.logger.info("Zabbixへ送信できるようになったため、LLD用データを送り直します(%d 台)", len(devices))
        self.send_lld(devices)

    def send_lld(self, devices: tuple[DeviceConfig, ...] | list[DeviceConfig]) -> None:
        now = self._now_ns()
        for device in devices:
            if device.device_type != DEVICE_TYPE_CISCO_NEXUS:
                continue
            self.runtime.enqueue(build_lld_values(device.hostname, device.targets, now))

    # ------------------------------------------------------------------
    # 定期処理
    # ------------------------------------------------------------------
    async def tick(self) -> None:
        """期限が来た定期処理を行う。"""
        now = self._now_ns()
        config = self.runtime.config
        if now >= self._next_config_poll_ns:
            self._next_config_poll_ns = now + CONFIG_POLL_INTERVAL_SEC * _NS_PER_SEC
            await self.poll_config()
            config = self.runtime.config
        if now >= self._next_monitor_ns:
            self._next_monitor_ns = now + config.self_monitor_interval_sec * _NS_PER_SEC
            self.runtime.enqueue(build_monitor_values(self.runtime))
        if now >= self._next_lld_ns:
            self._next_lld_ns = now + config.lld_interval_sec * _NS_PER_SEC
            self.send_lld(config.devices)
        if now >= self._next_status_ns:
            self._next_status_ns = now + STATUS_WRITE_INTERVAL_SEC * _NS_PER_SEC
            await self.write_status_now()
        self.runtime.aggregator.flush_if_due()

    def log_status_changes(self) -> None:
        """途絶の発生と復旧を、ログに出す(7.4.6.2)。"""
        now = self._now_ns()
        timeout = self.runtime.config.stale_timeout_sec
        for device in self.runtime.tracker.runtimes():
            hostname = device.config.hostname
            extra = {HOSTNAME_FIELD: hostname}
            status = device.status(now, timeout)
            previous = device.reported_status
            if status != previous:
                if status == STATUS_STALE:
                    self.logger.warning("機器からの受信が途絶しました(%d秒間受信なし)", device.age_sec(now), extra=extra)
                elif status == STATUS_OK and previous == STATUS_STALE:
                    self.logger.info("機器からの受信が復旧しました", extra=extra)
                elif status == STATUS_OK and previous == STATUS_WAITING:
                    self.logger.info("機器からの受信を開始しました", extra=extra)
                device.reported_status = status
            for command in sorted(device.config.commands):
                command_status = device.command_status(command, now, timeout)
                previous_command = device.reported_command_status.get(command)
                if command_status != previous_command:
                    if command_status == STATUS_STALE:
                        self.logger.warning("「%s」の受信が途絶しました", command, extra=extra)
                    elif command_status == STATUS_OK and previous_command == STATUS_STALE:
                        self.logger.info("「%s」の受信が復旧しました", command, extra=extra)
                    device.reported_command_status[command] = command_status

    async def write_status_now(self) -> None:
        """状態データをSQLiteへ書き出す(MN-05)。"""
        self.log_status_changes()
        snapshot = build_status_snapshot(
            self.runtime,
            started_ns=self.started_ns,
            zabbix_connected=self.sender.connected,
            config_error=self.config_error,
        )
        try:
            await self._db(write_status, snapshot)
        except sqlite3.Error as exc:
            self.runtime.aggregator.add(logging.ERROR, f"状態データを書き出せませんでした:{exc}")
