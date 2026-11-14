"""状態データのテーブルへの書き込み(8.4、MN-05、OP-06)。collector だけが使う(8.1.3、CF-06)。

このファイルでは、状態データのテーブル(device_status、system_status、unknown_sources)だけに書き込む。
"""

import sqlite3

from collector.monitor import StatusSnapshot
from common.database import transaction

_SYSTEM_COLUMNS = (
    "id",
    "collector_heartbeat_at",
    "collector_started_at",
    "applied_config_version",
    "config_error",
    "zabbix_connected",
    "received_per_min",
    "sent_ok_per_min",
    "sent_failed_per_min",
    "dropped_per_min",
    "skipped_per_min",
    "queue_size",
)
_DEVICE_COLUMNS = (
    "device_id",
    "status",
    "last_received_at",
    "last_counters_brief_at",
    "last_ptp_brief_at",
    "last_ptp_clock_at",
    "last_ptp_parent_at",
    "mismatch_interfaces",
    "updated_at",
)
_UNKNOWN_COLUMNS = ("source_ip", "node_id", "count", "last_received_at")


def _insert_sql(table: str, columns: tuple[str, ...]) -> str:
    names = ", ".join(columns)
    placeholders = ", ".join(f":{c}" for c in columns)
    return f"INSERT INTO {table} ({names}) VALUES ({placeholders})"


def reset_status(conn: sqlite3.Connection) -> None:
    """状態データを、すべて消去する。collector の起動時に呼ぶ(8.4.2、OP-06)。"""
    with transaction(conn):
        conn.execute("DELETE FROM device_status")
        conn.execute("DELETE FROM system_status")
        conn.execute("DELETE FROM unknown_sources")


def write_status(conn: sqlite3.Connection, snapshot: StatusSnapshot) -> None:
    """状態データを、現在の内容で置き換える(8.4.1)。"""
    with transaction(conn):
        conn.execute("DELETE FROM system_status")
        conn.execute(_insert_sql("system_status", _SYSTEM_COLUMNS), snapshot.system)
        conn.execute("DELETE FROM device_status")
        conn.executemany(_insert_sql("device_status", _DEVICE_COLUMNS), snapshot.devices)
        conn.execute("DELETE FROM unknown_sources")
        conn.executemany(_insert_sql("unknown_sources", _UNKNOWN_COLUMNS), snapshot.unknown_sources)
