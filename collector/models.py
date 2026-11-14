"""collector の中でやり取りする値の型。"""

from collections.abc import Iterable
from dataclasses import dataclass

# Zabbixへ送る値の型。文字列、整数、浮動小数のいずれか
TelemetryValue = str | int | float

_NS_PER_SEC = 1_000_000_000


@dataclass(frozen=True)
class KeyValue:
    """Zabbixのキーと値の組。どのホストへ送るかは、まだ決まっていない。"""

    key: str
    value: TelemetryValue


@dataclass(frozen=True)
class ZabbixValue:
    """Zabbixへ送る1件の値。

    clock と ns は、中間サーバーでの受信時刻(6.4.4、CV-12)。
    Zabbix sender プロトコルの clock(秒)と ns(ナノ秒の端数)にそのまま使う。
    """

    host: str
    key: str
    value: TelemetryValue
    clock: int
    ns: int


def stamp_values(host: str, key_values: Iterable[KeyValue], received_at_ns: int) -> list[ZabbixValue]:
    """キーと値の組に、送信先のホスト名と受信時刻を付ける(TX-04、CV-12)。

    received_at_ns は、中間サーバーで受信した時刻(time.time_ns() の値)。
    Nexus側の時刻(msg_timestamp など)は使わない(9.1.2.4)。
    """
    clock, ns = divmod(received_at_ns, _NS_PER_SEC)
    return [ZabbixValue(host=host, key=kv.key, value=kv.value, clock=clock, ns=ns) for kv in key_values]
