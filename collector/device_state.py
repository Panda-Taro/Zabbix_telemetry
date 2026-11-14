"""機器ごとの受信状態の管理と、途絶の判定(6.6、6.7.4、MN-01、MN-02)。

- 機器ごと、コマンドごとに、最後に受信した時刻を記録する(6.6.1)
- まだ1度も受信していない機器・コマンドは「受信待ち」とする(6.6.2.1)
- 最後の受信(受信待ちの場合は、待ち始め)から判定時間が過ぎたら「途絶」とする(6.6.2、6.6.2.2)
- チェックを外したコマンドは、判定の対象にしない(6.6.1、6.6.2.3)
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from collector.config import DeviceConfig
from common.constants import STATUS_OK, STATUS_STALE, STATUS_WAITING
from common.interfaces import InterfaceId

_NS_PER_SEC = 1_000_000_000


def judge(last_ns: int | None, waiting_since_ns: int, now_ns: int, timeout_sec: int) -> str:
    """最後の受信時刻から、受信状態(ok / stale / waiting)を判定する。"""
    timeout_ns = timeout_sec * _NS_PER_SEC
    if last_ns is None:
        return STATUS_WAITING if now_ns - waiting_since_ns < timeout_ns else STATUS_STALE
    return STATUS_OK if now_ns - last_ns < timeout_ns else STATUS_STALE


@dataclass
class DeviceRuntime:
    """機器1台分の、受信状態の記録。"""

    config: DeviceConfig
    waiting_since_ns: int
    """受信待ちを始めた時刻(機器の登録、collectorの起動、ホスト名・送信元の変更)。"""
    command_waiting_since_ns: dict[str, int] = field(default_factory=dict)
    """コマンドごとの受信待ちを始めた時刻(チェックした時刻)。"""
    last_by_command: dict[str, int] = field(default_factory=dict)
    """コマンドごとの、最後に受信した時刻。"""
    mismatches: dict[str, tuple[InterfaceId, ...]] = field(default_factory=dict)
    """showコマンド名 → 登録したが届いたデータに存在しなかったインターフェース番号(6.3.8)。"""
    reported_status: str | None = None
    """ログに出した直近の受信状態(途絶の発生と復旧をログに出すため)。"""
    reported_command_status: dict[str, str] = field(default_factory=dict)

    def last_received_ns(self) -> int | None:
        """チェックしたコマンドのうち、最後に受信した時刻(6.6.1)。"""
        times = [self.last_by_command[c] for c in self.config.commands if c in self.last_by_command]
        return max(times) if times else None

    def status(self, now_ns: int, timeout_sec: int) -> str:
        return judge(self.last_received_ns(), self.waiting_since_ns, now_ns, timeout_sec)

    def command_status(self, command: str, now_ns: int, timeout_sec: int) -> str:
        waiting_since = self.command_waiting_since_ns.get(command, self.waiting_since_ns)
        return judge(self.last_by_command.get(command), waiting_since, now_ns, timeout_sec)

    def age_sec(self, now_ns: int) -> int:
        """最後の受信からの経過秒数。1度も受信していない場合は、受信待ちを始めてからの経過秒数(6.6.2.1)。"""
        base = self.last_received_ns()
        if base is None:
            base = self.waiting_since_ns
        return max(0, (now_ns - base) // _NS_PER_SEC)

    def mismatch_list(self) -> list[dict[str, str]]:
        """不整合のインターフェース番号の一覧(状態データの mismatch_interfaces 用)。

        例) [{"command": "show interface counters brief", "ifnum": "1/99"}]
        """
        return [
            {"command": command, "ifnum": interface.ifnum}
            for command in sorted(self.mismatches)
            for interface in self.mismatches[command]
        ]


class DeviceTracker:
    """全機器の受信状態を管理する。"""

    def __init__(self) -> None:
        self._runtimes: dict[int, DeviceRuntime] = {}

    def runtimes(self) -> list[DeviceRuntime]:
        return sorted(self._runtimes.values(), key=lambda r: (r.config.hostname, r.config.id))

    def get(self, device_id: int) -> DeviceRuntime | None:
        return self._runtimes.get(device_id)

    def apply_config(self, devices: Iterable[DeviceConfig], now_ns: int) -> list[DeviceConfig]:
        """新しい設定の機器一覧を反映する。LLD用データを送り直すべき機器の一覧を返す(6.5.5)。

        - 新しく登録された機器は、受信待ちから始める
        - ホスト名または送信元IPアドレスが変わった機器は、受信待ちに戻す(6.7.4)
        - コマンドの選択やインターフェース番号の変更では、受信状態をそのまま保つ(6.7.4)
        - 削除された機器は、記録を消す
        """
        needs_lld: list[DeviceConfig] = []
        new_runtimes: dict[int, DeviceRuntime] = {}
        for device in devices:
            runtime = self._runtimes.get(device.id)
            if runtime is None:
                runtime = DeviceRuntime(config=device, waiting_since_ns=now_ns)
                runtime.command_waiting_since_ns = {c: now_ns for c in device.commands}
                needs_lld.append(device)
            else:
                old = runtime.config
                if old.hostname != device.hostname or old.source_ip != device.source_ip:
                    runtime = DeviceRuntime(config=device, waiting_since_ns=now_ns)
                    runtime.command_waiting_since_ns = {c: now_ns for c in device.commands}
                else:
                    for command in device.commands - old.commands:
                        # 新しくチェックしたコマンドは、その時点から受信待ちとする
                        runtime.command_waiting_since_ns[command] = now_ns
                        runtime.last_by_command.pop(command, None)
                    runtime.config = device
                    runtime.mismatches = self._filter_mismatches(runtime.mismatches, device)
                if old.lld_signature() != device.lld_signature():
                    needs_lld.append(device)
            new_runtimes[device.id] = runtime
        self._runtimes = new_runtimes
        return needs_lld

    @staticmethod
    def _filter_mismatches(
        mismatches: dict[str, tuple[InterfaceId, ...]], device: DeviceConfig
    ) -> dict[str, tuple[InterfaceId, ...]]:
        """設定の変更で監視対象から外れた番号やコマンドを、不整合の一覧から取り除く。"""
        result: dict[str, tuple[InterfaceId, ...]] = {}
        for command, missing in mismatches.items():
            if command not in device.commands:
                continue
            registered = set(device.targets_for(command))
            remaining = tuple(i for i in missing if i in registered)
            if remaining:
                result[command] = remaining
        return result

    def record(
        self,
        device_id: int,
        command: str,
        missing: Sequence[InterfaceId],
        received_ns: int,
    ) -> None:
        """チェックしたコマンドのデータを受信したことを記録する(MN-01、CV-11)。"""
        runtime = self._runtimes.get(device_id)
        if runtime is None:
            return
        runtime.last_by_command[command] = received_ns
        if missing:
            runtime.mismatches[command] = tuple(missing)
        else:
            runtime.mismatches.pop(command, None)
