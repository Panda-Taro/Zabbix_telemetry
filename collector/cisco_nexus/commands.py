"""showコマンドごとの、値の取り出し方とZabbixのキーの定義(11.4.6)。

新しいshowコマンドに対応するときは、このファイルの「コマンドの定義」に1つ追加し、
COMMANDS に加えるだけで済むようにしている。
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from collector.cisco_nexus.values import ValueKind, convert_value
from collector.models import KeyValue
from common.constants import (
    CMD_COUNTERS_BRIEF,
    CMD_PTP_BRIEF,
    CMD_PTP_CLOCK,
    CMD_PTP_PARENT,
    LLD_KEY_SUFFIX,
    PURPOSE_COUNTERS,
    PURPOSE_PTP,
)
from common.interfaces import InterfaceId, parse_device_ifname


@dataclass(frozen=True)
class FieldSpec:
    """取り出す1項目の定義。"""

    json_name: str
    """dataの中の項目名(例:eth_inrate1)。"""
    key_item: str
    """Zabbixのキーの末尾に使う名前(例:inrate1)。"""
    kind: ValueKind
    """値の種類。"""


@dataclass(frozen=True)
class Extraction:
    """1回分のデータから取り出した結果。"""

    values: tuple[KeyValue, ...]
    """Zabbixへ送るキーと値。"""
    missing_interfaces: tuple[InterfaceId, ...]
    """登録したが、届いたデータの中に存在しなかったインターフェース番号(不整合、6.3.8)。"""


class CommandSpec(Protocol):
    """showコマンド1つ分の部品が持つもの。"""

    @property
    def name(self) -> str:
        """showコマンド名(encoding_path と完全一致で照合する)。"""
        ...

    @property
    def interface_purpose(self) -> str | None:
        """監視対象インターフェースの用途。機器単位のコマンドは None。"""
        ...

    @property
    def lld_key(self) -> str | None:
        """LLDルールのキー。機器単位のコマンドは None。"""
        ...

    def item_keys(self, interface: InterfaceId | None = None) -> tuple[str, ...]:
        """このコマンドで作られるZabbixのキーの一覧。"""
        ...

    def extract(self, data: Mapping[str, Any], targets: Sequence[InterfaceId] = ()) -> Extraction:
        """dataから値を取り出す。どんな形のdataでも例外を出さない。"""
        ...


def build_key(key_prefix: str, key_item: str, interface: InterfaceId | None = None) -> str:
    """Zabbixのキーを組み立てる(9.2.2)。キーにホスト名は含めない(9.2.2.2)。

    例) show.ptp.clock.offset-from-master
        show.interface.counters.brief.inrate1[1/3]
    """
    key = f"{key_prefix}.{key_item}"
    if interface is not None:
        key += f"[{interface.ifnum}]"
    return key


def _extract_fields(
    source: Mapping[str, Any],
    key_prefix: str,
    fields: Sequence[FieldSpec],
    interface: InterfaceId | None = None,
) -> list[KeyValue]:
    """1つの行(または data の直下)から、定義した項目を取り出す。送らない値は含めない。"""
    result: list[KeyValue] = []
    for field in fields:
        value = convert_value(source.get(field.json_name), field.kind)
        if value is not None:
            result.append(KeyValue(build_key(key_prefix, field.key_item, interface), value))
    return result


def _get_rows(data: Mapping[str, Any], table_name: str, row_name: str) -> list[Any]:
    """TABLE_xxx → ROW_xxx の行の一覧を取り出す。

    行が1件だけの場合は、配列ではなく単独の { } で届くことがあるため、
    どちらの形でも一覧として返す(9.1.7.3)。形が想定と違う場合は空の一覧を返す。
    """
    table = data.get(table_name)
    if not isinstance(table, Mapping):
        return []
    rows = table.get(row_name)
    if isinstance(rows, Mapping):
        return [rows]
    if isinstance(rows, list):
        return rows
    return []


@dataclass(frozen=True)
class DeviceCommandSpec:
    """機器単位の値を、dataの直下から取り出すコマンド(show ptp clock、show ptp parent)。"""

    name: str
    key_prefix: str
    fields: tuple[FieldSpec, ...]

    @property
    def interface_purpose(self) -> str | None:
        return None

    @property
    def lld_key(self) -> str | None:
        return None

    def item_keys(self, interface: InterfaceId | None = None) -> tuple[str, ...]:
        return tuple(build_key(self.key_prefix, f.key_item) for f in self.fields)

    def extract(self, data: Mapping[str, Any], targets: Sequence[InterfaceId] = ()) -> Extraction:
        return Extraction(values=tuple(_extract_fields(data, self.key_prefix, self.fields)), missing_interfaces=())


@dataclass(frozen=True)
class InterfaceCommandSpec:
    """インターフェースごとの値を、TABLE_xxx → ROW_xxx から取り出すコマンド。"""

    name: str
    key_prefix: str
    purpose: str
    table_name: str
    row_name: str
    ifname_field: str
    """行の中で、インターフェース名が入っている項目名。"""
    fields: tuple[FieldSpec, ...]

    @property
    def interface_purpose(self) -> str | None:
        return self.purpose

    @property
    def lld_key(self) -> str | None:
        return f"{self.key_prefix}.{LLD_KEY_SUFFIX}"

    def item_keys(self, interface: InterfaceId | None = None) -> tuple[str, ...]:
        return tuple(build_key(self.key_prefix, f.key_item, interface) for f in self.fields)

    def extract(self, data: Mapping[str, Any], targets: Sequence[InterfaceId] = ()) -> Extraction:
        # 届いた行を、インターフェース番号ごとに整理する。
        # 形式に当てはまらない名前の行(mgmt0、port-channel、3段の番号など)は無視する(9.1.7.2)。
        rows_by_interface: dict[InterfaceId, Mapping[str, Any]] = {}
        for row in _get_rows(data, self.table_name, self.row_name):
            if not isinstance(row, Mapping):
                continue
            interface = parse_device_ifname(row.get(self.ifname_field))
            if interface is None:
                continue
            rows_by_interface.setdefault(interface, row)

        # 登録したインターフェース番号のものだけを取り出す
        values: list[KeyValue] = []
        missing: list[InterfaceId] = []
        for interface in sorted(set(targets)):
            row = rows_by_interface.get(interface)
            if row is None:
                missing.append(interface)
                continue
            values.extend(_extract_fields(row, self.key_prefix, self.fields, interface))
        return Extraction(values=tuple(values), missing_interfaces=tuple(missing))


# ======================================================================
# コマンドの定義(6.3.1、9.1.3〜9.1.6、9.2.3)
# ======================================================================

PTP_CLOCK = DeviceCommandSpec(
    name=CMD_PTP_CLOCK,
    key_prefix="show.ptp.clock",
    fields=(
        FieldSpec(json_name="clock-state", key_item="clock-state", kind=ValueKind.TEXT),
        FieldSpec(json_name="offset-from-master", key_item="offset-from-master", kind=ValueKind.NUMBER),
    ),
)

# dataの直下には clock-id(親クロックのID)もあるが、取り出すのは gm-id だけ(9.1.4.2)
PTP_PARENT = DeviceCommandSpec(
    name=CMD_PTP_PARENT,
    key_prefix="show.ptp.parent",
    fields=(FieldSpec(json_name="gm-id", key_item="gm-id", kind=ValueKind.TEXT),),
)

PTP_BRIEF = InterfaceCommandSpec(
    name=CMD_PTP_BRIEF,
    key_prefix="show.ptp.brief",
    purpose=PURPOSE_PTP,
    table_name="TABLE_ptp",
    row_name="ROW_ptp",
    ifname_field="ptp-ifindex",
    fields=(FieldSpec(json_name="state", key_item="state", kind=ValueKind.TEXT),),
)

# eth_inrate2 など5分間平均の項目やフレーム数は使わない(9.1.6.2)
COUNTERS_BRIEF = InterfaceCommandSpec(
    name=CMD_COUNTERS_BRIEF,
    key_prefix="show.interface.counters.brief",
    purpose=PURPOSE_COUNTERS,
    table_name="TABLE_interface",
    row_name="ROW_interface",
    ifname_field="interface",
    fields=(
        FieldSpec(json_name="eth_inrate1", key_item="inrate1", kind=ValueKind.BANDWIDTH),
        FieldSpec(json_name="eth_outrate1", key_item="outrate1", kind=ValueKind.BANDWIDTH),
    ),
)

# 対応するコマンドの一覧。キーはshowコマンド名
COMMANDS: Mapping[str, CommandSpec] = MappingProxyType(
    {spec.name: spec for spec in (COUNTERS_BRIEF, PTP_BRIEF, PTP_CLOCK, PTP_PARENT)}
)


def identify_command(encoding_path: str) -> CommandSpec | None:
    """encoding_path から、showコマンドの部品を探す(6.4.2、9.1.2.2、RX-06)。

    前後の空白を除いたうえで、4つのコマンド名と完全一致で判別する。
    部分一致は行わない。4つ以外のコマンドは None を返す(破棄の理由 unknown_command)。
    """
    return COMMANDS.get(encoding_path.strip())
