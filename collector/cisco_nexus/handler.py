"""Cisco Nexus から届いた1つのパケットの処理(6.4.1 (b)〜(e)、RX-02〜RX-07、RX-09)。"""

import logging

from collector.cisco_nexus.commands import identify_command
from collector.cisco_nexus.decoder import InvalidDataError, decode_packet
from collector.models import stamp_values
from collector.runtime import Runtime
from collector.stats import RECEIVED, SKIPPED, dropped
from common.constants import (
    DEVICE_TYPE_CISCO_NEXUS,
    DROP_INVALID_DATA,
    DROP_UNKNOWN_COMMAND,
    DROP_UNKNOWN_SOURCE,
)

# 4つ以外のコマンドとしてログに出す encoding_path の文字数の上限
_LOG_PATH_MAX_LENGTH = 64


class CiscoNexusHandler:
    """Cisco Nexus のTelemetryの、1パケットごとの処理。"""

    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    def handle(self, payload: bytes, source_ip: str, received_ns: int) -> None:
        """1つのパケットを処理する。どんなデータでも例外を外へ出さない(RX-09)。"""
        self.runtime.counters.add(RECEIVED)
        try:
            self._handle(payload, source_ip, received_ns)
        except Exception:  # noqa: BLE001 そのデータだけを破棄して、処理を続ける
            self.runtime.counters.add(dropped(DROP_INVALID_DATA))
            self.runtime.logger.exception("受信したデータの処理中にエラーが起きたため、破棄しました(送信元 %s)", source_ip)

    def _unknown_source(self, source_ip: str, node_id: str | None, received_ns: int, reason: str) -> None:
        runtime = self.runtime
        runtime.counters.add(dropped(DROP_UNKNOWN_SOURCE))
        runtime.unknown_sources.record(source_ip, node_id, received_ns)
        runtime.aggregator.add(logging.WARNING, f"{reason}のデータを破棄しました(送信元 {source_ip})")

    def _handle(self, payload: bytes, source_ip: str, received_ns: int) -> None:
        runtime = self.runtime

        # (b) 送信元の確認(RX-03)
        device = runtime.device_for_ip(source_ip, DEVICE_TYPE_CISCO_NEXUS)
        if device is None:
            node_id: str | None
            try:
                node_id = decode_packet(payload).node_id
            except InvalidDataError:
                node_id = None
            self._unknown_source(source_ip, node_id, received_ns, "未登録の送信元から")
            return

        # (c) 解析(RX-02)
        try:
            message = decode_packet(payload)
        except InvalidDataError as exc:
            runtime.counters.add(dropped(DROP_INVALID_DATA))
            runtime.aggregator.add(logging.WARNING, f"壊れたデータを破棄しました:{exc}", hostname=device.hostname)
            return

        # ホスト名の一致の確認(RX-04)
        if message.node_id != device.hostname:
            self._unknown_source(source_ip, message.node_id, received_ns, "登録したホスト名と一致しないホスト名")
            return

        # showコマンドの判別(RX-06)
        spec = identify_command(message.encoding_path)
        if spec is None:
            runtime.counters.add(dropped(DROP_UNKNOWN_COMMAND))
            path = message.encoding_path.strip()[:_LOG_PATH_MAX_LENGTH]
            runtime.aggregator.add(
                logging.WARNING, f"4つ以外のコマンド「{path}」のデータを破棄しました", hostname=device.hostname
            )
            return

        # チェックを外したコマンドは「対象外」として件数だけ数える(RX-07)
        if spec.name not in device.commands:
            runtime.counters.add(SKIPPED)
            return

        # (d) 値の取り出し(CV-01〜CV-11)
        extraction = spec.extract(message.data, device.targets_for(spec.name))
        runtime.tracker.record(device.id, spec.name, extraction.missing_interfaces, received_ns)

        # (e) 受信時刻を付けて、送信待ちキューへ(CV-12、TX-01、TX-04)
        runtime.enqueue(stamp_values(device.hostname, extraction.values, received_ns))
