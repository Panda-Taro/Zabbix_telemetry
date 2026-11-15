"""ログ画面の内容の読み出しと、ダウンロード用のファイルの作成(7.4.6、LG-07〜LG-09)。

- 動作ログ:collector と web のログファイル(1行1件のJSON、8.5.1)を読む。切り替え済みの古いファイルも読む
- 変更履歴:audit_log テーブルを読む
どちらも、日時、ログレベル(動作ログのみ)、ホスト名(動作ログのみ)、キーワードで絞り込み、
新しいものから最大1,000件を表示する。ダウンロードするのは、表示している内容と同じもの。
"""

import csv
import io
import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from common.constants import (
    COMPONENT_COLLECTOR,
    COMPONENT_WEB,
    LOG_BACKUP_COUNT,
    LOG_LEVELS,
    LOG_VIEW_MAX_ROWS,
    LOG_VIEW_TIMESTAMP_SLACK_SEC,
)
from web.dashboard import JST, parse_iso, to_jst_text

LEVEL_ORDER = {name: index for index, name in enumerate(LOG_LEVELS)}

CATEGORY_LABELS = {
    "device": "機器",
    "settings": "全体設定",
    "system": "システム設定",
    "user": "ユーザー管理",
    "auth": "ログイン",
    "restore": "設定の復元",
}
ACTION_LABELS = {
    "create": "登録",
    "update": "変更",
    "delete": "削除",
    "login": "ログイン",
    "logout": "ログアウト",
    "login_failed": "ログイン失敗",
    "restore": "復元",
}


@dataclass(frozen=True)
class LogFilter:
    """絞り込みの条件。日時は日本時間で入力されたもの。"""

    date_from: datetime | None = None
    date_to: datetime | None = None
    level: str = ""
    """この重要度以上のものを表示する(例:WARNING なら WARNING と ERROR)。空ならすべて。"""
    hostname: str = ""
    keyword: str = ""


@dataclass(frozen=True)
class LogEntry:
    timestamp: datetime
    level: str
    component: str
    hostname: str
    message: str

    @property
    def time_text(self) -> str:
        return to_jst_text(self.timestamp)


@dataclass(frozen=True)
class AuditEntry:
    timestamp: datetime | None
    username: str
    category: str
    action: str
    target: str
    before_value: str
    after_value: str

    @property
    def time_text(self) -> str:
        return to_jst_text(self.timestamp) if self.timestamp else ""

    @property
    def category_label(self) -> str:
        return CATEGORY_LABELS.get(self.category, self.category)

    @property
    def action_label(self) -> str:
        return ACTION_LABELS.get(self.action, self.action)


def parse_local_datetime(text: str) -> datetime | None:
    """画面の日時の入力(日本時間、例:2026-01-01T09:00)を読む。読めなければ None。"""
    text = text.strip()
    if not text or len(text) > 32:
        return None
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment.replace(tzinfo=JST) if moment.tzinfo is None else moment


def make_filter(date_from: str, date_to: str, level: str, hostname: str, keyword: str) -> LogFilter:
    return LogFilter(
        date_from=parse_local_datetime(date_from),
        date_to=parse_local_datetime(date_to),
        level=level if level in LEVEL_ORDER else "",
        hostname=hostname.strip()[:128],
        keyword=keyword.strip()[:200],
    )


# ======================================================================
# 動作ログ(ログファイル)
# ======================================================================


def _log_files(log_dir: Path, component: str) -> list[Path]:
    """新しいものから順に、ログファイルの一覧(collector.log、collector.log.1 …)。"""
    files = [log_dir / f"{component}.log"]
    files += [log_dir / f"{component}.log.{n}" for n in range(1, LOG_BACKUP_COUNT + 1)]
    return [path for path in files if path.is_file()]


def _parse_line(line: str) -> LogEntry | None:
    try:
        data = json.loads(line)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    timestamp = parse_iso(data.get("timestamp"))
    if timestamp is None:
        return None
    return LogEntry(
        timestamp=timestamp,
        level=str(data.get("level", "")),
        component=str(data.get("component", "")),
        hostname=str(data.get("hostname", "") or ""),
        message=str(data.get("message", "")),
    )


def _matches(entry: LogEntry, flt: LogFilter) -> bool:
    if flt.date_from and entry.timestamp < flt.date_from:
        return False
    if flt.date_to and entry.timestamp > flt.date_to:
        return False
    if flt.level and LEVEL_ORDER.get(entry.level, -1) < LEVEL_ORDER[flt.level]:
        return False
    if flt.hostname and flt.hostname.lower() not in entry.hostname.lower():
        return False
    if flt.keyword and flt.keyword.lower() not in entry.message.lower():
        return False
    return True


def _newest_first(log_dir: Path, component: str) -> Iterator[LogEntry]:
    for path in _log_files(log_dir, component):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            entry = _parse_line(line)
            if entry is not None:
                yield entry


def read_operation_logs(log_dir: Path | None, flt: LogFilter, limit: int = LOG_VIEW_MAX_ROWS) -> tuple[list[LogEntry], bool]:
    """動作ログを、新しいものから最大 limit 件読む。2つ目の値は、上限で打ち切ったかどうか。"""
    if log_dir is None or not log_dir.is_dir():
        return [], False
    collected: list[LogEntry] = []
    truncated = False
    for component in (COMPONENT_COLLECTOR, COMPONENT_WEB):
        count = 0
        for entry in _newest_first(log_dir, component):
            if flt.date_from and entry.timestamp < flt.date_from - timedelta(seconds=LOG_VIEW_TIMESTAMP_SLACK_SEC):
                break  # これより古いものは、条件に合わない(書き出しの順序の前後を少しだけ許す)
            if _matches(entry, flt):
                collected.append(entry)
                count += 1
                if count > limit:
                    truncated = True
                    break
    collected.sort(key=lambda e: e.timestamp, reverse=True)
    if len(collected) > limit:
        truncated = True
    return collected[:limit], truncated


# ======================================================================
# 変更履歴(audit_log)
# ======================================================================


def _to_utc_iso(moment: datetime) -> str:
    from datetime import UTC

    return moment.astimezone(UTC).isoformat(timespec="milliseconds")


def read_audit_logs(conn: sqlite3.Connection, flt: LogFilter, limit: int = LOG_VIEW_MAX_ROWS) -> tuple[list[AuditEntry], bool]:
    """変更履歴を、新しいものから最大 limit 件読む。"""
    conditions: list[str] = []
    params: list[object] = []
    if flt.date_from:
        conditions.append("timestamp >= ?")
        params.append(_to_utc_iso(flt.date_from))
    if flt.date_to:
        conditions.append("timestamp <= ?")
        params.append(_to_utc_iso(flt.date_to))
    if flt.keyword:
        pattern = "%" + flt.keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        conditions.append(
            "(username LIKE ? ESCAPE '\\' OR target LIKE ? ESCAPE '\\'"
            " OR before_value LIKE ? ESCAPE '\\' OR after_value LIKE ? ESCAPE '\\')"
        )
        params.extend([pattern] * 4)
    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = conn.execute(f"SELECT * FROM audit_log{where} ORDER BY id DESC LIMIT ?", (*params, limit + 1)).fetchall()
    entries = [
        AuditEntry(
            timestamp=parse_iso(row["timestamp"]),
            username=row["username"] or "",
            category=row["category"] or "",
            action=row["action"] or "",
            target=row["target"] or "",
            before_value=row["before_value"] or "",
            after_value=row["after_value"] or "",
        )
        for row in rows
    ]
    return entries[:limit], len(entries) > limit


# ======================================================================
# ダウンロード用のファイル(CSV)
# ======================================================================


def _safe_cell(value: str) -> str:
    """表計算ソフトで開いたときに、計算式として動かないようにする。"""
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def _csv_bytes(header: list[str], rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([_safe_cell(cell) for cell in row])
    # 表計算ソフト(Excel)で文字化けしないよう、先頭に目印(BOM)を付けたUTF-8にする
    return buffer.getvalue().encode("utf-8-sig")


def operation_logs_csv(entries: list[LogEntry]) -> bytes:
    return _csv_bytes(
        ["日時(日本時間)", "ログレベル", "出力元", "ホスト名", "内容"],
        [[e.time_text, e.level, e.component, e.hostname, e.message] for e in entries],
    )


def audit_logs_csv(entries: list[AuditEntry]) -> bytes:
    return _csv_bytes(
        ["日時(日本時間)", "ユーザー名", "変更した画面", "操作", "対象", "変更前の値", "変更後の値"],
        [
            [e.time_text, e.username, e.category_label, e.action_label, e.target, e.before_value, e.after_value]
            for e in entries
        ],
    )
