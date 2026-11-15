"""Zabbix接続テスト(7.4.5.2、UI-13)。

入力されたZabbixサーバーに接続し、Zabbix sender プロトコルで「値が0件の送信」を行って、
Zabbixから応答が返ってくるかを確かめる。値を送らないため、Zabbix側の失敗件数は増えない。
"""

import json
import socket
import struct
import zlib

from common.constants import ZABBIX_TEST_TIMEOUT_SEC

_HEADER = b"ZBXD"
_FLAG_COMPRESSED = 0x02
# 応答の大きさの上限(バイト)(外部データの大きさの検査、11.3.3)
_MAX_RESPONSE_BYTES = 64 * 1024


def _read_exact(sock: socket.socket, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("途中で接続が切れました")
        data += chunk
    return data


def check_zabbix(server: str, port: int, timeout: float = ZABBIX_TEST_TIMEOUT_SEC) -> tuple[bool, str]:
    """接続できたかどうかと、画面に表示する結果の文言を返す。"""
    body = json.dumps({"request": "sender data", "data": []}).encode("utf-8")
    packet = _HEADER + b"\x01" + struct.pack("<II", len(body), 0) + body
    try:
        with socket.create_connection((server, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(packet)
            header = _read_exact(sock, 13)
            if header[:4] != _HEADER:
                return False, "接続できましたが、応答がZabbixのものではありません(ポート番号を確かめてください)"
            flags = header[4]
            length = struct.unpack("<I", header[5:9])[0]
            if length > _MAX_RESPONSE_BYTES:
                return False, "接続できましたが、応答が大きすぎます"
            payload = _read_exact(sock, length)
            if flags & _FLAG_COMPRESSED:
                payload = zlib.decompress(payload)
            response = json.loads(payload.decode("utf-8"))
    except TimeoutError:
        return False, f"接続できません({server}:{port} から{int(timeout)}秒以内に応答がありません)"
    except ConnectionRefusedError:
        return False, f"接続できません({server}:{port} に接続を拒否されました。ポート番号が違うか、Zabbixが起動していません)"
    except (OSError, ConnectionError, ValueError, zlib.error) as exc:
        return False, f"接続できません({server}:{port}、{type(exc).__name__})"

    if isinstance(response, dict) and "response" in response:
        return True, f"接続できました({server}:{port} のZabbixが応答しました)"
    return False, "接続できましたが、応答がZabbixのものではありません"
