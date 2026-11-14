"""件数の集計(6.6.3、7.4.2.2、8.4.4、9.2.5.2、9.2.5.3)。

同じ件数を、2つの区切りで数える。
  - 自己監視値の送信間隔ごと(Zabbixの「telemetry」ホストへ送る件数。送るたびに0に戻す)
  - 直近1分間(ダッシュボードに表示する件数)
"""

import time
from collections import Counter
from collections.abc import Callable

from common.constants import DROP_REASONS, RECENT_WINDOW_SEC

RECEIVED = "received"
"""受信件数(パケット数)"""
SKIPPED = "skipped"
"""対象外の件数(パケット数)"""
SENT_OK = "sent_ok"
"""Zabbix送信の成功件数(値の数)"""
SENT_FAILED = "sent_failed"
"""Zabbix送信の失敗件数(値の数)"""


def dropped(reason: str) -> str:
    """破棄件数の名前。reason は破棄の理由(8.4.4.2)。"""
    if reason not in DROP_REASONS:
        raise ValueError(f"不明な破棄の理由です:{reason}")
    return f"dropped.{reason}"


COUNTER_NAMES: tuple[str, ...] = (RECEIVED, SKIPPED, SENT_OK, SENT_FAILED, *(dropped(r) for r in DROP_REASONS))


class Counters:
    """件数を数える。"""

    def __init__(self, now_ns: Callable[[], int] = time.time_ns) -> None:
        self._now_ns = now_ns
        self._period: Counter[str] = Counter()
        self._buckets: dict[int, Counter[str]] = {}

    def add(self, name: str, count: int = 1) -> None:
        if count <= 0:
            return
        self._period[name] += count
        second = self._now_ns() // 1_000_000_000
        bucket = self._buckets.get(second)
        if bucket is None:
            self._prune(second)
            bucket = self._buckets[second] = Counter()
        bucket[name] += count

    def _prune(self, current_second: int) -> None:
        oldest = current_second - RECENT_WINDOW_SEC
        for second in [s for s in self._buckets if s <= oldest]:
            del self._buckets[second]

    def take_period(self) -> dict[str, int]:
        """前回からの件数を返し、0に戻す(自己監視値の送信用、9.2.5.2)。"""
        snapshot = {name: self._period.get(name, 0) for name in COUNTER_NAMES}
        self._period = Counter()
        return snapshot

    def recent(self) -> dict[str, int]:
        """直近1分間の件数を返す(ダッシュボード用、7.4.2.2)。"""
        current = self._now_ns() // 1_000_000_000
        self._prune(current)
        total: Counter[str] = Counter()
        for bucket in self._buckets.values():
            total.update(bucket)
        return {name: total.get(name, 0) for name in COUNTER_NAMES}
