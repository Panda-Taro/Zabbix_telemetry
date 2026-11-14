#!/usr/bin/env python3
"""模擬送信ツール(要件定義書 12.1.2、DL-08)。

Cisco Nexus の代わりに、Telemetry(UDP・JSON)を中間サーバーへ送る検証用のツールです。
tests/fixtures/ の4つのJSON(架空の値に置き換えたテストデータ)をもとに送ります。
Python 3 の標準の機能だけで動きます(追加のライブラリは不要です)。

使い方の例:
  # 1台分を、10秒ごとに送り続ける(止めるときは Ctrl + C)
  python3 tools/telemetry_simulator.py --target 192.0.2.100

  # 1回だけ送る
  python3 tools/telemetry_simulator.py --target 192.0.2.100 --count 1

  # 50台分(NEXUS-TEST-01〜50、送信元 192.0.2.11〜192.0.2.60)を、10秒ごとに送る
  python3 tools/telemetry_simulator.py --target 192.0.2.100 --devices 50 \\
      --source-ips 192.0.2.11-192.0.2.60

  # 壊れたデータを送る
  python3 tools/telemetry_simulator.py --target 192.0.2.100 --count 1 --abnormal broken

詳しくは docs/模擬送信ツール.md を見てください。
"""

from __future__ import annotations

import argparse
import copy
import ipaddress
import json
import random
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---- 既定値 ----
DEFAULT_PORT = 50051
DEFAULT_INTERVAL_SEC = 10.0
DEFAULT_HOSTNAME_FORMAT = "NEXUS-TEST-{n:02d}"
DEFAULT_FIXTURES_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures"

# 4つのコマンドと、テストデータのファイル名
FIXTURE_FILES: dict[str, str] = {
    "show interface counters brief": "show_interface_counters_brief.json",
    "show ptp brief": "show_ptp_brief.json",
    "show ptp clock": "show_ptp_clock.json",
    "show ptp parent": "show_ptp_parent.json",
}
# --commands で使う短い名前
COMMAND_ALIASES: dict[str, str] = {
    "counters": "show interface counters brief",
    "ptp-brief": "show ptp brief",
    "ptp-clock": "show ptp clock",
    "ptp-parent": "show ptp parent",
}

# NX-OS がJSONの前に付けるヘッダーの代わり(中身に意味はない12バイト)
FAKE_HEADER = bytes([0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x2A, 0x00, 0x10, 0x7F, 0xFF])

# 異常なデータの種類
ABNORMAL_KINDS: dict[str, str] = {
    "broken": "壊れたデータ(JSONの途中で切れている)",
    "unknown-command": "4つ以外のコマンド(encoding_path が「show ptp clock detail」など)",
    "missing-values": "値が欠けたデータ(clock-state、gm-id、state、eth_inrate1 などの項目がない)",
    "na-values": "数値として読めない値(offset-from-master、eth_inrate1 などが「N/A」)",
}
# 4つ以外のコマンドとして送る encoding_path(元のコマンド → 送る名前)
UNKNOWN_COMMAND_PATHS: dict[str, str] = {
    "show interface counters brief": "show interface counters",
    "show ptp brief": "show ptp brief detail",
    "show ptp clock": "show ptp clock detail",
    "show ptp parent": "show nbm interface bandwidth-utilized",
}


class SimulatorError(Exception):
    """利用者に伝えるべき誤り(指定の誤りなど)。"""


# ======================================================================
# 送るデータの組み立て
# ======================================================================


def load_fixtures(fixtures_dir: Path) -> dict[str, dict[str, Any]]:
    """テストデータの4つのJSONを読み込む。"""
    documents: dict[str, dict[str, Any]] = {}
    for command, file_name in FIXTURE_FILES.items():
        path = fixtures_dir / file_name
        try:
            documents[command] = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise SimulatorError(f"テストデータが見つかりません:{path}") from None
        except json.JSONDecodeError as exc:
            raise SimulatorError(f"テストデータがJSONとして読めません:{path}({exc})") from None
    return documents


def _rows(data: dict[str, Any], table: str, row: str) -> list[dict[str, Any]]:
    rows = data[table][row]
    return rows if isinstance(rows, list) else [rows]


def expand_ports(document: dict[str, Any], command: str, ports: int) -> None:
    """インターフェースごとのコマンドの行を、Ethernet1/1〜1/<ports> の数にそろえる。

    show interface counters brief と show ptp brief だけが対象。1行目を手本にして行を作る。
    """
    data = document["data"]
    if command == "show interface counters brief":
        rows = _rows(data, "TABLE_interface", "ROW_interface")
        template = rows[0]
        by_name = {r.get("interface"): r for r in rows}
        new_rows = []
        for port in range(1, ports + 1):
            name = f"Ethernet1/{port}"
            new_rows.append(copy.deepcopy(by_name.get(name, {**template, "interface": name})))
        data["TABLE_interface"]["ROW_interface"] = new_rows
    elif command == "show ptp brief":
        rows = _rows(data, "TABLE_ptp", "ROW_ptp")
        template = rows[0]
        by_name = {r.get("ptp-ifindex"): r for r in rows}
        new_rows = []
        for port in range(1, ports + 1):
            name = f"Eth1/{port}"
            new_rows.append(copy.deepcopy(by_name.get(name, {**template, "ptp-ifindex": name, "state": "master"})))
        data["TABLE_ptp"]["ROW_ptp"] = new_rows


def make_missing_values(document: dict[str, Any], command: str) -> None:
    """値が欠けたデータにする(要件定義書 9.1.7.4 の確認用)。"""
    data = document["data"]
    if command == "show ptp clock":
        data.pop("clock-state", None)
        data.pop("offset-from-master", None)
    elif command == "show ptp parent":
        data.pop("gm-id", None)
    elif command == "show ptp brief":
        for row in _rows(data, "TABLE_ptp", "ROW_ptp"):
            row.pop("state", None)
    elif command == "show interface counters brief":
        for row in _rows(data, "TABLE_interface", "ROW_interface"):
            row.pop("eth_inrate1", None)
            row.pop("eth_outrate1", None)


def make_na_values(document: dict[str, Any], command: str) -> None:
    """数値として読めない値(N/A)にする(要件定義書 9.1.7.5 の確認用)。"""
    data = document["data"]
    if command == "show ptp clock":
        data["offset-from-master"] = "N/A"
    elif command == "show interface counters brief":
        for row in _rows(data, "TABLE_interface", "ROW_interface"):
            row["eth_inrate1"] = "N/A"
            row["eth_outrate1"] = "N/A"


@dataclass
class DeviceSpec:
    """模擬する機器1台分。"""

    hostname: str
    source_ip: str | None
    """送信元IPアドレス。None のときは、OSにまかせる。"""
    next_send: float = 0.0
    collection_id: int = 1000
    rounds: int = 0
    """送った回数(1回で、指定したコマンドの数だけパケットを送る)。"""
    sent: int = 0
    errors: int = 0
    sock: socket.socket | None = field(default=None, repr=False)


def build_payload(
    base: dict[str, Any],
    command: str,
    hostname: str,
    collection_id: int,
    abnormal: str | None,
    header: bool,
    now_ms: int,
) -> bytes:
    """1回分(1パケット分)のデータを組み立てる。"""
    document = copy.deepcopy(base)
    document["node_id_str"] = hostname
    document["collection_id"] = collection_id
    document["collection_start_time"] = str(now_ms - 100)
    document["collection_end_time"] = str(now_ms)
    document["msg_timestamp"] = str(now_ms)
    if abnormal == "unknown-command":
        document["encoding_path"] = UNKNOWN_COMMAND_PATHS[command]
    elif abnormal == "missing-values":
        make_missing_values(document, command)
    elif abnormal == "na-values":
        make_na_values(document, command)

    body = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if abnormal == "broken":
        body = body[: max(1, len(body) // 2)]  # JSONの途中で切る
    return (FAKE_HEADER if header else b"") + body


# ======================================================================
# 指定の読み取り
# ======================================================================


def parse_source_ips(text: str) -> list[str]:
    """送信元IPアドレスの指定を読み取る。

    受け付ける形:
      192.0.2.11                     1つ
      192.0.2.11,192.0.2.12          カンマ区切り
      192.0.2.11-192.0.2.60          範囲
      192.0.2.11-60                  範囲(最後の数字だけを書く形)
    """
    result: list[str] = []
    for item in [part.strip() for part in text.split(",") if part.strip()]:
        try:
            if "-" in item:
                start_text, end_text = (s.strip() for s in item.split("-", 1))
                start = ipaddress.IPv4Address(start_text)
                if "." not in end_text:
                    end_text = start_text.rsplit(".", 1)[0] + "." + end_text
                end = ipaddress.IPv4Address(end_text)
                if end < start:
                    raise SimulatorError(f"送信元IPアドレスの範囲が逆順です:{item}")
                result.extend(str(ipaddress.IPv4Address(n)) for n in range(int(start), int(end) + 1))
            else:
                result.append(str(ipaddress.IPv4Address(item)))
        except ipaddress.AddressValueError:
            raise SimulatorError(f"送信元IPアドレスの形式が正しくありません:{item}") from None
    return result


def make_hostnames(args: argparse.Namespace) -> list[str]:
    if args.hostnames:
        names = [n.strip() for n in args.hostnames.split(",") if n.strip()]
        if len(names) != args.devices:
            raise SimulatorError(f"--hostnames の数({len(names)})が、--devices の台数({args.devices})と合いません")
        return names
    try:
        return [args.hostname_format.format(n=args.start_number + i) for i in range(args.devices)]
    except (KeyError, IndexError, ValueError):
        raise SimulatorError("--hostname-format の書き方が正しくありません(例:NEXUS-TEST-{n:02d})") from None


def make_devices(args: argparse.Namespace) -> list[DeviceSpec]:
    hostnames = make_hostnames(args)
    sources: list[str | None]
    if args.source_ips:
        ips = parse_source_ips(args.source_ips)
        if len(ips) < len(hostnames):
            raise SimulatorError(
                f"送信元IPアドレスの数({len(ips)})が、台数({len(hostnames)})より少なくなっています"
            )
        sources = list(ips[: len(hostnames)])
    else:
        sources = [None] * len(hostnames)
    return [DeviceSpec(hostname=h, source_ip=s) for h, s in zip(hostnames, sources)]


def parse_commands(text: str) -> list[str]:
    commands = []
    for item in [part.strip() for part in text.split(",") if part.strip()]:
        if item not in COMMAND_ALIASES:
            raise SimulatorError(f"--commands に指定できるのは {', '.join(COMMAND_ALIASES)} です:{item}")
        commands.append(COMMAND_ALIASES[item])
    if not commands:
        raise SimulatorError("--commands に、1つ以上のコマンドを指定してください")
    return commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="telemetry_simulator.py",
        description="Cisco Nexus の代わりに、Telemetry(UDP・JSON)を中間サーバーへ送る模擬送信ツールです。",
        epilog="詳しい使い方は docs/模擬送信ツール.md を見てください。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", required=True, help="送信先(中間サーバー)のIPアドレス。例:192.0.2.100")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"送信先のUDPポート番号(既定値:{DEFAULT_PORT})")
    parser.add_argument("--devices", type=int, default=1, help="模擬する台数(既定値:1)")
    parser.add_argument(
        "--hostname-format",
        default=DEFAULT_HOSTNAME_FORMAT,
        help="ホスト名の付け方。{n} に番号が入る(既定値:NEXUS-TEST-{n:02d} → NEXUS-TEST-01、02 …)",
    )
    parser.add_argument("--start-number", type=int, default=1, help="ホスト名の番号の始まり(既定値:1)")
    parser.add_argument("--hostnames", help="ホスト名をカンマ区切りで直接指定する。例:NEXUS-TEST-01,NEXUS-TEST-02")
    parser.add_argument(
        "--source-ips",
        help="機器ごとの送信元IPアドレス。例:192.0.2.11-192.0.2.60 または 192.0.2.11,192.0.2.12"
        "(指定しない場合は、このPCのIPアドレスから送る)",
    )
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SEC, help="1台あたりの送信間隔(秒)(既定値:10)")
    parser.add_argument("--count", type=int, default=0, help="1台あたりに送る回数。0 は止めるまで送り続ける(既定値:0)")
    parser.add_argument("--duration", type=float, default=0, help="送り続ける時間(秒)。0 は制限なし(既定値:0)")
    parser.add_argument(
        "--commands",
        default=",".join(COMMAND_ALIASES),
        help="送るコマンド。counters, ptp-brief, ptp-clock, ptp-parent をカンマ区切りで指定(既定値:4つすべて)",
    )
    parser.add_argument(
        "--ports",
        type=int,
        default=0,
        help="show interface counters brief と show ptp brief の行を、Ethernet1/1〜1/<数> にそろえる(既定値:テストデータのまま)",
    )
    parser.add_argument(
        "--abnormal",
        choices=list(ABNORMAL_KINDS),
        help="異常なデータを送る。" + " / ".join(f"{k}:{v}" for k, v in ABNORMAL_KINDS.items()),
    )
    parser.add_argument("--no-header", action="store_true", help="NX-OS のヘッダーの代わりのバイト列を付けない")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES_DIR, help="テストデータのフォルダ")
    parser.add_argument("--quiet", action="store_true", help="1回ごとの表示をしない")
    return parser


# ======================================================================
# 送信
# ======================================================================


def open_sockets(devices: list[DeviceSpec]) -> None:
    """送信元IPアドレスごとに、送信用の口を開く。"""
    by_source: dict[str | None, socket.socket] = {}
    for device in devices:
        sock = by_source.get(device.source_ip)
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if device.source_ip is not None:
                try:
                    sock.bind((device.source_ip, 0))
                except OSError as exc:
                    sock.close()
                    for opened in by_source.values():
                        opened.close()
                    raise SimulatorError(
                        f"送信元IPアドレス {device.source_ip} から送れません({exc})。"
                        "このPCにそのIPアドレスが設定されているか確かめてください"
                    ) from None
            by_source[device.source_ip] = sock
        device.sock = sock


def close_sockets(devices: list[DeviceSpec]) -> None:
    for sock in {id(d.sock): d.sock for d in devices if d.sock is not None}.values():
        sock.close()


def run(args: argparse.Namespace, out=sys.stdout) -> dict[str, int]:  # noqa: ANN001
    """指定どおりに送る。送ったパケット数などを返す。"""
    if args.devices < 1:
        raise SimulatorError("--devices は1以上にしてください")
    if args.interval <= 0:
        raise SimulatorError("--interval は0より大きくしてください")
    if not 1 <= args.port <= 65535:
        raise SimulatorError("--port は 1〜65535 にしてください")
    if args.ports < 0 or args.ports > 256:
        raise SimulatorError("--ports は 0〜256 にしてください")

    commands = parse_commands(args.commands)
    fixtures = load_fixtures(args.fixtures)
    bases = {command: copy.deepcopy(fixtures[command]) for command in commands}
    if args.ports:
        for command, document in bases.items():
            expand_ports(document, command, args.ports)
    devices = make_devices(args)
    if len(devices) > 1 and devices[0].source_ip is None:
        print(
            "注意:--source-ips を指定していないため、全台が同じ送信元IPアドレスから送られます。"
            "中間サーバーは送信元IPアドレスで機器を見分けるため、受け付けられるのは1台分だけです。",
            file=out,
        )

    target = (args.target, args.port)
    print(
        f"送信を開始します:送信先 {args.target}:{args.port}、{len(devices)} 台、"
        f"{len(commands)} コマンド、間隔 {args.interval} 秒"
        + (f"、異常なデータ({ABNORMAL_KINDS[args.abnormal]})" if args.abnormal else ""),
        file=out,
    )
    open_sockets(devices)
    started = time.monotonic()
    # 全台が同じ瞬間に送らないよう、送り始めを間隔の中で散らす
    for index, device in enumerate(devices):
        device.next_send = started + args.interval * index / len(devices)
        device.collection_id = random.randint(1000, 9000)

    rounds_done = 0
    try:
        while True:
            pending = [d for d in devices if args.count == 0 or d.rounds < args.count]
            if not pending:
                break
            if args.duration and time.monotonic() - started >= args.duration:
                break
            device = min(pending, key=lambda d: d.next_send)
            wait = device.next_send - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            now_ms = int(time.time() * 1000)
            for command in commands:
                payload = build_payload(
                    bases[command], command, device.hostname, device.collection_id, args.abnormal, not args.no_header, now_ms
                )
                device.collection_id += 1
                try:
                    device.sock.sendto(payload, target)  # type: ignore[union-attr]
                    device.sent += 1
                except OSError as exc:
                    device.errors += 1
                    print(f"送信できませんでした({device.hostname}):{exc}", file=out)
            device.rounds += 1
            device.next_send += args.interval
            if device is devices[-1]:
                rounds_done += 1
                if not args.quiet:
                    total = sum(d.sent for d in devices)
                    print(f"{time.strftime('%H:%M:%S')} {rounds_done} 回目を送りました(合計 {total} パケット)", file=out)
    except KeyboardInterrupt:
        print("止めました。", file=out)
    finally:
        close_sockets(devices)

    summary = {"sent": sum(d.sent for d in devices), "errors": sum(d.errors for d in devices)}
    print(f"送信を終えました:合計 {summary['sent']} パケット、送信できなかったもの {summary['errors']} 件", file=out)
    return summary


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run(args)
    except SimulatorError as exc:
        print(f"エラー:{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
