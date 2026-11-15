#!/usr/bin/env python3
"""Zabbixテンプレートファイル(YAML形式、Zabbix 7.0 LTS用)を作る(要件定義書 9.2.6、DL-03、DL-04)。

テンプレートのキーは、collector が使うものと同じ定義(collector/cisco_nexus/commands.py、
common/constants.py)から作る。手で書き写さないため、collector が送るキーとずれない。

使い方(テンプレートを作り直すとき。普段は実行しなくてよい):
    python tools/zabbix_templates.py
    → zabbix_templates/ の2つのファイルを書き出す
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from collector.cisco_nexus.commands import COMMANDS, COUNTERS_BRIEF, PTP_BRIEF, PTP_CLOCK, PTP_PARENT  # noqa: E402
from collector.cisco_nexus.values import ValueKind  # noqa: E402
from common.constants import (  # noqa: E402
    DROP_INVALID_DATA,
    DROP_QUEUE_FULL,
    DROP_REASONS,
    DROP_UNKNOWN_COMMAND,
    DROP_UNKNOWN_SOURCE,
    DROP_ZABBIX_UNREACHABLE,
    KEY_COLLECTOR_DEVICES_STALE,
    KEY_COLLECTOR_DEVICES_TOTAL,
    KEY_COLLECTOR_DROPPED_FORMAT,
    KEY_COLLECTOR_QUEUE_SIZE,
    KEY_COLLECTOR_RECEIVED,
    KEY_COLLECTOR_SENT_FAILED,
    KEY_COLLECTOR_SENT_OK,
    KEY_COLLECTOR_SKIPPED,
    KEY_COMMAND_STATUS_FORMAT,
    KEY_DEVICE_AGE,
    KEY_DEVICE_STATUS,
    LLD_MACRO_IFNAME,
    LLD_MACRO_IFNUM,
)

ZABBIX_EXPORT_VERSION = "7.0"
TEMPLATE_GROUP = "Templates/Telemetry"

# テンプレートの名前。template は技術的な名前(半角英数字など)、name は画面に出る名前
NWSW_TEMPLATE = "Telemetry NWSW"
NWSW_TEMPLATE_NAME = "Telemetry NWSW用テンプレート"
COLLECTOR_TEMPLATE = "Telemetry collector"
COLLECTOR_TEMPLATE_NAME = "Telemetry 中間サーバー用テンプレート"

OUTPUT_DIR = REPO_ROOT / "zabbix_templates"
NWSW_FILE = OUTPUT_DIR / "telemetry_nwsw.yaml"
COLLECTOR_FILE = OUTPUT_DIR / "telemetry_collector.yaml"

# collectorが停止したと判断するまでの時間(9.2.6.3)
COLLECTOR_NODATA_PERIOD = "3m"

# トリガーの重要度(Zabbixの区分)(決定事項 D-028)
PRIORITY_DEVICE_STALE = "HIGH"
PRIORITY_COMMAND_STALE = "AVERAGE"
PRIORITY_COLLECTOR_STOPPED = "HIGH"

# 機器全体の途絶のトリガー。コマンドごとの途絶のトリガーは、これに依存させる(決定事項 D-028)
DEVICE_STALE_TRIGGER_NAME = "{HOST.NAME}: Telemetryの受信が途絶しました"
DEVICE_STALE_TRIGGER_EXPRESSION = f"last(/{NWSW_TEMPLATE}/{KEY_DEVICE_STATUS})=0"

# 受信状態の値のマッピング(1=正常、0=途絶)(決定事項 D-028)
STATUS_VALUEMAP_NAME = "Telemetry 受信状態"
STATUS_VALUEMAP: tuple[tuple[str, str], ...] = (("1", "正常"), ("0", "途絶"))

# 値の種類と、Zabbixのアイテムの値の型・単位の対応(9.2.3)
VALUE_KIND_TYPES: dict[ValueKind, tuple[str, str]] = {
    ValueKind.TEXT: ("CHAR", ""),
    ValueKind.NUMBER: ("FLOAT", "ns"),
    ValueKind.BANDWIDTH: ("UNSIGNED", "bps"),
}

# 項目の説明(Zabbixの画面に出る)
FIELD_DESCRIPTIONS: dict[str, str] = {
    "show.ptp.clock.clock-state": "PTPのロック状態(show ptp clock の clock-state)",
    "show.ptp.clock.offset-from-master": "マスターとの時刻差(show ptp clock の offset-from-master、単位はナノ秒)",
    "show.ptp.parent.gm-id": "グランドマスターのID(show ptp parent の gm-id)",
    "show.ptp.brief.state": "PTPポート状態(show ptp brief の state)",
    "show.interface.counters.brief.inrate1": "入力帯域(show interface counters brief の eth_inrate1 を bps に換算。30秒間の平均値)",
    "show.interface.counters.brief.outrate1": "出力帯域(show interface counters brief の eth_outrate1 を bps に換算。30秒間の平均値)",
}

# 「telemetry」ホストへ送る件数のアイテムの名前(単位を明記する。9.2.5.3、決定事項 D-016)
DROP_LABELS: dict[str, str] = {
    DROP_UNKNOWN_SOURCE: "破棄件数:未登録の送信元(パケット数)",
    DROP_INVALID_DATA: "破棄件数:壊れたデータ(パケット数)",
    DROP_UNKNOWN_COMMAND: "破棄件数:4つ以外のコマンド(パケット数)",
    DROP_QUEUE_FULL: "破棄件数:送信待ちキューが満杯(値の数)",
    DROP_ZABBIX_UNREACHABLE: "破棄件数:Zabbixに接続できない(値の数)",
}


def make_uuid(*parts: str) -> str:
    """名前から、いつも同じになる識別番号(UUID第4版の形、32桁の16進数)を作る。"""
    digest = list(hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32])
    digest[12] = "4"  # 第4版であることを示す桁
    digest[16] = "89ab"[int(digest[16], 16) % 4]  # 種類を示す桁
    return "".join(digest)


def _trapper_item(template: str, key: str, name: str, value_type: str, units: str, description: str,
                  component: str) -> dict[str, Any]:  # fmt: skip
    item: dict[str, Any] = {
        "uuid": make_uuid(template, "item", key),
        "name": name,
        "type": "TRAP",
        "key": key,
        "delay": "0",
    }
    if value_type in ("CHAR", "TEXT"):
        item["trends"] = "0"
    if value_type != "UNSIGNED":
        item["value_type"] = value_type
    if units:
        item["units"] = units
    item["description"] = description
    item["tags"] = [{"tag": "component", "value": component}]
    return item


def _trigger(
    template: str, key: str, expression: str, name: str, priority: str, description: str, *, manual_close: bool = False
) -> dict[str, Any]:
    trigger = {
        "uuid": make_uuid(template, "trigger", key, expression),
        "expression": expression,
        "name": name,
        "priority": priority,
        "description": description,
    }
    if manual_close:
        # Zabbix の画面で、障害を手動で閉じられるようにする(9.2.6.3.4、決定事項 D-034)
        trigger["manual_close"] = "YES"
    return trigger


# ======================================================================
# NWSW用テンプレート(DL-03)
# ======================================================================


def nwsw_template() -> dict[str, Any]:
    """NWSW用テンプレート:機器単位のアイテム3つ、LLDルール2つ、自己監視のアイテム6つ、トリガー5つ(9.2.6)。

    コマンドごとの途絶のトリガー4つは、機器全体の途絶のトリガーに依存させる。
    受信状態のアイテム5つには、1=正常、0=途絶 の値のマッピングを付ける(決定事項 D-028)。
    """
    t = NWSW_TEMPLATE
    items: list[dict[str, Any]] = []

    # 機器単位のアイテム(9.2.4.4)
    for spec in (PTP_CLOCK, PTP_PARENT):
        for field in spec.fields:
            key = f"{spec.key_prefix}.{field.key_item}"
            value_type, units = VALUE_KIND_TYPES[field.kind]
            items.append(
                _trapper_item(t, key, f"{spec.name}: {field.key_item}", value_type, units, FIELD_DESCRIPTIONS[key], "ptp")
            )

    # 自己監視のアイテム(9.2.5.1)とトリガー(9.2.6.3)
    status = _trapper_item(
        t, KEY_DEVICE_STATUS, "telemetry: 受信状態(1=正常、0=途絶)", "UNSIGNED", "",
        "その機器からのTelemetryの受信状態。1=正常、0=途絶。中間サーバーが一定間隔で送る。受信待ちの間は送られない。",
        "telemetry",
    )  # fmt: skip
    status["valuemap"] = {"name": STATUS_VALUEMAP_NAME}
    status["triggers"] = [
        _trigger(
            t, KEY_DEVICE_STATUS, DEVICE_STALE_TRIGGER_EXPRESSION, DEVICE_STALE_TRIGGER_NAME, PRIORITY_DEVICE_STALE,
            "中間サーバーが、この機器からのTelemetryを途絶判定時間(既定値5分)受信していません。"
            "Nexusの送信設定と、管理ネットワークの経路を確かめてください。"
            "機器の登録を削除して値が届かなくなった場合は、この障害を手動で閉じてください。",
            manual_close=True,
        )
    ]  # fmt: skip
    items.append(status)
    items.append(
        _trapper_item(
            t, KEY_DEVICE_AGE, "telemetry: 最後の受信からの経過秒数", "UNSIGNED", "s",
            "最後にTelemetryを受信してからの経過秒数。1度も受信しないまま途絶と判定した場合は、"
            "機器の登録、または中間サーバーのcollectorの起動からの経過秒数。",
            "telemetry",
        )
    )  # fmt: skip
    for command in COMMANDS:
        key = KEY_COMMAND_STATUS_FORMAT.format(command=command)
        item = _trapper_item(
            t, key, f"telemetry: {command} の受信状態(1=正常、0=途絶)", "UNSIGNED", "",
            f"{command} のTelemetryの受信状態。1=正常、0=途絶。中間サーバーの機器登録でチェックしたコマンドだけ送られる。",
            "telemetry",
        )  # fmt: skip
        item["valuemap"] = {"name": STATUS_VALUEMAP_NAME}
        trigger = _trigger(
            t, key, f"last(/{t}/{key})=0",
            f"{{HOST.NAME}}: {command} のTelemetryの受信が途絶しました", PRIORITY_COMMAND_STALE,
            f"中間サーバーが、この機器からの {command} のデータを、途絶判定時間(既定値5分)受信していません。"
            "中間サーバーの機器登録でチェックを外したコマンドは、値が送られないため、障害になりません。"
            "機器全体の受信が途絶している間は、この障害は出ません(機器全体の途絶のトリガーに依存させている)。"
            "チェックを外したあとも障害が残った場合は、手動で閉じてください。",
            manual_close=True,
        )  # fmt: skip
        # 機器全体が途絶したときに、障害が5件同時に出ないよう、同じホストの機器全体の途絶のトリガーに依存させる
        trigger["dependencies"] = [{"name": DEVICE_STALE_TRIGGER_NAME, "expression": DEVICE_STALE_TRIGGER_EXPRESSION}]
        item["triggers"] = [trigger]
        items.append(item)

    # LLDルールとアイテムのひな形(9.2.4.1)
    discovery_rules = []
    for spec, label, component in (
        (COUNTERS_BRIEF, "帯域監視のインターフェース", "bandwidth"),
        (PTP_BRIEF, "PTPポート監視のインターフェース", "ptp"),
    ):
        prototypes = []
        for field in spec.fields:
            prefix = f"{spec.key_prefix}.{field.key_item}"
            key = f"{prefix}[{LLD_MACRO_IFNUM}]"
            value_type, units = VALUE_KIND_TYPES[field.kind]
            prototype = _trapper_item(
                t, key, f"{spec.name}: {LLD_MACRO_IFNAME} {field.key_item}", value_type, units,
                f"{FIELD_DESCRIPTIONS[prefix]}。インターフェース番号 {LLD_MACRO_IFNUM}。",
                component,
            )  # fmt: skip
            prototype["tags"].append({"tag": "interface", "value": LLD_MACRO_IFNAME})
            prototypes.append(prototype)
        discovery_rules.append(
            {
                "uuid": make_uuid(t, "discovery", spec.lld_key),
                "name": f"{spec.name}: {label}",
                "type": "TRAP",
                "key": spec.lld_key,
                "delay": "0",
                "description": f"中間サーバーのWebGUIで {spec.name} 用に登録したインターフェースの一覧。中間サーバーが送る。",
                "item_prototypes": prototypes,
            }
        )

    return {
        "uuid": make_uuid(t),
        "template": t,
        "name": NWSW_TEMPLATE_NAME,
        "description": (
            "Telemetry 中間サーバーから値を受け取る、NWSW(Cisco Nexus)用のテンプレート。各NWSWのホストに適用する。"
            "ホスト名は、Nexusのホスト名と、中間サーバーに登録したホスト名と一致させる。"
        ),
        "groups": [{"name": TEMPLATE_GROUP}],
        "items": items,
        "discovery_rules": discovery_rules,
        "valuemaps": [
            {
                "uuid": make_uuid(t, "valuemap", STATUS_VALUEMAP_NAME),
                "name": STATUS_VALUEMAP_NAME,
                "mappings": [{"value": value, "newvalue": text} for value, text in STATUS_VALUEMAP],
            }
        ],
    }


# ======================================================================
# 中間サーバー用テンプレート(DL-04)
# ======================================================================


def collector_template() -> dict[str, Any]:
    """中間サーバー用テンプレート:自己監視のアイテム(9.2.5.2)と、collectorの停止を検知するトリガー(9.2.6.3)。"""
    t = COLLECTOR_TEMPLATE
    definitions: list[tuple[str, str, str]] = [
        (KEY_COLLECTOR_RECEIVED, "受信件数(パケット数)", "前回の送信から今回の送信までに受信したUDPパケットの数(破棄したものも含む)。"),
        *(
            (KEY_COLLECTOR_DROPPED_FORMAT.format(reason=reason), DROP_LABELS[reason], f"前回の送信から今回の送信までの{DROP_LABELS[reason]}。")
            for reason in DROP_REASONS
        ),
        (KEY_COLLECTOR_SKIPPED, "対象外の件数(パケット数)", "機器登録でチェックを外したコマンドのデータの数。エラーではない。"),
        (
            KEY_COLLECTOR_SENT_OK,
            "Zabbix送信の成功件数(値の数。LLD用データと自己監視値を含む)",
            "Zabbixが受け付けた値の数。監視データに加えて、LLD用データと自己監視値の送信も含む。",
        ),
        (
            KEY_COLLECTOR_SENT_FAILED,
            "Zabbix送信の失敗件数(値の数。LLD用データと自己監視値を含む)",
            "Zabbixが受け付けなかった値の数(Zabbix側にアイテムがない、など)。LLD用データと自己監視値の送信も含む。",
        ),
        (KEY_COLLECTOR_QUEUE_SIZE, "送信待ちキューの滞留量(値の数)", "送信待ちキューに残っている値の数。"),
        (KEY_COLLECTOR_DEVICES_TOTAL, "登録機器数", "中間サーバーに登録されている機器の台数。"),
        (KEY_COLLECTOR_DEVICES_STALE, "途絶中の機器数", "受信が途絶している機器の台数。"),
    ]
    items = []
    for key, label, description in definitions:
        items.append(_trapper_item(t, key, f"telemetry collector: {label}", "UNSIGNED", "", description, "telemetry"))
    items[0]["triggers"] = [
        _trigger(
            t, KEY_COLLECTOR_RECEIVED, f"nodata(/{t}/{KEY_COLLECTOR_RECEIVED},{COLLECTOR_NODATA_PERIOD})=1",
            "Telemetry 中間サーバーのcollectorが停止しています", PRIORITY_COLLECTOR_STOPPED,
            "中間サーバーから自己監視値が3分間届いていません。collectorの停止、中間サーバーの停止、"
            "または中間サーバーとZabbixの間の通信の異常が考えられます。",
        )
    ]  # fmt: skip
    return {
        "uuid": make_uuid(t),
        "template": t,
        "name": COLLECTOR_TEMPLATE_NAME,
        "description": "Telemetry 中間サーバー自身の状態を受け取るテンプレート。ホスト名「telemetry」のホストに適用する。",
        "groups": [{"name": TEMPLATE_GROUP}],
        "items": items,
    }


def export_document(template: dict[str, Any]) -> dict[str, Any]:
    return {
        "zabbix_export": {
            "version": ZABBIX_EXPORT_VERSION,
            "template_groups": [{"uuid": make_uuid("group", TEMPLATE_GROUP), "name": TEMPLATE_GROUP}],
            "templates": [template],
        }
    }


# ======================================================================
# YAMLの書き出し(この形だけを扱う、小さな書き出し機能)
# ======================================================================


def _scalar(value: Any) -> str:
    if not isinstance(value, str):
        raise TypeError(f"文字列以外の値は書き出せません:{value!r}")
    if "\n" in value or "\r" in value:
        raise ValueError("改行を含む値は書き出せません")
    return "'" + value.replace("'", "''") + "'"


def _emit(value: Any, indent: int, lines: list[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                lines.append(f"{pad}{key}:")
                _emit(child, indent + 2, lines)
            else:
                lines.append(f"{pad}{key}: {_scalar(child)}")
    elif isinstance(value, list):
        if not value:
            raise ValueError("空の一覧は書き出せません")
        for element in value:
            if not isinstance(element, dict):
                lines.append(f"{pad}- {_scalar(element)}")
                continue
            child_lines: list[str] = []
            _emit(element, indent + 2, child_lines)
            first = child_lines[0]
            lines.append(f"{pad}- {first[indent + 2:]}")
            lines.extend(child_lines[1:])
    else:
        raise TypeError(f"書き出せない値です:{value!r}")


def to_yaml(document: dict[str, Any]) -> str:
    lines: list[str] = []
    _emit(document, 0, lines)
    return "\n".join(lines) + "\n"


def render_all() -> dict[Path, str]:
    """作るファイルと、その中身。"""
    return {
        NWSW_FILE: to_yaml(export_document(nwsw_template())),
        COLLECTOR_FILE: to_yaml(export_document(collector_template())),
    }


def main() -> int:
    for path, content in render_all().items():
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"書き出しました:{path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
