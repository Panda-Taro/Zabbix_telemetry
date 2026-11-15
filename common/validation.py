"""入力内容の検査(6.3.5〜6.3.7、7.4.4.3、7.4.5.1、UI-09、UI-11)。

画面からの入力と、バックアップファイルの中身の両方を、ここで検査する(11.3.3)。
エラーの文言は、そのまま画面に表示できる日本語にする。
画面に表示するときは、入力された文字列を必ず無害化すること(11.3.3)。
"""

import ipaddress
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field

from common.constants import (
    COMMAND_COLUMNS,
    DEVICE_TYPE_CISCO_NEXUS,
    HOSTNAME_MAX_LENGTH,
    LOG_LEVELS,
    MAX_INTERFACE_INPUT_LENGTH,
    MAX_IP_INPUT_LENGTH,
    PORT_MAX,
    PORT_MIN,
    PURPOSE_COMMAND_COLUMNS,
    SELF_ZABBIX_HOSTNAME,
    SETTING_CONFIG_VERSION,
    SETTING_DEFAULTS,
    SETTING_INTEGER_MAX,
    SETTING_LLD_INTERVAL_SEC,
    SETTING_LOG_LEVEL,
    SETTING_QUEUE_MAX_SIZE,
    SETTING_RANGES,
    SETTING_SELF_MONITOR_INTERVAL_SEC,
    SETTING_STALE_TIMEOUT_SEC,
    SETTING_TELEMETRY_UDP_PORT,
    SETTING_WEBGUI_PORT,
    SETTING_ZABBIX_PORT,
    SETTING_ZABBIX_SERVER,
    SLOT_MAX,
    SLOT_MIN,
    TCP_UDP_PORT_MAX,
    TCP_UDP_PORT_MIN,
)
from common.interfaces import InterfaceId

# エラーの文言に入力をそのまま入れるときの、文字数の上限
_ECHO_MAX_LENGTH = 20


class ValidationError(Exception):
    """入力内容の検査で見つかった問題。errors は「項目名 → エラーの文言の一覧」。"""

    def __init__(self, errors: Mapping[str, Iterable[str]]) -> None:
        self.errors: dict[str, list[str]] = {name: list(messages) for name, messages in errors.items() if messages}
        super().__init__(" / ".join(f"{name}: {message}" for name, messages in self.errors.items() for message in messages))

    def messages(self) -> list[str]:
        """すべてのエラーの文言を、1つの一覧にする。"""
        return [message for messages in self.errors.values() for message in messages]


def _echo(text: str) -> str:
    """エラーの文言に入れるため、入力を短く切る。"""
    return text if len(text) <= _ECHO_MAX_LENGTH else text[:_ECHO_MAX_LENGTH] + "…"


# ======================================================================
# インターフェース番号(6.3.5〜6.3.7、UI-09)
# ======================================================================

# 「スロット番号/ポート番号」と、その範囲指定「スロット番号/ポート番号-ポート番号」
_INTERFACE_ITEM_PATTERN = re.compile(r"([0-9]+)/([0-9]+)(?:-([0-9]+))?", re.ASCII)


def check_interface_range(slot: int, port: int) -> str | None:
    """スロット番号とポート番号が範囲内か調べる。範囲外ならエラーの文言を返す。"""
    if not SLOT_MIN <= slot <= SLOT_MAX:
        return f"スロット番号は {SLOT_MIN}〜{SLOT_MAX} で入力してください"
    if not PORT_MIN <= port <= PORT_MAX:
        return f"ポート番号は {PORT_MIN}〜{PORT_MAX} で入力してください"
    return None


def parse_interface_list(text: str) -> tuple[InterfaceId, ...]:
    """インターフェース番号の入力を検査し、1件ずつに展開する。

    受け付ける形式(6.3.6、6.3.7):
      - 1/1                 1件
      - 1/1, 1/2, 1/48      カンマ区切り(カンマの前後の空白は無視する)
      - 1/1-4               範囲指定(1/1、1/2、1/3、1/4 に展開する。8.3.2.2)
    受け付けないもの:3段の番号(1/49/1)、機器の表記(Eth1/1)、範囲外の番号、逆順の範囲、重複。
    空の入力は、0件として返す(空でよいかどうかは、機器登録の検査で判断する)。
    問題があれば ValueError を出す。文言は args[0] に一覧で入る。
    """
    if len(text) > MAX_INTERFACE_INPUT_LENGTH:
        raise ValueError([f"入力が長すぎます({MAX_INTERFACE_INPUT_LENGTH}文字まで)"])
    stripped = text.strip()
    if stripped == "":
        return ()

    errors: list[str] = []
    result: list[InterfaceId] = []
    seen: set[InterfaceId] = set()
    duplicates: list[InterfaceId] = []

    for raw_item in stripped.split(","):
        item = raw_item.strip()
        if item == "":
            errors.append("カンマとカンマの間に、番号が入っていません")
            continue
        match = _INTERFACE_ITEM_PATTERN.fullmatch(item)
        if match is None:
            errors.append(f"「{_echo(item)}」は形式が正しくありません(例:1/1、1/1-4)")
            continue
        slot = int(match.group(1))
        start = int(match.group(2))
        end = int(match.group(3)) if match.group(3) is not None else start
        problem = check_interface_range(slot, start) or check_interface_range(slot, end)
        if problem is not None:
            errors.append(f"「{_echo(item)}」:{problem}")
            continue
        if end < start:
            errors.append(f"「{_echo(item)}」:範囲は、小さい番号から大きい番号の順に入力してください(例:1/1-4)")
            continue
        for port in range(start, end + 1):
            interface = InterfaceId(slot, port)
            if interface in seen:
                if interface not in duplicates:
                    duplicates.append(interface)
            else:
                seen.add(interface)
                result.append(interface)

    if duplicates:
        errors.append("番号が重複しています:" + "、".join(i.ifnum for i in duplicates))
    if errors:
        raise ValueError(errors)
    return tuple(sorted(result))


def format_interface_list(interfaces: Iterable[InterfaceId]) -> str:
    """インターフェース番号の一覧を、入力欄に表示する形にする(parse_interface_list の逆)。

    同じスロットで続いている番号は、範囲指定にまとめる。例)1/1, 1/2, 1/3, 1/48 → 「1/1-3, 1/48」
    """
    items: list[str] = []
    ordered = sorted(set(interfaces))
    index = 0
    while index < len(ordered):
        start = ordered[index]
        end = start
        while index + 1 < len(ordered) and ordered[index + 1] == InterfaceId(start.slot, end.port + 1):
            index += 1
            end = ordered[index]
        items.append(start.ifnum if end == start else f"{start.ifnum}-{end.port}")
        index += 1
    return ", ".join(items)


# ======================================================================
# ホスト名と送信元IPアドレス(7.4.4.3)
# ======================================================================

# Zabbixのホスト名で使える文字:半角の英数字、ピリオド、ハイフン、アンダースコア、空白
_HOSTNAME_PATTERN = re.compile(r"[A-Za-z0-9._\- ]+", re.ASCII)


def check_hostname(hostname: str) -> list[str]:
    """ホスト名の形式を検査し、問題があればエラーの文言の一覧を返す(重複は別に調べる)。"""
    if hostname == "":
        return ["ホスト名を入力してください"]
    if len(hostname) > HOSTNAME_MAX_LENGTH:
        return [f"ホスト名は {HOSTNAME_MAX_LENGTH} 文字以内で入力してください"]
    if _HOSTNAME_PATTERN.fullmatch(hostname) is None:
        return ["ホスト名に使えるのは、半角の英数字、ピリオド(.)、ハイフン(-)、アンダースコア(_)、空白だけです"]
    if hostname != hostname.strip():
        return ["ホスト名の先頭と末尾に、空白は使えません"]
    if same_hostname(hostname, SELF_ZABBIX_HOSTNAME):
        return [f"「{SELF_ZABBIX_HOSTNAME}」は本システム自身のZabbixホスト名のため、機器のホスト名には使えません"]
    return []


def same_hostname(first: str, second: str) -> bool:
    """機器登録の検査で、2つのホスト名を同じと見なすか。大文字・小文字は区別しない(決定事項 D-008)。"""
    return first.lower() == second.lower()


def normalize_ipv4(text: str) -> str:
    """IPv4アドレスの形式を検査し、標準の表記にして返す。正しくなければ ValueError。

    IPアドレスは IPv4 のみを対象とする(11.5.5)。
    """
    candidate = text.strip()
    if candidate == "":
        raise ValueError("IPアドレスを入力してください")
    if len(candidate) > MAX_IP_INPUT_LENGTH:
        raise ValueError("IPアドレスの形式が正しくありません(例:192.0.2.1)")
    try:
        address = ipaddress.IPv4Address(candidate)
    except ValueError:
        raise ValueError("IPアドレスの形式が正しくありません(例:192.0.2.1)") from None
    return str(address)


# 送信元IPアドレスとして受け付けないアドレス(決定事項 D-007)
_REJECTED_SOURCE_NETWORKS = (
    ipaddress.IPv4Network("0.0.0.0/32"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("255.255.255.255/32"),
    ipaddress.IPv4Network("224.0.0.0/4"),  # マルチキャスト(224.0.0.0〜239.255.255.255)
)


def normalize_source_ip(text: str) -> str:
    """機器の送信元IPアドレスを検査し、標準の表記にして返す。正しくなければ ValueError。

    形式の検査に加えて、機器には使わない特別なアドレスを拒否する。
    """
    normalized = normalize_ipv4(text)
    address = ipaddress.IPv4Address(normalized)
    if any(address in network for network in _REJECTED_SOURCE_NETWORKS):
        raise ValueError(
            "このIPアドレスは、機器の送信元としては使えません"
            "(0.0.0.0、127.x.x.x、255.255.255.255、224.0.0.0〜239.255.255.255 は使えません)"
        )
    return normalized


# ======================================================================
# 機器登録の検査(7.4.4.3、UI-11)
# ======================================================================


@dataclass(frozen=True)
class DeviceIdentity:
    """重複の検査に使う、登録済みの機器の情報。"""

    id: int | None
    hostname: str
    source_ip: str


@dataclass(frozen=True)
class DeviceData:
    """検査に合格した、機器1台分の登録内容。"""

    hostname: str
    source_ip: str
    device_type: str
    commands: Mapping[str, bool]
    """devices テーブルの列名(cmd_counters_brief など)→ チェックしたか。"""
    interfaces: Mapping[str, tuple[InterfaceId, ...]]
    """用途(counters / ptp)→ 監視対象インターフェース。チェックを外したコマンドの用途は空。"""


@dataclass
class DeviceForm:
    """画面から届いた、機器1台分の入力。"""

    hostname: str
    source_ip: str
    commands: Mapping[str, bool]
    """devices テーブルの列名(cmd_counters_brief など)→ チェックしたか。"""
    interfaces_text: Mapping[str, str] = field(default_factory=dict)
    """用途(counters / ptp)→ インターフェース番号の入力欄の文字列。"""


def error_key_for_purpose(purpose: str) -> str:
    """インターフェース番号の入力欄のエラーを入れる項目名。"""
    return f"interfaces.{purpose}"


def build_device(
    hostname: str,
    source_ip: str,
    commands: Mapping[str, bool],
    interfaces: Mapping[str, tuple[InterfaceId, ...]],
    existing: Iterable[DeviceIdentity],
    self_id: int | None = None,
    *,
    reject_unchecked_interfaces: bool = False,
    initial_errors: Mapping[str, list[str]] | None = None,
) -> DeviceData:
    """機器1台分の内容を検査し、合格すれば DeviceData を返す。問題があれば ValidationError。

    検査すること(7.4.4.3):
      - ホスト名:空、使えない文字、128文字超、「telemetry」、他の機器との重複
      - 送信元IPアドレス:形式、他の機器との重複
      - 4つのコマンドが1つもチェックされていない
      - チェックしたコマンドのインターフェース番号が空
    self_id には、編集中の機器のidを渡す(自分自身とは重複と見なさない)。
    reject_unchecked_interfaces が真のときは、チェックを外したコマンドに
    インターフェース番号があることも誤りとする(バックアップファイルの検査用)。
    """
    errors: dict[str, list[str]] = {name: list(msgs) for name, msgs in (initial_errors or {}).items()}

    def add(name: str, message: str) -> None:
        errors.setdefault(name, []).append(message)

    for message in check_hostname(hostname):
        add("hostname", message)

    normalized_ip = ""
    try:
        normalized_ip = normalize_source_ip(source_ip)
    except ValueError as exc:
        add("source_ip", str(exc))

    others = [device for device in existing if self_id is None or device.id != self_id]
    if "hostname" not in errors and any(same_hostname(device.hostname, hostname) for device in others):
        add("hostname", "このホスト名は、他の機器で登録済みです")
    if normalized_ip and any(device.source_ip == normalized_ip for device in others):
        add("source_ip", "この送信元IPアドレスは、他の機器で登録済みです")

    unknown_columns = set(commands) - set(COMMAND_COLUMNS.values())
    if unknown_columns:
        add("commands", "不明なコマンドの指定があります")
    checked = {column: bool(commands.get(column, False)) for column in COMMAND_COLUMNS.values()}
    if not any(checked.values()):
        add("commands", "Zabbixへ送るコマンドを、1つ以上チェックしてください")

    unknown_purposes = set(interfaces) - set(PURPOSE_COMMAND_COLUMNS)
    if unknown_purposes:
        add("interfaces", "不明な用途のインターフェース番号があります")

    final_interfaces: dict[str, tuple[InterfaceId, ...]] = {}
    for purpose, column in PURPOSE_COMMAND_COLUMNS.items():
        key = error_key_for_purpose(purpose)
        values = interfaces.get(purpose, ())
        if checked[column]:
            if not values and key not in errors:
                add(key, "チェックしたコマンドのインターフェース番号を入力してください")
            final_interfaces[purpose] = values
        else:
            if values and reject_unchecked_interfaces:
                add(key, "チェックを外したコマンドに、インターフェース番号があります")
            # チェックを外したコマンドのインターフェース番号は保存しない
            final_interfaces[purpose] = ()

    if errors:
        raise ValidationError(errors)
    return DeviceData(
        hostname=hostname,
        source_ip=normalized_ip,
        device_type=DEVICE_TYPE_CISCO_NEXUS,
        commands=checked,
        interfaces=final_interfaces,
    )


def validate_device_form(form: DeviceForm, existing: Iterable[DeviceIdentity], self_id: int | None = None) -> DeviceData:
    """画面から届いた機器登録の入力を検査する(UI-11)。

    ホスト名の前後の空白は取り除いてから検査する。
    チェックを外したコマンドのインターフェース番号の入力欄は、検査も保存もしない。
    """
    errors: dict[str, list[str]] = {}
    interfaces: dict[str, tuple[InterfaceId, ...]] = {}
    for purpose, column in PURPOSE_COMMAND_COLUMNS.items():
        if not form.commands.get(column, False):
            continue
        try:
            interfaces[purpose] = parse_interface_list(form.interfaces_text.get(purpose, ""))
        except ValueError as exc:
            errors[error_key_for_purpose(purpose)] = list(exc.args[0])
    return build_device(
        hostname=form.hostname.strip(),
        source_ip=form.source_ip,
        commands=form.commands,
        interfaces=interfaces,
        existing=existing,
        self_id=self_id,
        initial_errors=errors,
    )


# ======================================================================
# 全体設定の検査(7.4.5.1、8.3.3.2)
# ======================================================================


def _check_integer(text: str, minimum: int, maximum: int, label: str) -> str:
    candidate = text.strip()
    if re.fullmatch(r"[0-9]+", candidate, re.ASCII) is None or len(candidate) > 10:
        raise ValueError(f"{label}は、半角の整数で入力してください")
    value = int(candidate)
    if not minimum <= value <= maximum:
        raise ValueError(f"{label}は、{minimum}〜{maximum} の範囲で入力してください")
    return str(value)


def _check_port(label: str) -> Callable[[str], str]:
    return lambda text: _check_integer(text, TCP_UDP_PORT_MIN, TCP_UDP_PORT_MAX, label)


def _check_ranged(key: str, label: str) -> Callable[[str], str]:
    minimum, maximum = SETTING_RANGES[key]
    return lambda text: _check_integer(text, minimum, maximum, label)


def _check_zabbix_server(text: str) -> str:
    if text.strip() == "":
        return ""
    try:
        return normalize_ipv4(text)
    except ValueError as exc:
        raise ValueError(f"ZabbixサーバーのIPアドレス:{exc}") from None


def _check_log_level(text: str) -> str:
    candidate = text.strip().upper()
    if candidate not in LOG_LEVELS:
        raise ValueError("ログレベルは、" + " / ".join(LOG_LEVELS) + " のいずれかを選んでください")
    return candidate


def _check_config_version(text: str) -> str:
    return _check_integer(text, 1, SETTING_INTEGER_MAX, "設定の版番号")


# 設定名ごとの検査の方法。正しければ、保存する形の文字列を返す
_SETTING_CHECKERS = {
    SETTING_ZABBIX_SERVER: _check_zabbix_server,
    SETTING_ZABBIX_PORT: _check_port("Zabbixサーバーのポート番号"),
    SETTING_TELEMETRY_UDP_PORT: _check_port("Telemetryの受信ポート番号"),
    SETTING_STALE_TIMEOUT_SEC: _check_ranged(SETTING_STALE_TIMEOUT_SEC, "途絶判定時間(秒)"),
    SETTING_SELF_MONITOR_INTERVAL_SEC: _check_ranged(SETTING_SELF_MONITOR_INTERVAL_SEC, "自己監視値の送信間隔(秒)"),
    SETTING_LLD_INTERVAL_SEC: _check_ranged(SETTING_LLD_INTERVAL_SEC, "LLD用データの定期送信間隔(秒)"),
    SETTING_QUEUE_MAX_SIZE: _check_ranged(SETTING_QUEUE_MAX_SIZE, "送信待ちキューの上限(件)"),
    SETTING_LOG_LEVEL: _check_log_level,
    SETTING_WEBGUI_PORT: _check_port("WebGUIのポート番号"),
    SETTING_CONFIG_VERSION: _check_config_version,
}
if set(_SETTING_CHECKERS) != set(SETTING_DEFAULTS):
    raise RuntimeError("すべての設定名に、検査の方法を定めてください")


def validate_settings(
    values: Mapping[str, object],
    *,
    required_keys: Collection[str] = (),
    require_zabbix_server: bool = False,
) -> dict[str, str]:
    """全体設定の値を検査し、保存する形の文字列にして返す。問題があれば ValidationError。

    required_keys:必ず含まれていなければならない設定名(バックアップファイルの検査用)。
    require_zabbix_server:ZabbixサーバーのIPアドレスを必須にするか(全体設定の画面では必須、7.4.5.1)。
    """
    errors: dict[str, list[str]] = {}
    result: dict[str, str] = {}

    for key in required_keys:
        if key not in values:
            errors.setdefault(key, []).append(f"設定「{key}」がありません")

    for key, raw in values.items():
        checker = _SETTING_CHECKERS.get(key)
        if checker is None:
            errors.setdefault(str(key)[:_ECHO_MAX_LENGTH], []).append(f"不明な設定名「{_echo(str(key))}」があります")
            continue
        if not isinstance(raw, str):
            errors.setdefault(key, []).append(f"設定「{key}」の値は、文字列で指定してください")
            continue
        try:
            result[key] = checker(raw)
        except ValueError as exc:
            errors.setdefault(key, []).append(str(exc))

    if require_zabbix_server and result.get(SETTING_ZABBIX_SERVER, "") == "" and SETTING_ZABBIX_SERVER not in errors:
        errors.setdefault(SETTING_ZABBIX_SERVER, []).append("ZabbixサーバーのIPアドレスを入力してください")

    if errors:
        raise ValidationError(errors)
    return result
