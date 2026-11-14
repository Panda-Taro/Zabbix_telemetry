"""ダッシュボードに表示する内容の組み立て(7.4.2、UI-04〜UI-06)。

状態データ(collector が書き出したもの)と設定データを読むだけで、書き込みはしない(8.1.3)。
表示色は 決定事項 D-022 のとおり:
  ok(緑):正常、接続中、稼働中
  warn(黄):受信待ち、不整合あり、未登録の送信元あり、設定の読み込みエラー、
            破棄(未登録の送信元、壊れたデータ、4つ以外のコマンド)
  error(赤):途絶、接続できない、collectorの停止、破棄(送信待ちキューが満杯、Zabbixに接続できない)
  muted(灰):対象外、不明
"""

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from common.config_reader import get_settings, list_devices
from common.constants import (
    CMD_COUNTERS_BRIEF,
    CMD_PTP_BRIEF,
    CMD_PTP_CLOCK,
    CMD_PTP_PARENT,
    COLLECTOR_HEARTBEAT_TIMEOUT_SEC,
    COMMAND_COLUMNS,
    DROP_INVALID_DATA,
    DROP_QUEUE_FULL,
    DROP_UNKNOWN_COMMAND,
    DROP_UNKNOWN_SOURCE,
    DROP_ZABBIX_UNREACHABLE,
    SETTING_DEFAULTS,
    SETTING_QUEUE_MAX_SIZE,
    SETTING_STALE_TIMEOUT_SEC,
    STATUS_OK,
    STATUS_STALE,
    STATUS_WAITING,
)

# 日本時間(日本には夏時間がないため、9時間の固定の差とする)(8.1.4)
JST = timezone(timedelta(hours=9), "JST")

OK, WARN, ERROR, MUTED = "ok", "warn", "error", "muted"

# 表示するコマンドの順番と、device_status の列名(8.4.3)
COMMAND_DISPLAY: tuple[tuple[str, str], ...] = (
    (CMD_COUNTERS_BRIEF, "last_counters_brief_at"),
    (CMD_PTP_BRIEF, "last_ptp_brief_at"),
    (CMD_PTP_CLOCK, "last_ptp_clock_at"),
    (CMD_PTP_PARENT, "last_ptp_parent_at"),
)

# 破棄の理由の表示名と単位と色(8.4.4.2、9.2.5.3、D-022)
DROP_DISPLAY: tuple[tuple[str, str, str, str], ...] = (
    (DROP_UNKNOWN_SOURCE, "未登録の送信元", "パケット", WARN),
    (DROP_INVALID_DATA, "壊れたデータ", "パケット", WARN),
    (DROP_UNKNOWN_COMMAND, "4つ以外のコマンド", "パケット", WARN),
    (DROP_QUEUE_FULL, "送信待ちキューが満杯", "値", ERROR),
    (DROP_ZABBIX_UNREACHABLE, "Zabbixに接続できない", "値", ERROR),
)

STATUS_DISPLAY = {
    STATUS_OK: ("正常", OK),
    STATUS_STALE: ("途絶", ERROR),
    STATUS_WAITING: ("受信待ち", WARN),
}


@dataclass(frozen=True)
class Badge:
    text: str
    color: str


@dataclass(frozen=True)
class TimeCell:
    """日時の表示(日本時間と、そこからの経過時間)。"""

    text: str
    elapsed: str
    color: str


@dataclass(frozen=True)
class DropCell:
    label: str
    count: int
    unit: str
    color: str


@dataclass(frozen=True)
class CommandCell:
    command: str
    cell: TimeCell


@dataclass(frozen=True)
class DeviceRow:
    hostname: str
    source_ip: str
    status: Badge
    last_received: TimeCell
    commands: list[CommandCell]
    mismatches: list[str]


@dataclass(frozen=True)
class UnknownSourceRow:
    source_ip: str
    node_id: str
    count: int
    last_received: TimeCell


@dataclass
class DashboardView:
    collector: Badge
    zabbix: Badge
    devices_total: int
    devices_breakdown: list[Badge]
    received: int
    sent_ok: int
    sent_failed: int
    sent_failed_color: str
    drops: list[DropCell]
    skipped: int
    queue_size: int
    queue_max: int
    config_error: str | None
    collector_running: bool
    devices: list[DeviceRow] = field(default_factory=list)
    unknown_sources: list[UnknownSourceRow] = field(default_factory=list)


# ======================================================================
# 日時の表示
# ======================================================================


def parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def to_jst_text(moment: datetime) -> str:
    """日本時間の表示(例:2026-01-01 09:00:00)(8.1.4)。"""
    return moment.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S")


def elapsed_text(seconds: float) -> str:
    """経過時間の表示(例:12秒前、3分12秒前、2時間5分前、3日前)。"""
    total = max(0, int(seconds))
    if total < 60:
        return f"{total}秒前"
    if total < 3600:
        return f"{total // 60}分{total % 60}秒前"
    if total < 86400:
        return f"{total // 3600}時間{(total % 3600) // 60}分前"
    return f"{total // 86400}日前"


def time_cell(value: object, now: datetime, color: str = "") -> TimeCell:
    moment = parse_iso(value)
    if moment is None:
        return TimeCell(text="—", elapsed="", color=color or MUTED)
    return TimeCell(text=to_jst_text(moment), elapsed=elapsed_text((now - moment).total_seconds()), color=color)


# ======================================================================
# 組み立て
# ======================================================================


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _mismatch_texts(raw: object) -> list[str]:
    """不整合の一覧(D-015 の形)を、表示の文字列にする。"""
    if not isinstance(raw, str) or not raw:
        return []
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    texts = []
    if isinstance(items, list):
        for item in items:
            if isinstance(item, dict):
                texts.append(f"{item.get('command', '')}: {item.get('ifnum', '')}")
    return texts


def build_dashboard(conn: sqlite3.Connection, now: datetime) -> DashboardView:
    """ダッシュボードに表示する内容を組み立てる。"""
    settings = get_settings(conn)
    stale_timeout = _int(settings.get(SETTING_STALE_TIMEOUT_SEC, SETTING_DEFAULTS[SETTING_STALE_TIMEOUT_SEC]))
    queue_max = _int(settings.get(SETTING_QUEUE_MAX_SIZE, SETTING_DEFAULTS[SETTING_QUEUE_MAX_SIZE]))

    system = conn.execute("SELECT * FROM system_status WHERE id = 1").fetchone()
    heartbeat = parse_iso(system["collector_heartbeat_at"]) if system else None
    running = heartbeat is not None and (now - heartbeat).total_seconds() <= COLLECTOR_HEARTBEAT_TIMEOUT_SEC

    # collector の稼働状態と、Zabbixとの接続状態(7.4.2.2)
    collector = Badge("稼働中", OK) if running else Badge("停止", ERROR)
    if not running:
        zabbix = Badge("不明(collectorが停止しています)", MUTED)
    elif system["zabbix_connected"]:
        zabbix = Badge("接続中", OK)
    else:
        zabbix = Badge("接続できない", ERROR)

    dropped: dict[str, object] = {}
    if system and system["dropped_per_min"]:
        try:
            parsed = json.loads(system["dropped_per_min"])
            dropped = parsed if isinstance(parsed, dict) else {}
        except ValueError:
            dropped = {}
    drops = []
    for reason, label, unit, color in DROP_DISPLAY:
        count = _int(dropped.get(reason, 0))
        drops.append(DropCell(label=label, count=count, unit=unit, color=color if count else OK))

    # 機器ごとの状態(7.4.2.3)
    statuses = {row["device_id"]: row for row in conn.execute("SELECT * FROM device_status")}
    counts = {STATUS_OK: 0, STATUS_STALE: 0, STATUS_WAITING: 0, "unknown": 0}
    rows: list[DeviceRow] = []
    for device in list_devices(conn):
        status_row = statuses.get(device.id)
        if status_row is None:
            badge = Badge("不明", MUTED)
            counts["unknown"] += 1
        else:
            text, color = STATUS_DISPLAY.get(status_row["status"], ("不明", MUTED))
            badge = Badge(text, color)
            counts[status_row["status"] if status_row["status"] in counts else "unknown"] += 1

        commands: list[CommandCell] = []
        for command, column in COMMAND_DISPLAY:
            if not device.commands.get(COMMAND_COLUMNS[command]):
                commands.append(CommandCell(command, TimeCell(text="対象外", elapsed="", color=MUTED)))
                continue
            value = status_row[column] if status_row is not None else None
            moment = parse_iso(value)
            if moment is None:
                commands.append(CommandCell(command, TimeCell(text="未受信", elapsed="", color=WARN)))
                continue
            # 最終受信時刻から途絶判定時間が過ぎたコマンドは、赤で表示する(D-025)
            late = (now - moment).total_seconds() >= stale_timeout
            commands.append(CommandCell(command, time_cell(value, now, ERROR if late else OK)))

        last = status_row["last_received_at"] if status_row is not None else None
        rows.append(
            DeviceRow(
                hostname=device.hostname,
                source_ip=device.source_ip,
                status=badge,
                last_received=time_cell(last, now),
                commands=commands,
                mismatches=_mismatch_texts(status_row["mismatch_interfaces"] if status_row is not None else None),
            )
        )

    breakdown = [
        Badge(f"正常 {counts[STATUS_OK]}", OK if counts[STATUS_OK] else MUTED),
        Badge(f"途絶 {counts[STATUS_STALE]}", ERROR if counts[STATUS_STALE] else MUTED),
        Badge(f"受信待ち {counts[STATUS_WAITING]}", WARN if counts[STATUS_WAITING] else MUTED),
    ]
    if counts["unknown"]:
        breakdown.append(Badge(f"不明 {counts['unknown']}", MUTED))

    # 未登録の送信元(7.4.2.4)
    unknown = [
        UnknownSourceRow(
            source_ip=row["source_ip"],
            node_id=row["node_id"] or "",
            count=_int(row["count"]),
            last_received=time_cell(row["last_received_at"], now),
        )
        for row in conn.execute("SELECT * FROM unknown_sources ORDER BY last_received_at DESC")
    ]

    sent_failed = _int(system["sent_failed_per_min"]) if system else 0
    return DashboardView(
        collector=collector,
        zabbix=zabbix,
        devices_total=len(rows),
        devices_breakdown=breakdown,
        received=_int(system["received_per_min"]) if system else 0,
        sent_ok=_int(system["sent_ok_per_min"]) if system else 0,
        sent_failed=sent_failed,
        sent_failed_color=ERROR if sent_failed else OK,
        drops=drops,
        skipped=_int(system["skipped_per_min"]) if system else 0,
        queue_size=_int(system["queue_size"]) if system else 0,
        queue_max=queue_max,
        config_error=(system["config_error"] or None) if system else None,
        collector_running=running,
        devices=rows,
        unknown_sources=unknown,
    )
