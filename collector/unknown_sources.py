"""未登録の送信元の記録(5.4.2、8.4.5、RX-05)。"""

from collections.abc import Iterable
from dataclasses import dataclass

from common.constants import MAX_STORED_NODE_ID_LENGTH, UNKNOWN_SOURCES_MAX


@dataclass
class UnknownSource:
    source_ip: str
    node_id: str | None
    """データ内のホスト名。読み取れない場合は None。"""
    count: int
    last_received_ns: int


class UnknownSourceTracker:
    """未登録の送信元を、最大100件まで記録する(8.4.5.1)。"""

    def __init__(self, max_entries: int = UNKNOWN_SOURCES_MAX) -> None:
        self._max_entries = max_entries
        self._entries: dict[str, UnknownSource] = {}

    def record(self, source_ip: str, node_id: str | None, received_ns: int) -> None:
        if node_id is not None:
            node_id = node_id[:MAX_STORED_NODE_ID_LENGTH]
        entry = self._entries.get(source_ip)
        if entry is None:
            entry = self._entries[source_ip] = UnknownSource(source_ip, node_id, 0, received_ns)
        entry.count += 1
        entry.last_received_ns = received_ns
        if node_id is not None:
            entry.node_id = node_id
        if len(self._entries) > self._max_entries:
            # 最後に受信した日時が古いものから削除する(8.4.5.1)
            oldest = min(self._entries.values(), key=lambda e: e.last_received_ns)
            del self._entries[oldest.source_ip]

    def forget(self, source_ips: Iterable[str]) -> None:
        """指定した送信元IPアドレスの記録を消す(決定事項 D-035)。"""
        for source_ip in source_ips:
            self._entries.pop(source_ip, None)

    def entries(self) -> list[UnknownSource]:
        return sorted(self._entries.values(), key=lambda e: e.last_received_ns, reverse=True)
