"""インターフェース番号の扱い(6.3.5、9.1.7.1、9.1.7.2)。"""

import re
from dataclasses import dataclass

from common.constants import ETHERNET_NAME_PREFIX, MAX_DEVICE_IFNAME_LENGTH


@dataclass(frozen=True, order=True)
class InterfaceId:
    """「スロット番号/ポート番号」の2段のインターフェース番号。"""

    slot: int
    port: int

    @property
    def ifnum(self) -> str:
        """登録・キー・LLDで使う番号の表記(例:1/1)。"""
        return f"{self.slot}/{self.port}"

    @property
    def ifname(self) -> str:
        """機器での名前(例:Ethernet1/1)。保存せず、番号から組み立てる(8.3.2.3)。"""
        return f"{ETHERNET_NAME_PREFIX}{self.slot}/{self.port}"


# 「Eth」または「Ethernet」に続く「スロット番号/ポート番号」だけに一致させる。
# 3段の番号(ブレークアウトポート)や mgmt0、port-channel などは一致しない(9.1.7.2)。
_DEVICE_IFNAME_PATTERN = re.compile(r"(?:Ethernet|Eth)([0-9]+)/([0-9]+)", re.ASCII)


def parse_device_ifname(name: object) -> InterfaceId | None:
    """機器から届いたインターフェース名を、インターフェース番号に読み替える。

    「Eth1/1」(show ptp brief)と「Ethernet1/1」(show interface counters brief)の
    どちらの表記でも、同じ番号 1/1 として扱う(9.1.7.1)。
    形式に当てはまらない名前は None を返す。エラーとは扱わない(9.1.7.2)。
    """
    if not isinstance(name, str) or len(name) > MAX_DEVICE_IFNAME_LENGTH:
        return None
    match = _DEVICE_IFNAME_PATTERN.fullmatch(name.strip())
    if match is None:
        return None
    return InterfaceId(slot=int(match.group(1)), port=int(match.group(2)))
