"""送信待ちキューから値を取り出して、Zabbixへ送る(6.4.1 (f)(g)、TX-03〜TX-08)。

受信側とは独立して動く。Zabbixの応答が遅い間も、受信側は止まらない(RX-08)。
- 最大250件をまとめて送る。キューに値がある間は、待たずに続けて送る(TX-03)
- Zabbixの応答から、成功件数と失敗件数を記録する。失敗した値は再送しない(TX-06、TX-07)
- 接続できない場合は、その値を破棄して件数を記録し、一定間隔をおいて接続を試み直す。
  接続を試み直すまでの間に取り出した値も、破棄して件数を記録する(TX-08)
- ZabbixサーバーのIPアドレスが未設定の間は、値を「接続できない」として破棄する(6.8)
- 送信できない状態から送信できる状態になった時点で、on_recovered を呼ぶ。
  collector はこれを受けて、全機器分のLLD用データを送り直す(決定事項 D-014)
"""

import logging
from collections.abc import Callable

from collector.runtime import Runtime
from collector.stats import SENT_FAILED, SENT_OK, dropped
from collector.zabbix_sink import ZabbixRejected, ZabbixSink, ZabbixUnreachable
from common.constants import DROP_ZABBIX_UNREACHABLE, ZABBIX_BATCH_SIZE, ZABBIX_RECONNECT_INTERVAL_SEC

_NS_PER_SEC = 1_000_000_000


class ZabbixSender:
    """Zabbixへの送信を受け持つ。"""

    def __init__(
        self,
        runtime: Runtime,
        sink_provider: Callable[[], ZabbixSink | None],
        on_recovered: Callable[[], None] | None = None,
    ) -> None:
        self.runtime = runtime
        self._sink_provider = sink_provider
        self._on_recovered = on_recovered
        self.connected = False
        self._retry_at_ns = 0
        # 送信できない間に値を破棄した、または送信先が変わったために、
        # 次に送信できたときにLLD用データを送り直す必要があるか
        self._recovery_pending = False

    def reset_connection(self) -> None:
        """送信先が変わったときに呼ぶ。次の値から、すぐに接続を試みる。

        新しい送信先へのLLD用データは、呼び出す側(collector)がすぐに送信待ちキューへ入れる。
        それが届かなかった場合は、送信できるようになった時点で送り直す。
        """
        self._retry_at_ns = 0
        self._recovery_pending = False

    def _drop(self, count: int, reason_message: str) -> None:
        self._recovery_pending = True
        self.runtime.counters.add(dropped(DROP_ZABBIX_UNREACHABLE), count)
        self.runtime.aggregator.add(logging.WARNING, f"{reason_message}。値を破棄しました(値の数)", count=count)

    def _set_connected(self, connected: bool, detail: str = "") -> None:
        if connected and not self.connected:
            self.runtime.logger.info("Zabbixへの送信ができるようになりました")
        elif not connected and self.connected:
            self.runtime.logger.warning("Zabbixへ送信できなくなりました:%s", detail)
        self.connected = connected
        if connected and self._recovery_pending:
            self._recovery_pending = False
            if self._on_recovered is not None:
                self._on_recovered()

    async def send_batch(self) -> int:
        """キューから最大250件を取り出して送る。取り出した件数を返す。"""
        batch = self.runtime.queue.take(ZABBIX_BATCH_SIZE)
        if not batch:
            return 0

        sink = self._sink_provider()
        if sink is None:
            self._set_connected(False, "ZabbixサーバーのIPアドレスが設定されていません")
            self._drop(len(batch), "ZabbixサーバーのIPアドレスが設定されていないため")
            return len(batch)

        now_ns = self.runtime.now_ns()
        if now_ns < self._retry_at_ns:
            self._drop(len(batch), "Zabbixに接続できないため")
            return len(batch)

        try:
            result = await sink.send(batch)
        except ZabbixUnreachable as exc:
            self._set_connected(False, str(exc))
            self._retry_at_ns = self.runtime.now_ns() + ZABBIX_RECONNECT_INTERVAL_SEC * _NS_PER_SEC
            self._drop(len(batch), "Zabbixに接続できないため")
            return len(batch)
        except ZabbixRejected as exc:
            # Zabbixには届いたが、全体が受け付けられなかった。失敗として数え、再送しない(TX-07)
            self._set_connected(True)
            self.runtime.counters.add(SENT_FAILED, len(batch))
            self.runtime.aggregator.add(logging.WARNING, str(exc))
            return len(batch)
        except Exception:  # noqa: BLE001 想定外のエラーでも送信を止めない
            self.runtime.logger.exception("Zabbixへの送信中に想定外のエラーが起きました")
            self._set_connected(False, "想定外のエラー")
            self._retry_at_ns = self.runtime.now_ns() + ZABBIX_RECONNECT_INTERVAL_SEC * _NS_PER_SEC
            self._drop(len(batch), "Zabbixへの送信中にエラーが起きたため")
            return len(batch)

        self._set_connected(True)
        self._retry_at_ns = 0
        self.runtime.counters.add(SENT_OK, result.processed)
        self.runtime.counters.add(SENT_FAILED, result.failed)
        if result.failed:
            self.runtime.aggregator.add(
                logging.WARNING,
                "Zabbixが受け付けなかった値があります(Zabbix側にアイテムがない、など)(値の数)",
                count=result.failed,
            )
        return len(batch)

    async def drain(self) -> None:
        """キューが空になるまで送る。"""
        while len(self.runtime.queue):
            await self.send_batch()

    async def run(self) -> None:
        """値が入るのを待って送ることを、止められるまで繰り返す。"""
        while True:
            await self.runtime.queue.wait_not_empty()
            await self.send_batch()
