"""本体側の補助プログラム(決定事項 D-029)。

中間サーバー本体(Ubuntu)で、管理者の権限で動く。web コンテナには特別な権限を与えず、
web はこのプログラムへの「依頼」を決まったフォルダにファイルで置くだけとする。
このプログラムは依頼の中身をすべて検査し、正しい場合だけ次のことを行う。

  - 本体のIPアドレスの設定ファイル(netplan)の書き換え(反映は再起動のとき)
  - 本体の再起動
  - ファイアウォール(ufw)が有効なときの、ポート番号の開け閉め(決定事項 D-030)

deploy.sh が本体に置き、systemd(常駐の仕組み)から呼び出す。deploy.sh と復旧用コマンドも、
このプログラムを直接使う。本体の Python 3 と、Ubuntu に入っている PyYAML(netplan の設定ファイルの
読み書き)だけで動く。

使い方(管理者の権限で実行する):
    python3 telemetry_hostctl.py process                 依頼を処理する(systemd から呼ばれる)
    python3 telemetry_hostctl.py boot                    起動時の処理(状態の書き出しとファイアウォールの調整)
    python3 telemetry_hostctl.py state                   本体のネットワークの状態を書き出す
    python3 telemetry_hostctl.py firewall-sync           ファイアウォールを、保存された設定のポート番号に合わせる
    python3 telemetry_hostctl.py show-ports              保存された設定のポート番号を表示する
    python3 telemetry_hostctl.py show-network [--field 項目]   本体のネットワークの状態を表示する
    python3 telemetry_hostctl.py set-network --address 192.0.2.10/24 --gateway 192.0.2.1 --dns 192.0.2.53 [--check-only]
                                                          IPアドレスの設定ファイルを書き換える(反映はしない)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# リポジトリの中(tools/)からでも、本体に置いた場所(/usr/local/lib/telemetry-hostctl/tools/)からでも、
# 1つ上のフォルダにある common を読み込めるようにする
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from common.constants import (  # noqa: E402
    DEFAULT_DB_PATH,
    HOSTCTL_COMMAND_TIMEOUT_SEC,
    HOSTCTL_INBOX,
    HOSTCTL_MAX_REQUEST_BYTES,
    HOSTCTL_NETPLAN_FILE,
    HOSTCTL_NETWORK_STATE_FILE,
    HOSTCTL_REBOOT_DELAY_SEC,
    HOSTCTL_REQUEST_MAX_AGE_SEC,
    HOSTCTL_RESULT_TTL_SEC,
    HOSTCTL_RESULTS,
    HOSTCTL_RUN_DIR,
    HOSTCTL_STATE,
    HOSTCTL_STATE_DIR,
    SETTING_DEFAULTS,
    SETTING_TELEMETRY_UDP_PORT,
    SETTING_WEBGUI_PORT,
    SQLITE_BUSY_TIMEOUT_SEC,
    WEBGUI_FALLBACK_PORT,
)
from common.netconfig import (  # noqa: E402
    ACTION_REBOOT,
    ACTION_SYNC_FIREWALL,
    HostRequest,
    HostResult,
    NetworkConfig,
    NetworkInputError,
    RequestError,
    check_listen_port,
    ipv4_strings,
    is_valid_request_id,
    parse_request,
    validate_network,
)

NETPLAN_DIR = "/etc/netplan"
CLOUD_INIT_CFG_DIR = "/etc/cloud/cloud.cfg.d"
# cloud-init が、起動のたびにネットワークの設定ファイルを作り直さないようにする設定
CLOUD_INIT_DISABLE_FILE = "99-telemetry-disable-network-config.cfg"
CLOUD_INIT_DISABLE_TEXT = "# Telemetry 中間サーバーが置いたファイル。本体のIPアドレスは netplan の設定を正とする\nnetwork: {config: disabled}\n"
FIREWALL_STATE_FILE = "firewall.json"
NETPLAN_BACKUP_DIR = "netplan-backup"
REBOOT_COMMAND = ("systemctl", "reboot")
# netplan で LAN端子の設定を書く場所のうち、このプログラムが扱えるもの
NETPLAN_ETHERNETS = "ethernets"
NETPLAN_OTHER_SECTIONS = ("bonds", "bridges", "vlans", "wifis", "tunnels", "vrfs", "modems", "dummy-devices", "virtual-ethernets")
# 自分が書く netplan の設定ファイルの先頭に付ける説明
NETPLAN_HEADER = "# Telemetry 中間サーバーのシステム設定(WebGUI)、または deploy.sh が書いたファイル\n"


class HostCtlError(Exception):
    """処理を続けられない誤り。文言は、そのまま画面やログに出せる日本語にする。"""


# ======================================================================
# 外部のコマンドの実行(テストでは差し替える)
# ======================================================================

Runner = Callable[[Sequence[str]], "subprocess.CompletedProcess[str]"]


def run_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command), capture_output=True, text=True, timeout=HOSTCTL_COMMAND_TIMEOUT_SEC, check=False
    )


def _run_checked(runner: Runner, command: Sequence[str], what: str) -> str:
    try:
        completed = runner(command)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HostCtlError(f"{what}に失敗しました({exc})") from None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        raise HostCtlError(f"{what}に失敗しました({detail[-1] if detail else f'終了コード {completed.returncode}'})")
    return completed.stdout


@dataclass
class YamlIO:
    """netplan の設定ファイルの読み書き。本体では PyYAML を使う(テストでは差し替える)。"""

    load: Callable[[str], Any]
    dump: Callable[[Any], str]


def pyyaml_io() -> YamlIO:
    try:
        import yaml  # noqa: PLC0415 本体の Ubuntu に入っている PyYAML(python3-yaml)
    except ImportError:
        raise HostCtlError("PyYAML(python3-yaml)が見つかりません。deploy.sh をもう一度実行してください") from None
    return YamlIO(
        load=yaml.safe_load,
        dump=lambda data: yaml.safe_dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
    )


# ======================================================================
# 本体のネットワークの状態
# ======================================================================


@dataclass(frozen=True)
class NetworkState:
    interface: str
    address: str  # 例:192.0.2.10/24
    gateway: str
    dns: tuple[str, ...]
    dhcp: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "interface": self.interface,
            "address": self.address,
            "gateway": self.gateway,
            "dns": list(self.dns),
            "dhcp": self.dhcp,
        }


def _ip_json(runner: Runner, args: Sequence[str], what: str) -> list[dict[str, Any]]:
    output = _run_checked(runner, ["ip", "-j", *args], what)
    try:
        data = json.loads(output or "[]")
    except json.JSONDecodeError:
        raise HostCtlError(f"{what}の結果を読み取れませんでした") from None
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def default_route(runner: Runner) -> tuple[str, str]:
    """デフォルトゲートウェイにつながっているLAN端子の名前と、ゲートウェイのアドレス(決定事項 D-030)。"""
    routes = _ip_json(runner, ["-4", "route", "show", "default"], "デフォルトゲートウェイの確認")
    for route in routes:
        dev, gateway = route.get("dev"), route.get("gateway")
        if isinstance(dev, str) and dev and isinstance(gateway, str):
            return dev, gateway
    raise HostCtlError("デフォルトゲートウェイが見つかりません。コンソールで、Ubuntuの標準の手順で設定してください")


def read_network_state(runner: Runner) -> NetworkState:
    dev, gateway = default_route(runner)
    address = ""
    dhcp = False
    for link in _ip_json(runner, ["-4", "addr", "show", "dev", dev], "IPアドレスの確認"):
        for info in link.get("addr_info", []):
            if isinstance(info, dict) and info.get("family") == "inet" and not info.get("secondary"):
                address = f"{info.get('local')}/{info.get('prefixlen')}"
                dhcp = bool(info.get("dynamic"))
                break
        if address:
            break
    dns: list[str] = []
    try:
        completed = runner(["resolvectl", "dns", dev])
        if completed.returncode == 0:
            # 出力の例:「Link 2 (eth0): 192.0.2.53 192.0.2.54」
            dns = ipv4_strings(completed.stdout.split("):", 1)[-1].split())
    except (OSError, subprocess.TimeoutExpired):
        pass
    return NetworkState(interface=dev, address=address, gateway=gateway, dns=tuple(dns), dhcp=dhcp)


def write_json_atomic(path: Path, data: Any, mode: int = 0o644) -> None:
    """書きかけを読まれないよう、別の名前で書いてから置き換える。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temp, mode)
    os.replace(temp, path)


def write_state(base_dir: Path, runner: Runner) -> None:
    """本体のネットワークの状態を、web が読む場所に書き出す。"""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        data: dict[str, Any] = {**read_network_state(runner).to_dict(), "updated_at": now}
    except HostCtlError as exc:
        data = {"error": str(exc), "updated_at": now}
    write_json_atomic(base_dir / HOSTCTL_STATE / HOSTCTL_NETWORK_STATE_FILE, data)


# ======================================================================
# netplan の設定ファイルの書き換え(反映は再起動のとき)
# ======================================================================


def _netdef_matches(key: str, netdef: Any, dev: str) -> bool:
    if key == dev:
        return True
    if not isinstance(netdef, dict):
        return False
    match = netdef.get("match")
    return netdef.get("set-name") == dev or (isinstance(match, dict) and match.get("name") == dev)


def _deep_merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _is_ipv6_entry(value: Any) -> bool:
    text = next(iter(value), "") if isinstance(value, dict) else value
    return isinstance(text, str) and ":" in text


def _is_ipv4_default_route(route: Any) -> bool:
    if not isinstance(route, dict):
        return False
    return route.get("to") in ("default", "0.0.0.0/0") and ":" not in str(route.get("via", ""))


def build_netdef(original: dict[str, Any], config: NetworkConfig) -> dict[str, Any]:
    """LAN端子1本分の設定を作る。IPv4のアドレス、デフォルトゲートウェイ、DNSサーバーだけを変え、
    それ以外(IPv6の設定、名前の決め方など)は元のまま残す。"""
    netdef = copy.deepcopy(original)
    netdef.pop("gateway4", None)  # 古い書き方。routes に置き換える
    netdef["dhcp4"] = False
    netdef["addresses"] = [config.address] + [a for a in original.get("addresses", []) or [] if _is_ipv6_entry(a)]
    routes = [r for r in original.get("routes", []) or [] if not _is_ipv4_default_route(r)]
    netdef["routes"] = routes + [{"to": "default", "via": config.gateway}]
    nameservers = dict(original.get("nameservers") or {})
    nameservers["addresses"] = list(config.dns) + [a for a in nameservers.get("addresses", []) or [] if _is_ipv6_entry(a)]
    netdef["nameservers"] = nameservers
    return netdef


def plan_netplan_change(
    files: dict[str, Any], dev: str, config: NetworkConfig, own_name: str = HOSTCTL_NETPLAN_FILE
) -> dict[str, Any | None]:
    """netplan の設定ファイルをどう変えるかを決める(ファイルには書かない)。

    files は「ファイル名 → 読み込んだ中身」。戻り値は「ファイル名 → 新しい中身(None は削除)」で、
    変えるファイルだけを含む。netplan は複数のファイルの設定を重ね合わせるため、対象のLAN端子の設定は
    自分のファイル(own_name)だけに書き、ほかのファイルからは取り除く(ほかのLAN端子の設定は残す)。
    """
    merged: dict[str, Any] = {}
    netdef_key: str | None = None
    holders: list[str] = []
    for name in sorted(files):
        data = files[name]
        if data is None:
            continue
        network = data.get("network") if isinstance(data, dict) else None
        if not isinstance(network, dict):
            raise HostCtlError(f"netplan の設定ファイル {name} の形が正しくないため、変更できません")
        for section in NETPLAN_OTHER_SECTIONS:
            entries = network.get(section)
            if isinstance(entries, dict) and any(_netdef_matches(k, v, dev) for k, v in entries.items()):
                raise HostCtlError(
                    f"LAN端子 {dev} は、自動で変更できない形({section})で設定されています。"
                    "コンソールで、Ubuntuの標準の手順で変更してください"
                )
        ethernets = network.get(NETPLAN_ETHERNETS)
        if not isinstance(ethernets, dict):
            continue
        for key, netdef in ethernets.items():
            if _netdef_matches(str(key), netdef, dev):
                if netdef_key is not None and key != netdef_key:
                    raise HostCtlError(f"LAN端子 {dev} の設定が、別々の名前で複数あるため、変更できません")
                netdef_key = str(key)
                merged = _deep_merge(merged, netdef if isinstance(netdef, dict) else {})
                if name not in holders:
                    holders.append(name)

    key = netdef_key or dev
    plan: dict[str, Any | None] = {
        own_name: {"network": {"version": 2, NETPLAN_ETHERNETS: {key: build_netdef(merged, config)}}}
    }
    for name in holders:
        if name == own_name:
            continue
        data = copy.deepcopy(files[name])
        network = data["network"]
        network[NETPLAN_ETHERNETS].pop(key, None)
        if not network[NETPLAN_ETHERNETS]:
            del network[NETPLAN_ETHERNETS]
        # LAN端子の設定が何も残らないファイルは、削除する(控えは残す)
        plan[name] = None if set(network) <= {"version"} else data
    return plan


@dataclass
class NetplanPaths:
    netplan_dir: Path = Path(NETPLAN_DIR)
    state_dir: Path = Path(HOSTCTL_STATE_DIR)
    cloud_init_dir: Path = Path(CLOUD_INIT_CFG_DIR)


def _write_private(path: Path, text: str) -> None:
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(text, encoding="utf-8")
    os.chmod(temp, 0o600)  # netplan の設定ファイルは、管理者だけが読める形にする
    os.replace(temp, path)


def write_netplan(config: NetworkConfig, runner: Runner, yaml_io: YamlIO, paths: NetplanPaths) -> str:
    """本体のIPアドレスの設定ファイルを書き換える。反映は、次の再起動(または netplan apply)のとき。

    書き換えの前に、元のファイルの控えを残す。書き換えた設定を netplan が読めなかった場合は、
    元に戻して HostCtlError を出す。対象のLAN端子の名前を返す。
    """
    dev, _ = default_route(runner)
    originals: dict[str, str] = {}
    files: dict[str, Any] = {}
    for path in sorted(paths.netplan_dir.glob("*.yaml")):
        text = path.read_text(encoding="utf-8")
        originals[path.name] = text
        try:
            files[path.name] = yaml_io.load(text)
        except Exception:  # noqa: BLE001 PyYAML の読み込みの誤り
            raise HostCtlError(f"netplan の設定ファイル {path.name} を読み取れないため、変更できません") from None
    plan = plan_netplan_change(files, dev, config)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = paths.state_dir / NETPLAN_BACKUP_DIR / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, 0o700)
    for name in plan:
        if name in originals:
            _write_private(backup_dir / name, originals[name])

    try:
        for name, data in plan.items():
            target = paths.netplan_dir / name
            if data is None:
                target.unlink(missing_ok=True)
            else:
                header = NETPLAN_HEADER if name == HOSTCTL_NETPLAN_FILE else ""
                _write_private(target, header + yaml_io.dump(data))
        _run_checked(runner, ["netplan", "generate"], "書き換えた設定の確認(netplan generate)")
    except Exception:
        # 1つでも失敗したら、すべて元に戻す
        for name in plan:
            target = paths.netplan_dir / name
            if name in originals:
                _write_private(target, originals[name])
            else:
                target.unlink(missing_ok=True)
        raise

    if paths.cloud_init_dir.is_dir():
        disable_file = paths.cloud_init_dir / CLOUD_INIT_DISABLE_FILE
        if not disable_file.exists():
            disable_file.write_text(CLOUD_INIT_DISABLE_TEXT, encoding="utf-8")
    return dev


# ======================================================================
# ファイアウォール(ufw)(決定事項 D-030)
# ======================================================================


@dataclass(frozen=True)
class Ports:
    webgui_port: int
    telemetry_udp_port: int

    def rules(self) -> set[str]:
        # 既定値の8080は、保存されたポート番号が使えなかったときに WebGUI が使うため、常に開けておく(D-029)
        return {f"{self.webgui_port}/tcp", f"{WEBGUI_FALLBACK_PORT}/tcp", f"{self.telemetry_udp_port}/udp"}


def read_ports(db_path: Path) -> Ports:
    """保存された設定のポート番号を読む。データベースがない(導入の直後)ときは既定値。"""
    values = {key: SETTING_DEFAULTS[key] for key in (SETTING_WEBGUI_PORT, SETTING_TELEMETRY_UDP_PORT)}
    if db_path.exists():
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=SQLITE_BUSY_TIMEOUT_SEC)
            try:
                rows = conn.execute(
                    "SELECT key, value FROM settings WHERE key IN (?, ?)", (SETTING_WEBGUI_PORT, SETTING_TELEMETRY_UDP_PORT)
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.Error as exc:
            raise HostCtlError(f"設定のデータベースを読み取れませんでした({exc})") from None
        values.update({str(key): str(value) for key, value in rows})
    result: dict[str, int] = {}
    for key, text in values.items():
        try:
            result[key] = check_listen_port(int(text))
        except ValueError:
            result[key] = int(SETTING_DEFAULTS[key])  # 不正な値は既定値として扱う
    return Ports(result[SETTING_WEBGUI_PORT], result[SETTING_TELEMETRY_UDP_PORT])


def ufw_active(runner: Runner) -> bool:
    """ファイアウォール(ufw)が有効なら真。ufw がなければ偽。"""
    try:
        completed = runner(["ufw", "status"])
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0 and "Status: active" in completed.stdout


def _load_rules(state_dir: Path) -> set[str]:
    try:
        data = json.loads((state_dir / FIREWALL_STATE_FILE).read_text(encoding="utf-8"))
        return {rule for rule in data.get("rules", []) if isinstance(rule, str)}
    except (OSError, json.JSONDecodeError, AttributeError):
        return set()


def sync_firewall(runner: Runner, state_dir: Path, wanted: set[str], *, close_old: bool) -> str:
    """ファイアウォールが有効なときだけ、必要なポート番号を開け、close_old なら以前に開けた不要な番号を閉じる。

    閉じるのは、このプログラムが開けた番号だけとする(記録を state_dir に残す)。
    """
    if not ufw_active(runner):
        return "ファイアウォール(ufw)は無効のため、何もしません"
    opened = _load_rules(state_dir)
    for rule in sorted(wanted):
        _run_checked(runner, ["ufw", "allow", rule, "comment", "telemetry"], f"ポート {rule} の開放")
    closed: list[str] = []
    if close_old:
        for rule in sorted(opened - wanted):
            _run_checked(runner, ["ufw", "delete", "allow", rule], f"ポート {rule} の閉鎖")
            closed.append(rule)
    write_json_atomic(state_dir / FIREWALL_STATE_FILE, {"rules": sorted(wanted if close_old else opened | wanted)}, 0o600)
    message = "開けたポート:" + "、".join(sorted(wanted))
    return message + (f"。閉じたポート:{'、'.join(closed)}" if closed else "")


# ======================================================================
# 依頼の処理
# ======================================================================


@dataclass
class HostCtl:
    """依頼の処理に必要なものの入れ物(テストでは差し替える)。"""

    base_dir: Path = Path(HOSTCTL_RUN_DIR)
    db_path: Path = Path(DEFAULT_DB_PATH)
    paths: NetplanPaths = field(default_factory=NetplanPaths)
    runner: Runner = run_command
    yaml_io_factory: Callable[[], YamlIO] = pyyaml_io
    clock: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    log: Callable[[str], None] = lambda message: print(message, flush=True)

    def handle(self, request: HostRequest) -> HostResult:
        if abs(self.clock() - request.created_at) > HOSTCTL_REQUEST_MAX_AGE_SEC:
            return HostResult(request.id, False, "依頼が古いため、処理しませんでした")
        try:
            if request.action == ACTION_SYNC_FIREWALL:
                ports = read_ports(self.db_path)
                message = sync_firewall(self.runner, self.paths.state_dir, ports.rules(), close_old=True)
                return HostResult(request.id, True, message)
            # 再起動の依頼:先にファイアウォールで新しいポート番号を開け(閉じるのは再起動のあと)、
            # 次に IPアドレスの設定ファイルを書き換える。どちらかが失敗したら、再起動しない
            messages: list[str] = []
            if request.webgui_port is not None:
                current = read_ports(self.db_path)
                wanted = Ports(request.webgui_port, current.telemetry_udp_port).rules()
                messages.append(sync_firewall(self.runner, self.paths.state_dir, wanted, close_old=False))
            if request.network is not None:
                dev = write_netplan(request.network, self.runner, self.yaml_io_factory(), self.paths)
                messages.append(f"LAN端子 {dev} のIPアドレスの設定を書き換えました")
            return HostResult(request.id, True, "。".join(messages) + "。再起動します")
        except HostCtlError as exc:
            return HostResult(request.id, False, str(exc))

    def _read_request_file(self, path: Path) -> bytes:
        # 依頼のフォルダは web コンテナが書けるため、別の場所への近道(シンボリックリンク)は開かない
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            return os.read(fd, HOSTCTL_MAX_REQUEST_BYTES + 1)
        finally:
            os.close(fd)

    def process_inbox(self) -> None:
        """依頼をすべて処理する。依頼のファイルは、正しくないものも含めて必ず消す。"""
        inbox = self.base_dir / HOSTCTL_INBOX
        results = self.base_dir / HOSTCTL_RESULTS
        reboot = False
        for path in sorted(inbox.iterdir()) if inbox.is_dir() else []:
            name = path.name
            try:
                raw = self._read_request_file(path) if path.is_file() and not path.is_symlink() else b""
            except OSError:
                raw = b""
            finally:
                self._discard(path)
            request_id = name[: -len(".json")] if name.endswith(".json") else ""
            try:
                request = parse_request(raw)
                if request.id != request_id:
                    raise RequestError("依頼のファイル名と、依頼の番号が一致しません")
            except RequestError as exc:
                self.log(f"正しくない依頼を捨てました({name[:40]}:{exc})")
                if is_valid_request_id(request_id):
                    write_json_atomic(results / f"{request_id}.json", json.loads(HostResult(request_id, False, str(exc)).to_json_bytes()))
                continue
            result = self.handle(request)
            self.log(f"依頼 {request.id}({request.action}):{'成功' if result.ok else '失敗'}:{result.message}")
            write_json_atomic(results / f"{request.id}.json", json.loads(result.to_json_bytes()))
            if result.ok and request.action == ACTION_REBOOT:
                reboot = True
        self._clean_results(results)
        if reboot:
            self.sleep(HOSTCTL_REBOOT_DELAY_SEC)  # web が画面を返し終えるのを待つ
            self.log("本体を再起動します")
            _run_checked(self.runner, REBOOT_COMMAND, "本体の再起動")

    def _discard(self, path: Path) -> None:
        """依頼のフォルダの中のものを消す(フォルダは中が空のときだけ消せる)。"""
        try:
            if path.is_dir() and not path.is_symlink():
                path.rmdir()
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            self.log(f"依頼のフォルダの中の {path.name[:40]} を消せませんでした({exc})")

    def _clean_results(self, results: Path) -> None:
        if not results.is_dir():
            return
        limit = self.clock() - HOSTCTL_RESULT_TTL_SEC
        for path in results.iterdir():
            try:
                if path.stat().st_mtime < limit:
                    path.unlink()
            except OSError:
                continue


# ======================================================================
# コマンドとしての入口
# ======================================================================


def _format_state(state: NetworkState) -> str:
    return "\n".join(
        [
            f"  LAN端子              : {state.interface}",
            f"  IPアドレス/プレフィックス長: {state.address or '(不明)'}{'(自動取得 DHCP)' if state.dhcp else ''}",
            f"  デフォルトゲートウェイ   : {state.gateway}",
            f"  DNSサーバー           : {' '.join(state.dns) or '(不明)'}",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    """コマンドの引数の読み取り方。全体の指定(--db など)は、処理の名前(firewall-sync など)より前に書く。"""
    parser = argparse.ArgumentParser(prog="telemetry_hostctl", description="Telemetry 中間サーバーの、本体側の補助プログラム")
    parser.add_argument("--base-dir", default=HOSTCTL_RUN_DIR, help="依頼をやり取りするフォルダ")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help="設定のデータベース")
    parser.add_argument("--state-dir", default=HOSTCTL_STATE_DIR, help="このプログラムの記録を置くフォルダ")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("process", help="依頼を処理する")
    sub.add_parser("boot", help="起動時の処理")
    sub.add_parser("state", help="本体のネットワークの状態を書き出す")
    sub.add_parser("firewall-sync", help="ファイアウォールを、保存された設定のポート番号に合わせる")
    sub.add_parser("show-ports", help="保存された設定のポート番号を表示する")
    show = sub.add_parser("show-network", help="本体のネットワークの状態を表示する")
    show.add_argument("--field", choices=("interface", "address", "ip", "gateway", "dns"))
    setnet = sub.add_parser("set-network", help="IPアドレスの設定ファイルを書き換える(反映はしない)")
    setnet.add_argument("--address", required=True)
    setnet.add_argument("--gateway", required=True)
    setnet.add_argument("--dns", nargs="*", default=[], help="DNSサーバー(1つか2つ)")
    setnet.add_argument("--check-only", action="store_true", help="入力の検査だけを行う")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    ctl = HostCtl(
        base_dir=Path(args.base_dir),
        db_path=Path(args.db),
        paths=NetplanPaths(state_dir=Path(args.state_dir)),
    )
    try:
        if args.command == "process":
            ctl.process_inbox()
        elif args.command == "boot":
            write_state(ctl.base_dir, ctl.runner)
            print(sync_firewall(ctl.runner, ctl.paths.state_dir, read_ports(ctl.db_path).rules(), close_old=True))
        elif args.command == "state":
            write_state(ctl.base_dir, ctl.runner)
        elif args.command == "firewall-sync":
            print(sync_firewall(ctl.runner, ctl.paths.state_dir, read_ports(ctl.db_path).rules(), close_old=True))
        elif args.command == "show-ports":
            ports = read_ports(ctl.db_path)
            print(f"{ports.webgui_port} {ports.telemetry_udp_port}")
        elif args.command == "show-network":
            state = read_network_state(ctl.runner)
            if args.field is None:
                print(_format_state(state))
            elif args.field == "ip":
                print(state.address.split("/", 1)[0])
            elif args.field == "dns":
                print(" ".join(state.dns))
            else:
                print(getattr(state, args.field))
        elif args.command == "set-network":
            try:
                config = validate_network(args.address, args.gateway, args.dns)
            except NetworkInputError as exc:
                for message in exc.messages():
                    print(f"  入力の誤り:{message}", file=sys.stderr)
                return 2
            if args.check_only:
                print(f"{config.address} {config.gateway} {' '.join(config.dns)}")
                return 0
            dev = write_netplan(config, ctl.runner, ctl.yaml_io_factory(), ctl.paths)
            print(f"LAN端子 {dev} のIPアドレスの設定ファイルを書き換えました")
    except HostCtlError as exc:
        print(f"エラー:{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
