"""送信待ちキュー(6.4.1、TX-01、TX-02)。

メモリ上に置き、上限を持つ。満杯のときは古い値から破棄する。
受信側(値を入れる)と送信側(値を取り出す)は、このキューだけでつながる。
"""

import asyncio
from collections import deque
from collections.abc import Iterable

from collector.models import ZabbixValue


class SendQueue:
    """上限付きの送信待ちキュー。"""

    def __init__(self, max_size: int) -> None:
        if max_size < 1:
            raise ValueError("キューの上限は1以上にしてください")
        self._items: deque[ZabbixValue] = deque()
        self._max_size = max_size
        self._not_empty = asyncio.Event()

    @property
    def max_size(self) -> int:
        return self._max_size

    def __len__(self) -> int:
        return len(self._items)

    def put_many(self, values: Iterable[ZabbixValue]) -> int:
        """値を入れる。上限を超えた分は古い値から捨て、捨てた数を返す(TX-02)。"""
        discarded = 0
        for value in values:
            if len(self._items) >= self._max_size:
                self._items.popleft()
                discarded += 1
            self._items.append(value)
        if self._items:
            self._not_empty.set()
        return discarded

    def take(self, limit: int) -> list[ZabbixValue]:
        """古いものから最大 limit 件を取り出す。"""
        count = min(limit, len(self._items))
        batch = [self._items.popleft() for _ in range(count)]
        if not self._items:
            self._not_empty.clear()
        return batch

    async def wait_not_empty(self) -> None:
        """値が入るまで待つ。"""
        await self._not_empty.wait()

    def resize(self, max_size: int) -> int:
        """上限を変える。小さくした場合に入りきらない分は古い値から捨て、捨てた数を返す。"""
        if max_size < 1:
            raise ValueError("キューの上限は1以上にしてください")
        self._max_size = max_size
        discarded = 0
        while len(self._items) > max_size:
            self._items.popleft()
            discarded += 1
        return discarded
