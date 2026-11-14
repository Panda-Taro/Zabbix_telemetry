"""LLD用データの組み立て(6.5、9.2.4、LD-02、LD-03)。"""

import json
from collections.abc import Iterable, Mapping, Sequence

from collector.cisco_nexus.commands import COMMANDS
from collector.models import KeyValue, ZabbixValue, stamp_values
from common.constants import LLD_MACRO_IFNAME, LLD_MACRO_IFNUM
from common.interfaces import InterfaceId


def build_lld_json(interfaces: Iterable[InterfaceId]) -> str:
    """インターフェース一覧を、LLD用データのJSONにする(9.2.4.2)。

    例) [{"{#IFNUM}": "1/1", "{#IFNAME}": "Ethernet1/1"}]
    0件の場合は空の一覧 [] になる(9.2.4.3)。
    """
    rows = [
        {LLD_MACRO_IFNUM: interface.ifnum, LLD_MACRO_IFNAME: interface.ifname}
        for interface in sorted(set(interfaces))
    ]
    return json.dumps(rows, ensure_ascii=False)


def build_lld_values(
    host: str,
    enabled_targets: Mapping[str, Sequence[InterfaceId]],
    sent_at_ns: int,
) -> list[ZabbixValue]:
    """1台分のLLD用データを、LLDルールごとに組み立てる(6.5.2、LD-01〜LD-03)。

    enabled_targets には、機器登録でチェックしたコマンドだけを
    「showコマンド名 → 登録したインターフェース番号」の形で渡す。
    チェックを外したコマンド(enabled_targets にないもの)と、
    登録したインターフェースが0件のコマンドは、空の一覧を送る(9.2.4.3)。
    """
    key_values: list[KeyValue] = []
    for spec in COMMANDS.values():
        if spec.lld_key is None:
            continue
        targets = enabled_targets.get(spec.name, ())
        key_values.append(KeyValue(key=spec.lld_key, value=build_lld_json(targets)))
    return stamp_values(host, key_values, sent_at_ns)
