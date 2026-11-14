"""Zabbixへの送信の窓口(9.2.1)。

ZabbixSink は「値の一覧を送り、成功件数と失敗件数を返す」部品。
本番では zabbix_utils の非同期版Sender を使う ZabbixUtilsSink を使い、
自動テストでは同じ形の模擬の部品に差し替える。
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from zabbix_utils import AsyncSender, ItemValue
from zabbix_utils.exceptions import ProcessingError

from collector.models import ZabbixValue
from common.constants import ZABBIX_BATCH_SIZE, ZABBIX_CONNECT_TIMEOUT_SEC, ZABBIX_SEND_TIMEOUT_SEC

# zabbix_utils が接続できなかったときに出すエラーの文面の始まり
_CONNECT_FAILURE_PREFIX = "Couldn't connect"

# zabbix_utils 自身のログは、collector の動作ログには出さない(同じ内容を collector がまとめて出すため)
_library_logger = logging.getLogger("zabbix_utils")
_library_logger.addHandler(logging.NullHandler())
_library_logger.propagate = False


@dataclass(frozen=True)
class SendResult:
    """Zabbixの応答。"""

    processed: int
    """Zabbixが受け付けた値の数(成功件数)。"""
    failed: int
    """Zabbixが受け付けなかった値の数(失敗件数。Zabbix側にアイテムがない、など)。"""


class ZabbixUnreachable(Exception):
    """Zabbixに接続できない、または応答がない。"""


class ZabbixRejected(Exception):
    """Zabbixが、送った値の全体を受け付けなかった(応答が「成功」でなかった)。"""


class ZabbixSink(Protocol):
    async def send(self, values: Sequence[ZabbixValue]) -> SendResult:
        """値を送り、Zabbixの応答を返す。

        接続できない場合は ZabbixUnreachable、全体を拒否された場合は ZabbixRejected を出す。
        """
        ...


class ZabbixUtilsSink:
    """zabbix_utils の非同期版Sender で送る(4.2、9.2.1)。"""

    def __init__(self, server: str, port: int) -> None:
        self.server = server
        self.port = port
        self._sender = AsyncSender(
            server=server, port=port, timeout=ZABBIX_CONNECT_TIMEOUT_SEC, chunk_size=ZABBIX_BATCH_SIZE
        )

    async def send(self, values: Sequence[ZabbixValue]) -> SendResult:
        items = [ItemValue(v.host, v.key, str(v.value), v.clock, v.ns) for v in values]
        try:
            response = await asyncio.wait_for(self._sender.send(items), timeout=ZABBIX_SEND_TIMEOUT_SEC)
        except ProcessingError as exc:
            message = str(exc)
            if message.startswith(_CONNECT_FAILURE_PREFIX):
                raise ZabbixUnreachable(f"Zabbixサーバー {self.server}:{self.port} に接続できません") from None
            raise ZabbixRejected(f"Zabbixが値を受け付けませんでした({message[:200]})") from None
        except (TimeoutError, asyncio.TimeoutError, asyncio.IncompleteReadError, OSError) as exc:
            raise ZabbixUnreachable(
                f"Zabbixサーバー {self.server}:{self.port} との通信に失敗しました({type(exc).__name__})"
            ) from None
        return SendResult(processed=int(response.processed), failed=int(response.failed))
