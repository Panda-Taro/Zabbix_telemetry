"""Zabbixテンプレートの自動テスト(段階7、DL-03、DL-04、要件定義書 9.2.3〜9.2.6)。

collector が実際に送るキーと、テンプレートのキーが一致していることを確かめる。
"""

import asyncio
import re
from typing import Any

import pytest

from collector.cisco_nexus.commands import COMMANDS, COUNTERS_BRIEF, PTP_BRIEF, PTP_CLOCK, PTP_PARENT, build_key
from collector.cisco_nexus.lld import build_lld_values
from collector.config import DeviceConfig
from collector.device_state import DeviceRuntime
from collector.monitor import collector_monitor_values, device_monitor_values
from collector.stats import COUNTER_NAMES
from common.constants import LLD_MACRO_IFNUM
from common.interfaces import InterfaceId
from tests.collector_support import NS, START_NS, device, handler_of, prepare_db, started_app
from tests.conftest import fixture_bytes
from tools import zabbix_templates as zt


# ======================================================================
# YAMLの読み戻し(テンプレートの書き出しの形だけを扱う)
# ======================================================================


def _unquote(text: str) -> str:
    assert text.startswith("'") and text.endswith("'"), f"値が引用符で囲まれていません:{text}"
    inner = text[1:-1]
    assert "'" not in inner.replace("''", ""), f"引用符の書き方が正しくありません:{text}"
    return inner.replace("''", "'")


def parse_yaml(text: str) -> Any:
    """テンプレートの書き出しの形(辞書、辞書の一覧、引用符付きの文字列)を読み戻す。"""
    lines = [line for line in text.splitlines() if line.strip()]
    position = 0

    def indent_of(line: str) -> int:
        return len(line) - len(line.lstrip(" "))

    def parse_block(indent: int) -> Any:
        nonlocal position
        if lines[position].lstrip().startswith("- "):
            result_list: list[Any] = []
            while position < len(lines) and indent_of(lines[position]) == indent and lines[position].lstrip().startswith("- "):
                # 「- 」の後ろを、2つ深い字下げの辞書の1行目として扱う
                lines[position] = " " * (indent + 2) + lines[position].lstrip()[2:]
                result_list.append(parse_block(indent + 2))
            return result_list
        result: dict[str, Any] = {}
        while position < len(lines) and indent_of(lines[position]) == indent and not lines[position].lstrip().startswith("- "):
            key, _, rest = lines[position].strip().partition(":")
            position += 1
            assert key not in result, f"同じ項目名が2回あります:{key}"
            if rest.strip():
                result[key] = _unquote(rest.strip())
            else:
                result[key] = parse_block(indent_of(lines[position]))
        return result

    document = parse_block(0)
    assert position == len(lines), "読み残した行があります"
    return document


@pytest.fixture(scope="module")
def nwsw() -> dict[str, Any]:
    return parse_yaml(zt.NWSW_FILE.read_text(encoding="utf-8"))["zabbix_export"]


@pytest.fixture(scope="module")
def collector_tpl() -> dict[str, Any]:
    return parse_yaml(zt.COLLECTOR_FILE.read_text(encoding="utf-8"))["zabbix_export"]


def items_of(export: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["key"]: item for item in export["templates"][0]["items"]}


def prototypes_of(export: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        prototype["key"]: prototype
        for rule in export["templates"][0].get("discovery_rules", [])
        for prototype in rule["item_prototypes"]
    }


def triggers_of(export: dict[str, Any]) -> list[dict[str, Any]]:
    return [trigger for item in export["templates"][0]["items"] for trigger in item.get("triggers", [])]


# ======================================================================
# ファイルの形
# ======================================================================


def test_files_are_up_to_date():
    """リポジトリのテンプレートファイルが、collector の定義から作ったものと同じである(作り直し忘れがない)。"""
    for path, content in zt.render_all().items():
        assert path.read_text(encoding="utf-8") == content, f"{path.name} を作り直してください(python tools/zabbix_templates.py)"


def test_yaml_round_trip():
    """書き出したYAMLを読み戻すと、元の内容と同じになる。"""
    assert parse_yaml(zt.to_yaml(zt.export_document(zt.nwsw_template()))) == zt.export_document(zt.nwsw_template())
    assert parse_yaml(zt.to_yaml(zt.export_document(zt.collector_template()))) == zt.export_document(zt.collector_template())


def test_yaml_quoting():
    text = zt.to_yaml({"a": "it's: {#IFNAME} [1/1] #x", "b": [{"c": "d"}, {"e": "f", "g": "h"}]})
    assert parse_yaml(text) == {"a": "it's: {#IFNAME} [1/1] #x", "b": [{"c": "d"}, {"e": "f", "g": "h"}]}
    with pytest.raises(ValueError):
        zt.to_yaml({"a": "改行\nあり"})


def test_zabbix_7_0_export_structure(nwsw, collector_tpl):
    """Zabbix 7.0 のテンプレートの書き出し形式に沿っている。"""
    for export in (nwsw, collector_tpl):
        assert export["version"] == "7.0"
        assert export["template_groups"] == [{"uuid": zt.make_uuid("group", zt.TEMPLATE_GROUP), "name": "Templates/Telemetry"}]
        template = export["templates"][0]
        assert template["groups"] == [{"name": "Templates/Telemetry"}]
        # 技術的な名前は、Zabbixで使える文字だけ
        assert re.fullmatch(r"[A-Za-z0-9 ._-]+", template["template"])
        for item in [*template["items"], *prototypes_of(export).values()]:
            assert item["type"] == "TRAP"
            assert item["delay"] == "0"
            assert item.get("value_type", "UNSIGNED") in ("UNSIGNED", "FLOAT", "CHAR")


def test_uuids_are_unique_v4(nwsw, collector_tpl):
    """識別番号(uuid)は、32桁の16進数(UUID第4版の形)で、重複しない。"""
    found: list[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            if "uuid" in node:
                found.append(node["uuid"])
            for child in node.values():
                collect(child)
        elif isinstance(node, list):
            for child in node:
                collect(child)

    collect(nwsw["templates"])
    collect(collector_tpl["templates"])
    assert len(found) == len(set(found))
    for uuid in found:
        assert re.fullmatch(r"[0-9a-f]{12}4[0-9a-f]{3}[89ab][0-9a-f]{15}", uuid), uuid


# ======================================================================
# キー、値の型、単位(9.2.3〜9.2.5)
# ======================================================================


def test_nwsw_contents(nwsw):
    """NWSW用:機器単位のアイテム3つ、自己監視のアイテム6つ、LLDルール2つとアイテムのひな形3つ(9.2.6.1)。"""
    items = items_of(nwsw)
    assert set(items) == {
        "show.ptp.clock.clock-state",
        "show.ptp.clock.offset-from-master",
        "show.ptp.parent.gm-id",
        "telemetry.status",
        "telemetry.age",
        "telemetry.status[show interface counters brief]",
        "telemetry.status[show ptp brief]",
        "telemetry.status[show ptp clock]",
        "telemetry.status[show ptp parent]",
    }
    rules = {rule["key"]: rule for rule in nwsw["templates"][0]["discovery_rules"]}
    assert set(rules) == {"show.interface.counters.brief.discovery", "show.ptp.brief.discovery"}
    assert {p["key"] for p in rules["show.interface.counters.brief.discovery"]["item_prototypes"]} == {
        "show.interface.counters.brief.inrate1[{#IFNUM}]",
        "show.interface.counters.brief.outrate1[{#IFNUM}]",
    }
    assert {p["key"] for p in rules["show.ptp.brief.discovery"]["item_prototypes"]} == {"show.ptp.brief.state[{#IFNUM}]"}
    for rule in rules.values():
        assert rule["type"] == "TRAP"


@pytest.mark.parametrize(
    ("key", "value_type", "units"),
    [
        ("show.ptp.clock.clock-state", "CHAR", None),
        ("show.ptp.clock.offset-from-master", "FLOAT", "ns"),
        ("show.ptp.parent.gm-id", "CHAR", None),
        ("show.ptp.brief.state[{#IFNUM}]", "CHAR", None),
        ("show.interface.counters.brief.inrate1[{#IFNUM}]", "UNSIGNED", "bps"),
        ("show.interface.counters.brief.outrate1[{#IFNUM}]", "UNSIGNED", "bps"),
        ("telemetry.status", "UNSIGNED", None),
        ("telemetry.age", "UNSIGNED", "s"),
        ("telemetry.status[show ptp clock]", "UNSIGNED", None),
    ],
)
def test_value_types_and_units(nwsw, key, value_type, units):
    """値の型と単位が、9.2.3と9.2.5.1の表のとおりである。"""
    item = {**items_of(nwsw), **prototypes_of(nwsw)}[key]
    assert item.get("value_type", "UNSIGNED") == value_type
    assert item.get("units") == units


def test_keys_match_collector_definitions(nwsw, collector_tpl):
    """テンプレートのキーが、collector の定義(値の取り出しとLLD)から作られるキーと一致する。"""
    device_keys = set(PTP_CLOCK.item_keys()) | set(PTP_PARENT.item_keys())
    prototype_keys = {
        f"{build_key(spec.key_prefix, field.key_item)}[{LLD_MACRO_IFNUM}]"
        for spec in (COUNTERS_BRIEF, PTP_BRIEF)
        for field in spec.fields
    }
    lld_keys = {spec.lld_key for spec in COMMANDS.values() if spec.lld_key}
    template_items = set(items_of(nwsw))
    assert device_keys <= template_items
    assert set(prototypes_of(nwsw)) == prototype_keys
    assert {rule["key"] for rule in nwsw["templates"][0]["discovery_rules"]} == lld_keys
    assert {v.key for v in build_lld_values("NEXUS-TEST-01", {}, 0)} == lld_keys


def test_monitor_keys_match_templates(nwsw, collector_tpl):
    """collector が組み立てる自己監視値のキーが、テンプレートのキーと一致する(9.2.5)。"""
    config = DeviceConfig(
        id=1, hostname="NEXUS-TEST-01", source_ip="192.0.2.1", device_type="cisco_nexus", commands=frozenset(COMMANDS)
    )
    runtime = DeviceRuntime(config=config, waiting_since_ns=START_NS)
    sent = device_monitor_values(runtime, START_NS + 1000 * NS, timeout_sec=300)  # 受信待ちのまま途絶 → すべて送る
    nwsw_telemetry = {key for key in items_of(nwsw) if key.startswith("telemetry.")}
    assert {v.key for v in sent} == nwsw_telemetry
    assert {v.host for v in sent} == {"NEXUS-TEST-01"}

    collector_values = collector_monitor_values({name: 0 for name in COUNTER_NAMES}, 0, 0, 0, START_NS)
    assert {v.key for v in collector_values} == set(items_of(collector_tpl))
    assert {v.host for v in collector_values} == {"telemetry"}


def test_values_actually_sent_match_templates(tmp_path, nwsw):
    """テストデータを collector に通したときに、実際に送られるキーと値の型が、テンプレートと合う。"""
    db = tmp_path / "telemetry.db"
    prepare_db(db, [device(counters="1/1-54", ptp="1/1, 1/3-9, 1/21")])
    items = items_of(nwsw)
    prototypes = prototypes_of(nwsw)
    lld_keys = {rule["key"] for rule in nwsw["templates"][0]["discovery_rules"]}

    async def scenario() -> list:
        async with started_app(db) as (app, sink, clock):
            handler = handler_of(app)
            for command in COMMANDS:
                handler.handle(fixture_bytes(command), "192.0.2.1", clock())
            await app.sender.drain()
            return sink.values

    sent = asyncio.run(scenario())
    assert len(sent) > 100
    seen_prototypes = set()
    for value in sent:
        if value.key in lld_keys:
            continue
        generic = re.sub(r"\[\d+/\d+\]$", f"[{LLD_MACRO_IFNUM}]", value.key)
        item = items.get(value.key) or prototypes.get(generic)
        assert item is not None, f"テンプレートにないキーが送られています:{value.key}"
        if generic in prototypes:
            seen_prototypes.add(generic)
        value_type = item.get("value_type", "UNSIGNED")
        if value_type == "CHAR":
            assert isinstance(value.value, str) and len(value.value) <= 255
        elif value_type == "FLOAT":
            assert isinstance(value.value, (int, float))
        else:
            assert isinstance(value.value, int) and value.value >= 0
    assert seen_prototypes == set(prototypes)


# ======================================================================
# アイテムの名前(9.2.6.2、9.2.5.3、決定事項 D-016)
# ======================================================================


def test_item_names_follow_rule(nwsw):
    """アイテムの名前は、showコマンドと項目が分かる名前。インターフェースごとのアイテムには機器での名前を含む(9.2.6.2)。"""
    names = {item["name"] for item in [*items_of(nwsw).values(), *prototypes_of(nwsw).values()]}
    assert "show ptp clock: clock-state" in names
    assert "show ptp brief: {#IFNAME} state" in names
    assert "show interface counters brief: {#IFNAME} inrate1" in names
    assert "show interface counters brief: {#IFNAME} outrate1" in names


def test_collector_item_names_show_units(collector_tpl):
    """件数のアイテムの名前に、パケット数か値の数かを明記する(9.2.5.3)。送信件数はLLD用データと自己監視値を含むことを書き添える(D-016)。"""
    names = {key: item["name"] for key, item in items_of(collector_tpl).items()}
    for key in ("telemetry.collector.received", "telemetry.collector.skipped",
                "telemetry.collector.dropped[unknown_source]", "telemetry.collector.dropped[invalid_data]",
                "telemetry.collector.dropped[unknown_command]"):  # fmt: skip
        assert "(パケット数)" in names[key], key
    for key in ("telemetry.collector.dropped[queue_full]", "telemetry.collector.dropped[zabbix_unreachable]",
                "telemetry.collector.sent.ok", "telemetry.collector.sent.failed"):  # fmt: skip
        assert "値の数" in names[key], key
    assert "LLD用データと自己監視値を含む" in names["telemetry.collector.sent.ok"]
    assert "LLD用データと自己監視値を含む" in names["telemetry.collector.sent.failed"]


# ======================================================================
# トリガー(9.2.6.3、9.2.6.4)
# ======================================================================


def test_nwsw_triggers(nwsw):
    """NWSW用のトリガーは、telemetry.status が 0 と、telemetry.status[<コマンド名>] が 0(4コマンドそれぞれ)だけ。"""
    triggers = triggers_of(nwsw)
    expressions = sorted(t["expression"] for t in triggers)
    assert expressions == sorted(
        [
            "last(/Telemetry NWSW/telemetry.status)=0",
            "last(/Telemetry NWSW/telemetry.status[show interface counters brief])=0",
            "last(/Telemetry NWSW/telemetry.status[show ptp brief])=0",
            "last(/Telemetry NWSW/telemetry.status[show ptp clock])=0",
            "last(/Telemetry NWSW/telemetry.status[show ptp parent])=0",
        ]
    )
    for trigger in triggers:
        assert trigger["priority"] in ("NOT_CLASSIFIED", "INFO", "WARNING", "AVERAGE", "HIGH", "DISASTER")
    # PTPや帯域の監視データに対するトリガーは含めない(9.2.6.4)
    for key, item in {**items_of(nwsw), **prototypes_of(nwsw)}.items():
        if key.startswith("show."):
            assert "triggers" not in item
    assert all("trigger_prototypes" not in rule for rule in nwsw["templates"][0]["discovery_rules"])


def test_command_triggers_depend_on_device_trigger(nwsw):
    """コマンドごとの途絶のトリガー4つは、同じテンプレート(=同じホスト)の機器全体の途絶のトリガーに依存する(D-028)。"""
    triggers = triggers_of(nwsw)
    device = [t for t in triggers if t["expression"] == "last(/Telemetry NWSW/telemetry.status)=0"]
    assert len(device) == 1
    device_trigger = device[0]
    assert "dependencies" not in device_trigger  # 機器全体の途絶のトリガーは、何にも依存しない
    commands = [t for t in triggers if t is not device_trigger]
    assert len(commands) == 4
    for trigger in commands:
        # Zabbix は、依存先を「名前」と「条件式」の組で探す。どちらも依存先と1文字違わず一致していること
        assert trigger["dependencies"] == [{"name": device_trigger["name"], "expression": device_trigger["expression"]}]
    # 依存先の条件式は、このテンプレートのアイテムを指す(テンプレートを適用したホストの、同じホストのトリガーになる)
    assert device_trigger["expression"].startswith(f"last(/{nwsw['templates'][0]['template']}/")


def test_stale_triggers_allow_manual_close(nwsw, collector_tpl):
    """途絶のトリガー5つは、Zabbix の画面で障害を手動で閉じられる(9.2.6.3.4、D-034)。"""
    triggers = triggers_of(nwsw)
    assert len(triggers) == 5
    for trigger in triggers:
        assert trigger["manual_close"] == "YES", trigger["name"]
    # collector の停止のトリガーは、値が届けば自動で解決するため、対象にしない
    assert all("manual_close" not in t for t in triggers_of(collector_tpl))


def test_status_valuemap(nwsw, collector_tpl):
    """受信状態(機器全体と4コマンドごと)の 1 を「正常」、0 を「途絶」と表示する(D-028)。"""
    template = nwsw["templates"][0]
    valuemaps = {vm["name"]: vm for vm in template["valuemaps"]}
    assert set(valuemaps) == {"Telemetry 受信状態"}
    assert valuemaps["Telemetry 受信状態"]["mappings"] == [
        {"value": "1", "newvalue": "正常"},
        {"value": "0", "newvalue": "途絶"},
    ]
    with_map = {key for key, item in items_of(nwsw).items() if "valuemap" in item}
    assert with_map == {
        "telemetry.status",
        "telemetry.status[show interface counters brief]",
        "telemetry.status[show ptp brief]",
        "telemetry.status[show ptp clock]",
        "telemetry.status[show ptp parent]",
    }
    for key in with_map:
        assert items_of(nwsw)[key]["valuemap"] == {"name": "Telemetry 受信状態"}
    assert "valuemaps" not in collector_tpl["templates"][0]
    assert all("valuemap" not in item for item in items_of(collector_tpl).values())


def test_collector_trigger(collector_tpl):
    """中間サーバー用のトリガーは、telemetry.collector.received が3分間届かない、の1つだけ。"""
    triggers = triggers_of(collector_tpl)
    assert [t["expression"] for t in triggers] == ["nodata(/Telemetry collector/telemetry.collector.received,3m)=1"]


def test_trigger_expressions_reference_existing_items(nwsw, collector_tpl):
    for export in (nwsw, collector_tpl):
        template = export["templates"][0]["template"]
        keys = set(items_of(export))
        for trigger in triggers_of(export):
            match = re.fullmatch(r"\w+\(/([^/]+)/(.+?)(,\w+)?\)=\d", trigger["expression"])
            assert match, trigger["expression"]
            assert match.group(1) == template
            assert match.group(2) in keys


def test_self_monitor_interval_shorter_than_nodata():
    """自己監視値の送信間隔の上限(120秒)は、停止を判定する3分より短い(7.4.5.3)。"""
    from common.constants import SETTING_RANGES, SETTING_SELF_MONITOR_INTERVAL_SEC

    assert SETTING_RANGES[SETTING_SELF_MONITOR_INTERVAL_SEC][1] < 3 * 60
    assert zt.COLLECTOR_NODATA_PERIOD == "3m"


def test_interface_keys_use_registered_number_format():
    """LLDの {#IFNUM} に入るのは、登録したインターフェース番号の表記(例:1/1)。"""
    values = build_lld_values("NEXUS-TEST-01", {"show ptp brief": [InterfaceId(1, 1)]}, 0)
    lld = {v.key: v.value for v in values}
    assert '"{#IFNUM}": "1/1"' in lld["show.ptp.brief.discovery"]
    assert PTP_BRIEF.item_keys(InterfaceId(1, 1)) == ("show.ptp.brief.state[1/1]",)
