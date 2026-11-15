"""本体側の補助プログラム(tools/telemetry_hostctl.py)の自動テストで使う部品。

本体のコマンド(ip、resolvectl、ufw、netplan、systemctl)は実行せず、決まった結果を返す偽物に差し替える。
netplan の設定ファイルの読み書きには、PyYAML の代わりに JSON を使う(JSON は YAML としても正しい形)。
本体の再起動は、実行されたコマンドの記録で確かめる(実際には再起動しない)。
"""

import json
import subprocess
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

from common.constants import HOSTCTL_INBOX, HOSTCTL_RESULTS, HOSTCTL_SPOOL, HOSTCTL_STATE
from tools.telemetry_hostctl import HostCtl, NetplanPaths, YamlIO

DEV = "eth0"
CURRENT_ADDRESS = "192.0.2.10/24"
CURRENT_GATEWAY = "192.0.2.1"
CURRENT_DNS = "192.0.2.53"


class FakeRunner:
    """本体のコマンドの偽物。実行されたコマンドを commands に記録する。"""

    def __init__(self, *, ufw_active: bool = True, netplan_ok: bool = True, has_default_route: bool = True) -> None:
        self.ufw_active = ufw_active
        self.netplan_ok = netplan_ok
        self.has_default_route = has_default_route
        self.commands: list[tuple[str, ...]] = []
        self.lock = threading.Lock()

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        cmd = tuple(command)
        with self.lock:
            self.commands.append(cmd)

        def done(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(list(cmd), returncode, stdout, stderr)

        if cmd == ("ip", "-j", "-4", "route", "show", "default"):
            routes = [{"dst": "default", "gateway": CURRENT_GATEWAY, "dev": DEV}] if self.has_default_route else []
            return done(json.dumps(routes))
        if cmd == ("ip", "-j", "-4", "addr", "show", "dev", DEV):
            ip, prefix = CURRENT_ADDRESS.split("/")
            return done(json.dumps([{"ifname": DEV, "addr_info": [{"family": "inet", "local": ip, "prefixlen": int(prefix)}]}]))
        if cmd == ("resolvectl", "dns", DEV):
            return done(f"Link 2 ({DEV}): {CURRENT_DNS}\n")
        if cmd == ("ufw", "status"):
            return done("Status: active\n" if self.ufw_active else "Status: inactive\n")
        if cmd == ("netplan", "generate"):
            return done() if self.netplan_ok else done(returncode=1, stderr="Error in network definition")
        if cmd[:2] in {("ufw", "allow"), ("ufw", "delete")} or cmd == ("systemctl", "reboot"):
            return done()
        raise AssertionError(f"想定していないコマンドです:{cmd}")

    def ran(self, *command: str) -> bool:
        with self.lock:
            return tuple(command) in self.commands

    @property
    def rebooted(self) -> bool:
        return self.ran("systemctl", "reboot")


def _load(text: str) -> object:
    return json.loads("\n".join(line for line in text.splitlines() if not line.startswith("#")))


JSON_YAML = YamlIO(load=_load, dump=lambda data: json.dumps(data, ensure_ascii=False, indent=2) + "\n")

# Ubuntu の導入時に作られる設定ファイルの例(自動取得 DHCP)
INSTALLER_NETPLAN = {"network": {"version": 2, "ethernets": {DEV: {"dhcp4": True}}}}


def make_hostctl_dirs(root: Path) -> Path:
    """補助プログラムとやり取りするフォルダ(本体の /run/telemetry-hostctl の代わり)を作る。"""
    base = root / "hostctl"
    for name in (HOSTCTL_SPOOL, HOSTCTL_INBOX, HOSTCTL_RESULTS, HOSTCTL_STATE):
        (base / name).mkdir(parents=True, exist_ok=True)
    return base


def make_netplan_paths(root: Path, files: dict[str, object] | None = None) -> NetplanPaths:
    netplan = root / "netplan"
    netplan.mkdir(parents=True, exist_ok=True)
    for name, data in (files if files is not None else {"50-cloud-init.yaml": INSTALLER_NETPLAN}).items():
        (netplan / name).write_text(json.dumps(data), encoding="utf-8")
    cloud = root / "cloud.cfg.d"
    cloud.mkdir(parents=True, exist_ok=True)
    return NetplanPaths(netplan_dir=netplan, state_dir=root / "hostctl-state", cloud_init_dir=cloud)


def make_hostctl(
    root: Path,
    db_path: Path,
    runner: FakeRunner,
    *,
    netplan_files: dict[str, object] | None = None,
    clock: Callable[[], float] = time.time,
) -> HostCtl:
    """テスト用の補助プログラム。netplan の設定ファイルは、netplan_files(省略時は導入時の例)で用意する。"""
    return HostCtl(
        base_dir=make_hostctl_dirs(root),
        db_path=db_path,
        paths=make_netplan_paths(root, netplan_files),
        runner=runner,
        yaml_io_factory=lambda: JSON_YAML,
        clock=clock,
        sleep=lambda seconds: None,
        log=lambda message: None,
    )


class HelperThread:
    """補助プログラムの代わりに、依頼のフォルダを見張って処理する(systemd の見張りの代わり)。"""

    def __init__(self, ctl: HostCtl) -> None:
        self.ctl = ctl
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        inbox = self.ctl.base_dir / HOSTCTL_INBOX
        while not self.stop_event.is_set():
            if any(inbox.iterdir()):
                self.ctl.process_inbox()
            self.stop_event.wait(0.05)

    def __enter__(self) -> "HelperThread":
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop_event.set()
        self.thread.join(timeout=5)
