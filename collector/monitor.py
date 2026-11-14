"""自己監視値と、状態データの組み立て(6.6.3、8.4、9.2.5、MN-02〜MN-05)。"""

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from collector.device_state import DeviceRuntime
from collector.models import KeyValue, ZabbixValue, stamp_values
from collector.runtime import Runtime
from collector.stats import RECEIVED, SENT_FAILED, SENT_OK, SKIPPED, dropped
from common.constants import (
    CMD_COUNTERS_BRIEF,
    CMD_PTP_BRIEF,
    CMD_PTP_CLOCK,
    CMD_PTP_PARENT,
    DROP_REASONS,
    KEY_COLLECTOR_DEVICES_STALE,
    KEY_COLLECTOR_DEVICES_TOTAL,
    KEY_COLLECTOR_DROPPED_FORMAT,
    KEY_COLLECTOR_QUEUE_SIZE,
    KEY_COLLECTOR_RECEIVED,
    KEY_COLLECTOR_SENT_FAILED,
    KEY_COLLECTOR_SENT_OK,
    KEY_COLLECTOR_SKIPPED,
    KEY_COMMAND_STATUS_FORMAT,
    KEY_DEVICE_AGE,
    KEY_DEVICE_STATUS,
    SELF_ZABBIX_HOSTNAME,
    STATUS_OK,
    STATUS_STALE,
    STATUS_WAITING,
)

# device_status テーブルの、コマンドごとの最終受信日時の列名(8.4.3)
COMMAND_LAST_COLUMNS: dict[str, str] = {
    CMD_COUNTERS_BRIEF: "last_counters_brief_at",
    CMD_PTP_BRIEF: "last_ptp_brief_at",
    CMD_PTP_CLOCK: "last_ptp_clock_at",
    CMD_PTP_PARENT: "last_ptp_parent_at",
}


def ns_to_iso(value_ns: int | None) -> str | None:
    """ナノ秒の時刻を、UTCのISO 8601形式の文字列にする(8.1.4)。"""
    if value_ns is None:
        return None
    return datetime.fromtimestamp(value_ns / 1_000_000_000, UTC).isoformat(timespec="milliseconds")


def command_status_key(command: str) -> str:
    return KEY_COMMAND_STATUS_FORMAT.format(command=command)


# ======================================================================
# 自己監視値(9.2.5)
# ======================================================================


def device_monitor_values(runtime: DeviceRuntime, now_ns: int, timeout_sec: int) -> list[ZabbixValue]:
    """各NWSWのホストへ送る自己監視値(9.2.5.1、MN-03)。

    受信待ちの間は、機器全体の受信状態と経過秒数を送らない(6.6.2.1)。
    コマンドごとの受信状態は、チェックしたコマンドのうち、受信待ちでないものだけを送る(6.6.2.2、6.6.2.3)。
    """
    key_values: list[KeyValue] = []
    status = runtime.status(now_ns, timeout_sec)
    if status != STATUS_WAITING:
        key_values.append(KeyValue(KEY_DEVICE_STATUS, 1 if status == STATUS_OK else 0))
        key_values.append(KeyValue(KEY_DEVICE_AGE, runtime.age_sec(now_ns)))
    for command in sorted(runtime.config.commands):
        command_status = runtime.command_status(command, now_ns, timeout_sec)
        if command_status != STATUS_WAITING:
            key_values.append(KeyValue(command_status_key(command), 1 if command_status == STATUS_OK else 0))
    return stamp_values(runtime.config.hostname, key_values, now_ns)


def collector_monitor_values(
    period: dict[str, int], queue_size: int, devices_total: int, devices_stale: int, now_ns: int
) -> list[ZabbixValue]:
    """「telemetry」ホストへ送る自己監視値(9.2.5.2、MN-04)。件数は前回の送信からの件数。"""
    key_values = [
        KeyValue(KEY_COLLECTOR_RECEIVED, period[RECEIVED]),
        *(KeyValue(KEY_COLLECTOR_DROPPED_FORMAT.format(reason=r), period[dropped(r)]) for r in DROP_REASONS),
        KeyValue(KEY_COLLECTOR_SKIPPED, period[SKIPPED]),
        KeyValue(KEY_COLLECTOR_SENT_OK, period[SENT_OK]),
        KeyValue(KEY_COLLECTOR_SENT_FAILED, period[SENT_FAILED]),
        KeyValue(KEY_COLLECTOR_QUEUE_SIZE, queue_size),
        KeyValue(KEY_COLLECTOR_DEVICES_TOTAL, devices_total),
        KeyValue(KEY_COLLECTOR_DEVICES_STALE, devices_stale),
    ]
    return stamp_values(SELF_ZABBIX_HOSTNAME, key_values, now_ns)


def build_monitor_values(runtime: Runtime) -> list[ZabbixValue]:
    """自己監視値をすべて組み立てる。「telemetry」ホストの件数は、ここで0に戻る。"""
    now_ns = runtime.now_ns()
    timeout = runtime.config.stale_timeout_sec
    values: list[ZabbixValue] = []
    stale = 0
    devices = runtime.tracker.runtimes()
    for device in devices:
        values.extend(device_monitor_values(device, now_ns, timeout))
        if device.status(now_ns, timeout) == STATUS_STALE:
            stale += 1
    values.extend(
        collector_monitor_values(runtime.counters.take_period(), len(runtime.queue), len(devices), stale, now_ns)
    )
    return values


# ======================================================================
# 状態データ(8.4)
# ======================================================================


@dataclass(frozen=True)
class StatusSnapshot:
    """SQLiteへ書き出す状態データの内容。"""

    system: dict[str, object]
    devices: list[dict[str, object]]
    unknown_sources: list[dict[str, object]]


def build_status_snapshot(
    runtime: Runtime,
    *,
    started_ns: int,
    zabbix_connected: bool,
    config_error: str | None,
) -> StatusSnapshot:
    """現在の状態を、状態データのテーブルの形にする(MN-05)。"""
    now_ns = runtime.now_ns()
    now_iso = ns_to_iso(now_ns)
    timeout = runtime.config.stale_timeout_sec
    recent = runtime.counters.recent()

    system = {
        "id": 1,
        "collector_heartbeat_at": now_iso,
        "collector_started_at": ns_to_iso(started_ns),
        "applied_config_version": runtime.config.version,
        "config_error": config_error,
        "zabbix_connected": 1 if zabbix_connected else 0,
        "received_per_min": recent[RECEIVED],
        "sent_ok_per_min": recent[SENT_OK],
        "sent_failed_per_min": recent[SENT_FAILED],
        "dropped_per_min": json.dumps({r: recent[dropped(r)] for r in DROP_REASONS}),
        "skipped_per_min": recent[SKIPPED],
        "queue_size": len(runtime.queue),
    }

    devices = []
    for device in runtime.tracker.runtimes():
        mismatches = device.mismatch_list()
        row: dict[str, object] = {
            "device_id": device.config.id,
            "status": device.status(now_ns, timeout),
            "last_received_at": ns_to_iso(device.last_received_ns()),
            "mismatch_interfaces": json.dumps(mismatches, ensure_ascii=False) if mismatches else None,
            "updated_at": now_iso,
        }
        for command, column in COMMAND_LAST_COLUMNS.items():
            row[column] = ns_to_iso(device.last_by_command.get(command))
        devices.append(row)

    unknown = [
        {
            "source_ip": entry.source_ip,
            "node_id": entry.node_id,
            "count": entry.count,
            "last_received_at": ns_to_iso(entry.last_received_ns),
        }
        for entry in runtime.unknown_sources.entries()
    ]
    return StatusSnapshot(system=system, devices=devices, unknown_sources=unknown)
