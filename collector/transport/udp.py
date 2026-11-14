"""UDPの受け口(RX-01、11.1.3)。メーカーに依存しない共通部品。

受け取ったパケットは、すぐに受信部の処理(callback)へ渡す。
Zabbixへの送信は待たないため、Zabbixの応答が遅くても受信は止まらない(RX-08)。
"""

import asyncio
import logging
import socket
from collections.abc import Callable

from common.constants import UDP_BIND_ADDRESS, UDP_RECEIVE_BUFFER_BYTES

# 受け取ったパケットを渡す先。(パケットの中身, 送信元IPアドレス, 受信時刻[ナノ秒])
PacketCallback = Callable[[bytes, str, int], None]


class _DatagramProtocol(asyncio.DatagramProtocol):
    def __init__(self, callback: PacketCallback, now_ns: Callable[[], int], logger: logging.Logger) -> None:
        self._callback = callback
        self._now_ns = now_ns
        self._logger = logger

    def datagram_received(self, data: bytes, addr: tuple) -> None:  # type: ignore[override]
        received_ns = self._now_ns()
        try:
            self._callback(data, str(addr[0]), received_ns)
        except Exception:  # noqa: BLE001 1件の処理のエラーで受信を止めない(RX-09)
            self._logger.exception("受信したデータの処理中に想定外のエラーが起きました")

    def error_received(self, exc: Exception) -> None:
        self._logger.warning("UDPの受信でエラーが起きました:%s", exc)


class UdpListener:
    """指定したポート番号で、UDPのパケットを受け取る。"""

    def __init__(self, port: int, transport: asyncio.DatagramTransport) -> None:
        self.port = port
        self._transport = transport

    @classmethod
    async def open(
        cls,
        port: int,
        callback: PacketCallback,
        now_ns: Callable[[], int],
        logger: logging.Logger,
        bind_address: str = UDP_BIND_ADDRESS,
    ) -> "UdpListener":
        """受け口を開く。開けない場合(ポートが使用中など)は OSError を出す。"""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # 受信が集中しても取りこぼさないよう、受信バッファを大きくする(11.1.3)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, UDP_RECEIVE_BUFFER_BYTES)
            sock.bind((bind_address, port))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _DatagramProtocol(callback, now_ns, logger), sock=sock
        )
        actual = sock.getsockname()
        logger.info(
            "UDPの受信を開始しました(ポート番号 %d、受信バッファ %d バイト)",
            port,
            sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF),
        )
        return cls(port=actual[1] if port == 0 else port, transport=transport)

    def close(self) -> None:
        self._transport.close()
