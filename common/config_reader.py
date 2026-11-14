"""設定データを読む部品。collector と web の両方が使う(8.1.3)。

このファイルには、読む処理だけを置く。書き込む処理は config_writer.py に置き、
config_writer.py は web だけが使う(CF-06)。
"""

import sqlite3
from dataclasses import dataclass

from common.constants import (
    COMMAND_COLUMNS,
    PURPOSE_COMMAND_COLUMNS,
    SETTING_CONFIG_VERSION,
)
from common.interfaces import InterfaceId
from common.validation import DeviceIdentity


@dataclass(frozen=True)
class DeviceRecord:
    """登録済みの機器1台分(devices と device_interfaces)。"""

    id: int
    hostname: str
    source_ip: str
    device_type: str
    commands: dict[str, bool]
    """devices テーブルの列名(cmd_counters_brief など)→ 送るか。"""
    interfaces: dict[str, tuple[InterfaceId, ...]]
    """用途(counters / ptp)→ 監視対象インターフェース。"""
    created_at: str
    updated_at: str


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """全体設定を「設定名 → 値」の形で読む。"""
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}


def get_config_version(conn: sqlite3.Connection) -> int:
    """設定の版番号(config_version)を読む(8.3.3.3)。"""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTING_CONFIG_VERSION,)).fetchone()
    if row is None:
        raise LookupError("設定の版番号(config_version)が見つかりません")
    return int(row["value"])


def list_device_identities(conn: sqlite3.Connection) -> list[DeviceIdentity]:
    """重複の検査に使う、登録済みの全機器のid・ホスト名・送信元IPアドレスを読む。"""
    return [
        DeviceIdentity(id=row["id"], hostname=row["hostname"], source_ip=row["source_ip"])
        for row in conn.execute("SELECT id, hostname, source_ip FROM devices ORDER BY id")
    ]


def _load_interfaces(conn: sqlite3.Connection, device_ids: list[int]) -> dict[int, dict[str, list[InterfaceId]]]:
    result: dict[int, dict[str, list[InterfaceId]]] = {
        device_id: {purpose: [] for purpose in PURPOSE_COMMAND_COLUMNS} for device_id in device_ids
    }
    rows = conn.execute("SELECT device_id, purpose, slot, port FROM device_interfaces ORDER BY device_id, purpose, slot, port")
    for row in rows:
        per_device = result.get(row["device_id"])
        if per_device is not None and row["purpose"] in per_device:
            per_device[row["purpose"]].append(InterfaceId(row["slot"], row["port"]))
    return result


def _to_record(row: sqlite3.Row, interfaces: dict[str, list[InterfaceId]]) -> DeviceRecord:
    return DeviceRecord(
        id=row["id"],
        hostname=row["hostname"],
        source_ip=row["source_ip"],
        device_type=row["device_type"],
        commands={column: bool(row[column]) for column in COMMAND_COLUMNS.values()},
        interfaces={purpose: tuple(values) for purpose, values in interfaces.items()},
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_devices(conn: sqlite3.Connection) -> list[DeviceRecord]:
    """登録済みの全機器を、ホスト名の順に読む。"""
    rows = conn.execute("SELECT * FROM devices ORDER BY hostname, id").fetchall()
    interfaces = _load_interfaces(conn, [row["id"] for row in rows])
    return [_to_record(row, interfaces[row["id"]]) for row in rows]


def get_device(conn: sqlite3.Connection, device_id: int) -> DeviceRecord | None:
    """機器1台を読む。見つからなければ None。"""
    row = conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone()
    if row is None:
        return None
    return _to_record(row, _load_interfaces(conn, [device_id])[device_id])
