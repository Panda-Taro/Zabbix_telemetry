"""届いた値を、Zabbixへ送る値に変換する(9.1.7.4、9.1.7.5、9.2.3.2)。"""

import math
import re
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from collector.models import TelemetryValue
from common.constants import BANDWIDTH_MBPS_TO_BPS, MAX_NUMERIC_TEXT_LENGTH, MAX_TEXT_VALUE_LENGTH


class ValueKind(Enum):
    """値の種類。種類ごとに、検査と変換の方法が決まる。"""

    TEXT = "text"
    """文字列をそのまま送る(clock-state、gm-id、state)。"""
    NUMBER = "number"
    """数値をそのまま送る(offset-from-master)。"""
    BANDWIDTH = "bandwidth"
    """MB/秒の数値を、bpsの整数に換算して送る(eth_inrate1、eth_outrate1)。"""


# 数値として受け付ける文字列の形(例:"1319"、"-4"、"12.5"、"1e3")
_NUMERIC_TEXT_PATTERN = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,3})?", re.ASCII)


def parse_number(raw: object) -> Decimal | None:
    """数値、または数値を表す文字列を読み取る。読めない場合は None を返す(9.1.7.5)。

    「N/A」などの文字列、真偽値、無限大・非数は、数値として読めないものとして扱う。
    """
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return Decimal(raw)
    if isinstance(raw, float):
        return Decimal(repr(raw)) if math.isfinite(raw) else None
    if isinstance(raw, str):
        text = raw.strip()
        if len(text) > MAX_NUMERIC_TEXT_LENGTH or _NUMERIC_TEXT_PATTERN.fullmatch(text) is None:
            return None
        return Decimal(text)
    return None


def convert_value(raw: object, kind: ValueKind) -> TelemetryValue | None:
    """届いた値を、Zabbixへ送る値に変換する。

    送らない値は None を返す。値がない場合に 0 などで埋めることはしない(9.1.7.4)。
    """
    if raw is None:
        return None

    if kind is ValueKind.TEXT:
        if isinstance(raw, str) and len(raw) <= MAX_TEXT_VALUE_LENGTH:
            return raw
        return None

    number = parse_number(raw)
    if number is None:
        return None

    if kind is ValueKind.NUMBER:
        if number == number.to_integral_value():
            return int(number)
        return float(number)

    # BANDWIDTH:MB/秒 × 8,000,000 = bps。Zabbix側の型が整数のため、整数に丸める
    bps = number * BANDWIDTH_MBPS_TO_BPS
    return int(bps.to_integral_value(rounding=ROUND_HALF_UP))
