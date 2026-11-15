"""本体のIPアドレスの入力の検査と、本体側の補助プログラムへの依頼の形(7.4.7、決定事項 D-029、D-030)。

web(コンテナの中)と、本体側の補助プログラム(tools/telemetry_hostctl.py)の両方が使う。
補助プログラムは本体の Python で動くため、このファイルは Python 標準の機能と
common.constants だけを使うこと(deploy.sh が、この2つのファイルだけを本体に置く)。
"""

import ipaddress
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from common.constants import (
    HOSTCTL_MAX_REQUEST_BYTES,
    LISTEN_PORT_MAX,
    LISTEN_PORT_MIN,
    MAX_IP_INPUT_LENGTH,
    NETWORK_DNS_MAX,
    NETWORK_PREFIX_MAX,
    NETWORK_PREFIX_MIN,
)

# 機器や本体のアドレスとしては使わない特別なアドレス(決定事項 D-007、D-030)
REJECTED_IPV4_NETWORKS = (
    ipaddress.IPv4Network("0.0.0.0/32"),
    ipaddress.IPv4Network("127.0.0.0/8"),
    ipaddress.IPv4Network("255.255.255.255/32"),
    ipaddress.IPv4Network("224.0.0.0/4"),  # マルチキャスト(224.0.0.0〜239.255.255.255)
)
REJECTED_IPV4_TEXT = "0.0.0.0、127.x.x.x、255.255.255.255、224.0.0.0〜239.255.255.255 は使えません"

# 入力欄の名前(エラーを表示する場所)
FIELD_ADDRESS = "address"
FIELD_GATEWAY = "gateway"
FIELD_DNS = "dns"

_ADDRESS_PATTERN = re.compile(r"([0-9.]+)/([0-9]{1,2})", re.ASCII)
_REQUEST_ID_PATTERN = re.compile(r"[0-9a-f]{32}", re.ASCII)

# 依頼の種類
ACTION_REBOOT = "reboot"  # (必要なら設定を書き換えてから)本体を再起動する
ACTION_SYNC_FIREWALL = "sync_firewall"  # ファイアウォールの開けるポート番号を、保存された設定に合わせる
ACTIONS = (ACTION_REBOOT, ACTION_SYNC_FIREWALL)


def is_rejected_ipv4(address: ipaddress.IPv4Address) -> bool:
    """特別なアドレス(0.0.0.0、127.x.x.x、255.255.255.255、マルチキャスト)なら真。"""
    return any(address in network for network in REJECTED_IPV4_NETWORKS)


def _parse_ipv4(text: str) -> ipaddress.IPv4Address:
    candidate = text.strip()
    if candidate == "" or len(candidate) > MAX_IP_INPUT_LENGTH:
        raise ValueError
    return ipaddress.IPv4Address(candidate)  # 先頭の0や全角の数字は、ここで誤りになる


# ======================================================================
# 本体のIPアドレスの入力の検査
# ======================================================================


class NetworkInputError(ValueError):
    """本体のIPアドレスの入力の誤り。errors は「入力欄の名前 → エラーの文言の一覧」。"""

    def __init__(self, errors: Mapping[str, list[str]]) -> None:
        self.errors = {name: list(messages) for name, messages in errors.items() if messages}
        super().__init__(" / ".join(m for messages in self.errors.values() for m in messages))

    def messages(self) -> list[str]:
        return [message for messages in self.errors.values() for message in messages]


@dataclass(frozen=True)
class NetworkConfig:
    """検査に合格した、本体のIPアドレスの設定。"""

    address: str
    """IPアドレス/プレフィックス長(例:192.0.2.10/24)"""
    gateway: str
    dns: tuple[str, ...]

    @property
    def ip(self) -> str:
        return self.address.split("/", 1)[0]

    def to_dict(self) -> dict[str, Any]:
        return {"address": self.address, "gateway": self.gateway, "dns": list(self.dns)}


def validate_network(address: str, gateway: str, dns: Sequence[str]) -> NetworkConfig:
    """本体のIPアドレスの入力を検査する(決定事項 D-030)。問題があれば NetworkInputError。

    - IPアドレス/プレフィックス長:必須。プレフィックス長は 8〜30。ネットワークの先頭と末尾のアドレスは使えない
    - デフォルトゲートウェイ:必須。同じネットワークの中のアドレスで、本体のアドレスとは別のもの
    - DNSサーバー:1つ以上、2つまで(空欄の入力欄は数えない)
    - いずれも、特別なアドレス(0.0.0.0、127.x.x.x など)は使えない
    """
    errors: dict[str, list[str]] = {}

    def add(name: str, message: str) -> None:
        errors.setdefault(name, []).append(message)

    interface: ipaddress.IPv4Interface | None = None
    address_text = address.strip()
    match = _ADDRESS_PATTERN.fullmatch(address_text) if len(address_text) <= MAX_IP_INPUT_LENGTH else None
    if address_text == "":
        add(FIELD_ADDRESS, "IPアドレス/プレフィックス長を入力してください(例:192.0.2.10/24)")
    elif match is None:
        add(FIELD_ADDRESS, "IPアドレス/プレフィックス長の形式が正しくありません(例:192.0.2.10/24)")
    else:
        try:
            ip = _parse_ipv4(match.group(1))
        except ValueError:
            add(FIELD_ADDRESS, "IPアドレスの形式が正しくありません(例:192.0.2.10/24)")
        else:
            prefix = int(match.group(2))
            if not NETWORK_PREFIX_MIN <= prefix <= NETWORK_PREFIX_MAX:
                add(FIELD_ADDRESS, f"プレフィックス長は {NETWORK_PREFIX_MIN}〜{NETWORK_PREFIX_MAX} で入力してください")
            elif is_rejected_ipv4(ip):
                add(FIELD_ADDRESS, f"このIPアドレスは、本体には使えません({REJECTED_IPV4_TEXT})")
            else:
                interface = ipaddress.IPv4Interface(f"{ip}/{prefix}")
                if ip in (interface.network.network_address, interface.network.broadcast_address):
                    add(FIELD_ADDRESS, "ネットワークの先頭と末尾のアドレスは、本体には使えません")
                    interface = None

    gateway_text = ""
    if gateway.strip() == "":
        add(FIELD_GATEWAY, "デフォルトゲートウェイを入力してください")
    else:
        try:
            gateway_ip = _parse_ipv4(gateway)
        except ValueError:
            add(FIELD_GATEWAY, "デフォルトゲートウェイの形式が正しくありません(例:192.0.2.1)")
        else:
            gateway_text = str(gateway_ip)
            if is_rejected_ipv4(gateway_ip):
                add(FIELD_GATEWAY, f"このアドレスは、デフォルトゲートウェイには使えません({REJECTED_IPV4_TEXT})")
            elif interface is not None:
                network = interface.network
                if gateway_ip not in network:
                    add(FIELD_GATEWAY, f"デフォルトゲートウェイは、本体と同じネットワーク({network})の中のアドレスにしてください")
                elif gateway_ip in (network.network_address, network.broadcast_address):
                    add(FIELD_GATEWAY, "ネットワークの先頭と末尾のアドレスは、デフォルトゲートウェイには使えません")
                elif gateway_ip == interface.ip:
                    add(FIELD_GATEWAY, "デフォルトゲートウェイには、本体と別のアドレスを入力してください")

    dns_values: list[str] = []
    entries = [text for text in dns if text.strip() != ""]
    if not entries:
        add(FIELD_DNS, "DNSサーバーを1つ以上入力してください")
    elif len(entries) > NETWORK_DNS_MAX:
        add(FIELD_DNS, f"DNSサーバーは {NETWORK_DNS_MAX} つまで入力できます")
    else:
        for text in entries:
            try:
                dns_ip = _parse_ipv4(text)
            except ValueError:
                add(FIELD_DNS, "DNSサーバーの形式が正しくありません(例:192.0.2.53)")
                continue
            if is_rejected_ipv4(dns_ip):
                add(FIELD_DNS, f"このアドレスは、DNSサーバーには使えません({REJECTED_IPV4_TEXT})")
            elif str(dns_ip) in dns_values:
                add(FIELD_DNS, "同じDNSサーバーが2回入力されています")
            else:
                dns_values.append(str(dns_ip))

    if errors:
        raise NetworkInputError(errors)
    assert interface is not None
    return NetworkConfig(address=str(interface), gateway=gateway_text, dns=tuple(dns_values))


def network_from_dict(data: object) -> NetworkConfig:
    """依頼や状態のファイルに書かれた設定を、もう一度すべて検査して読み取る。"""
    if not isinstance(data, dict) or set(data) != {"address", "gateway", "dns"}:
        raise NetworkInputError({FIELD_ADDRESS: ["IPアドレスの設定の形が正しくありません"]})
    address, gateway, dns = data["address"], data["gateway"], data["dns"]
    if not isinstance(address, str) or not isinstance(gateway, str) or not isinstance(dns, list):
        raise NetworkInputError({FIELD_ADDRESS: ["IPアドレスの設定の形が正しくありません"]})
    if not all(isinstance(item, str) for item in dns):
        raise NetworkInputError({FIELD_DNS: ["DNSサーバーの形が正しくありません"]})
    return validate_network(address, gateway, dns)


def check_listen_port(value: object) -> int:
    """待ち受けるポート番号(1024〜65535)を検査する(決定事項 D-030)。正しくなければ ValueError。"""
    if isinstance(value, bool) or not isinstance(value, int) or not LISTEN_PORT_MIN <= value <= LISTEN_PORT_MAX:
        raise ValueError(f"ポート番号は {LISTEN_PORT_MIN}〜{LISTEN_PORT_MAX} にしてください")
    return value


# ======================================================================
# 補助プログラムへの依頼(web → 補助プログラム)と、その結果(補助プログラム → web)
# ======================================================================


class RequestError(ValueError):
    """依頼のファイルの誤り。"""


@dataclass(frozen=True)
class HostRequest:
    """補助プログラムへの依頼。"""

    id: str
    created_at: float
    """依頼を作った時刻(1970年からの秒数)"""
    action: str
    network: NetworkConfig | None = None
    """書き換える本体のIPアドレス(再起動の依頼のときだけ)"""
    webgui_port: int | None = None
    """再起動のあとに使う、新しいWebGUIのポート番号(再起動の依頼のときだけ)"""

    def to_json_bytes(self) -> bytes:
        data: dict[str, Any] = {"id": self.id, "created_at": self.created_at, "action": self.action}
        if self.network is not None:
            data["network"] = self.network.to_dict()
        if self.webgui_port is not None:
            data["webgui_port"] = self.webgui_port
        return json.dumps(data, ensure_ascii=False).encode("utf-8")


def is_valid_request_id(value: object) -> bool:
    return isinstance(value, str) and _REQUEST_ID_PATTERN.fullmatch(value) is not None


def parse_request(raw: bytes) -> HostRequest:
    """依頼のファイルの中身を、すべて検査して読み取る。問題があれば RequestError。

    依頼は web コンテナが書くため、中身を信用せずに検査する(11.3.3)。
    """
    if len(raw) > HOSTCTL_MAX_REQUEST_BYTES:
        raise RequestError("依頼のファイルが大きすぎます")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RequestError("依頼のファイルの形式が正しくありません") from None
    if not isinstance(data, dict):
        raise RequestError("依頼のファイルの形式が正しくありません")
    allowed = {"id", "created_at", "action", "network", "webgui_port"}
    if not set(data) <= allowed or not {"id", "created_at", "action"} <= set(data):
        raise RequestError("依頼のファイルの項目が正しくありません")
    if not is_valid_request_id(data["id"]):
        raise RequestError("依頼の番号が正しくありません")
    created_at = data["created_at"]
    if isinstance(created_at, bool) or not isinstance(created_at, (int, float)):
        raise RequestError("依頼の時刻が正しくありません")
    action = data["action"]
    if action not in ACTIONS:
        raise RequestError("依頼の種類が正しくありません")
    network = None
    webgui_port = None
    if action == ACTION_SYNC_FIREWALL and ("network" in data or "webgui_port" in data):
        raise RequestError("ファイアウォールの依頼に、余計な項目があります")
    if "network" in data:
        try:
            network = network_from_dict(data["network"])
        except NetworkInputError as exc:
            raise RequestError(f"IPアドレスの設定が正しくありません:{exc}") from None
    if "webgui_port" in data:
        try:
            webgui_port = check_listen_port(data["webgui_port"])
        except ValueError as exc:
            raise RequestError(f"WebGUIのポート番号が正しくありません:{exc}") from None
    if action == ACTION_REBOOT and network is None and webgui_port is None:
        raise RequestError("再起動の依頼に、変更する内容がありません")
    return HostRequest(id=data["id"], created_at=float(created_at), action=action, network=network, webgui_port=webgui_port)


@dataclass(frozen=True)
class HostResult:
    """補助プログラムの処理の結果。"""

    id: str
    ok: bool
    message: str

    def to_json_bytes(self) -> bytes:
        return json.dumps({"id": self.id, "ok": self.ok, "message": self.message}, ensure_ascii=False).encode("utf-8")


def parse_result(raw: bytes, expected_id: str) -> HostResult:
    """結果のファイルの中身を読み取る。問題があれば RequestError。"""
    if len(raw) > HOSTCTL_MAX_REQUEST_BYTES:
        raise RequestError("結果のファイルが大きすぎます")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RequestError("結果のファイルの形式が正しくありません") from None
    if (
        not isinstance(data, dict)
        or data.get("id") != expected_id
        or not isinstance(data.get("ok"), bool)
        or not isinstance(data.get("message"), str)
    ):
        raise RequestError("結果のファイルの形式が正しくありません")
    return HostResult(id=expected_id, ok=data["ok"], message=data["message"][:500])


def ipv4_strings(values: Iterable[object]) -> list[str]:
    """IPv4アドレスとして正しい文字列だけを取り出す(状態の表示用)。"""
    result: list[str] = []
    for value in values:
        if isinstance(value, str):
            try:
                result.append(str(_parse_ipv4(value)))
            except ValueError:
                continue
    return result
