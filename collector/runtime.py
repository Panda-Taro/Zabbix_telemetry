"""collector の実行中の状態を、受信部と共有するための入れ物。

受信部(メーカー・方式ごとの部品)は、この Runtime を通じて、件数の記録、
未登録の送信元の記録、受信状態の記録、送信待ちキューへの投入を行う。
"""

import logging
from collections.abc import Callable, Iterable

from collector.config import CollectorConfig, DeviceConfig
from collector.device_state import DeviceTracker
from collector.models import ZabbixValue
from collector.send_queue import SendQueue
from collector.stats import Counters, dropped
from collector.unknown_sources import UnknownSourceTracker
from common.constants import DROP_QUEUE_FULL
from common.logging_setup import LogAggregator


class Runtime:
    """collector の実行中の状態。"""

    def __init__(
        self,
        config: CollectorConfig,
        logger: logging.Logger,
        now_ns: Callable[[], int],
    ) -> None:
        self.config = config
        self.logger = logger
        self.now_ns = now_ns
        self.counters = Counters(now_ns)
        self.tracker = DeviceTracker()
        self.unknown_sources = UnknownSourceTracker()
        self.queue = SendQueue(config.queue_max_size)
        self.aggregator = LogAggregator(logger, now_ns=now_ns)
        self._devices_by_ip = dict(config.devices_by_ip)

    def set_config(self, config: CollectorConfig) -> None:
        self.config = config
        self._devices_by_ip = dict(config.devices_by_ip)

    def device_for_ip(self, source_ip: str, device_type: str) -> DeviceConfig | None:
        """送信元IPアドレスから、登録済みの機器を探す(RX-03)。機器の種類も一致するものだけ。"""
        device = self._devices_by_ip.get(source_ip)
        if device is None or device.device_type != device_type:
            return None
        return device

    def enqueue(self, values: Iterable[ZabbixValue]) -> None:
        """値を送信待ちキューに入れる。満杯で捨てた値は、件数を記録する(TX-01、TX-02)。"""
        discarded = self.queue.put_many(values)
        if discarded:
            self.counters.add(dropped(DROP_QUEUE_FULL), discarded)
            self.aggregator.add(logging.WARNING, "送信待ちキューが満杯のため、古い値を破棄しました(値の数)", count=discarded)
