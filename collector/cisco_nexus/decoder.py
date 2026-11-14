"""受信したUDPパケットから、Telemetryのデータを取り出す(9.1.1.1、9.1.2、RX-02)。"""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from common.constants import MAX_TELEMETRY_PAYLOAD_BYTES


class InvalidDataError(Exception):
    """壊れたデータ。破棄の理由「invalid_data」(8.4.4.2)として扱う。"""


@dataclass(frozen=True)
class TelemetryMessage:
    """Telemetryの1回分のデータのうち、本システムが使う3項目(9.1.2.2)。"""

    node_id: str
    """Nexusのホスト名(node_id_str)。"""
    encoding_path: str
    """showコマンドの種類(encoding_path)。前後の空白は、判別のときに除く。"""
    data: Mapping[str, Any]
    """コマンドごとの出力(data)。"""


def decode_packet(payload: bytes) -> TelemetryMessage:
    """UDPパケットの中身から、Telemetryのデータを取り出す。

    パケットの先頭には NX-OS が付けるヘッダーがあるため、最初の「{」から末尾までを
    JSONとして解析する(9.1.1.1)。解析できない場合や、必要な項目の形が正しくない場合は
    InvalidDataError を出す。それ以外の例外は出さない(RX-09)。
    """
    if not isinstance(payload, (bytes, bytearray)):
        raise InvalidDataError("受信データがバイト列ではありません")
    if len(payload) > MAX_TELEMETRY_PAYLOAD_BYTES:
        raise InvalidDataError(f"受信データが大きすぎます({len(payload)}バイト)")

    start = payload.find(b"{")
    if start < 0:
        raise InvalidDataError("JSONの開始位置「{」が見つかりません")

    try:
        document = json.loads(payload[start:].decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise InvalidDataError("文字コードがUTF-8ではありません") from exc
    except RecursionError as exc:
        raise InvalidDataError("JSONの入れ子が深すぎます") from exc
    except ValueError as exc:
        raise InvalidDataError(f"JSONとして解析できません({exc})") from exc

    if not isinstance(document, dict):
        raise InvalidDataError("JSONの最上位が { } の形ではありません")

    node_id = document.get("node_id_str")
    encoding_path = document.get("encoding_path")
    data = document.get("data")
    if not isinstance(node_id, str) or node_id == "":
        raise InvalidDataError("node_id_str がないか、文字列ではありません")
    if not isinstance(encoding_path, str):
        raise InvalidDataError("encoding_path がないか、文字列ではありません")
    if not isinstance(data, dict):
        raise InvalidDataError("data がないか、{ } の形ではありません")

    return TelemetryMessage(node_id=node_id, encoding_path=encoding_path, data=data)
