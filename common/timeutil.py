"""日時の扱い。日時はすべてUTCのISO 8601形式の文字列で保存する(8.1.4)。"""

from datetime import UTC, datetime


def utc_now_iso(now: datetime | None = None) -> str:
    """現在の日時(UTC)を、ISO 8601形式の文字列にする。例)2026-01-01T00:00:00.000+00:00"""
    moment = now if now is not None else datetime.now(UTC)
    return moment.astimezone(UTC).isoformat(timespec="milliseconds")
