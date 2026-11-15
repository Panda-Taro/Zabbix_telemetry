"""本体側の補助プログラム(tools/telemetry_hostctl.py)への依頼を出す部品(決定事項 D-029)。

web コンテナには特別な権限を与えない。web は、依頼をファイルにして決まったフォルダに置き、
補助プログラムが書く結果のファイルを読むだけとする。
"""

import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from common.constants import (
    HOSTCTL_ACK_TIMEOUT_SEC,
    HOSTCTL_INBOX,
    HOSTCTL_MAX_REQUEST_BYTES,
    HOSTCTL_NETWORK_STATE_FILE,
    HOSTCTL_RESULTS,
    HOSTCTL_SPOOL,
    HOSTCTL_STATE,
)
from common.netconfig import HostRequest, HostResult, NetworkConfig, RequestError, ipv4_strings, parse_result

# 結果のファイルができたかを確かめる間隔(秒)
_POLL_INTERVAL_SEC = 0.1
# 状態のファイルの大きさの上限(バイト)
_MAX_STATE_BYTES = 8192


@dataclass(frozen=True)
class HostNetworkState:
    """補助プログラムが書き出した、本体のネットワークの現在の状態(表示用)。"""

    interface: str = ""
    address: str = ""
    gateway: str = ""
    dns: tuple[str, ...] = ()
    dhcp: bool = False
    updated_at: str = ""
    error: str = ""

    @property
    def ip(self) -> str:
        return self.address.split("/", 1)[0]


class HostCtlClient:
    """補助プログラムへの依頼の出し方と、本体の状態の読み方。"""

    def __init__(
        self,
        base_dir: Path | None,
        *,
        ack_timeout: float = HOSTCTL_ACK_TIMEOUT_SEC,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_dir = base_dir
        self.ack_timeout = ack_timeout
        self.clock = clock
        self.sleep = sleep

    @property
    def available(self) -> bool:
        """補助プログラムとやり取りするフォルダがあれば真(開発用PCで動かしている場合などは偽)。"""
        return self.base_dir is not None and all(
            (self.base_dir / name).is_dir() for name in (HOSTCTL_SPOOL, HOSTCTL_INBOX, HOSTCTL_RESULTS)
        )

    def network_state(self) -> HostNetworkState | None:
        """本体のネットワークの現在の状態。読み取れなければ None。"""
        if self.base_dir is None:
            return None
        path = self.base_dir / HOSTCTL_STATE / HOSTCTL_NETWORK_STATE_FILE
        try:
            with path.open("rb") as file:
                data = json.loads(file.read(_MAX_STATE_BYTES + 1)[:_MAX_STATE_BYTES].decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None

        def text(key: str, limit: int = 64) -> str:
            value = data.get(key)
            return value[:limit] if isinstance(value, str) else ""

        dns = data.get("dns")
        return HostNetworkState(
            interface=text("interface"),
            address=text("address"),
            gateway=text("gateway"),
            dns=tuple(ipv4_strings(dns if isinstance(dns, list) else [])),
            dhcp=data.get("dhcp") is True,
            updated_at=text("updated_at"),
            error=text("error", 300),
        )

    def new_request(
        self, action: str, *, network: NetworkConfig | None = None, webgui_port: int | None = None
    ) -> HostRequest:
        return HostRequest(
            id=secrets.token_hex(16), created_at=self.clock(), action=action, network=network, webgui_port=webgui_port
        )

    def submit(self, request: HostRequest, *, wait: bool) -> HostResult:
        """依頼を出す。wait なら、補助プログラムの返事を待つ。

        返事が来ないまま待つ時間を過ぎた場合は、依頼を取り下げる。取り下げられなかった
        (補助プログラムがすでに受け取っていた)場合は、もう少し返事を待つ。
        """
        if not self.available or self.base_dir is None:
            return HostResult(request.id, False, "本体側の補助プログラムが見つかりません")
        raw = request.to_json_bytes()
        if len(raw) > HOSTCTL_MAX_REQUEST_BYTES:
            return HostResult(request.id, False, "依頼が大きすぎます")
        name = f"{request.id}.json"
        spool = self.base_dir / HOSTCTL_SPOOL / name
        inbox = self.base_dir / HOSTCTL_INBOX / name
        try:
            fd = os.open(spool, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(fd, raw)
            finally:
                os.close(fd)
            os.replace(spool, inbox)  # 書き終えてから、補助プログラムが見るフォルダへ移す
        except OSError as exc:
            spool.unlink(missing_ok=True)
            return HostResult(request.id, False, f"依頼を書き込めませんでした({exc.strerror or exc})")
        if not wait:
            return HostResult(request.id, True, "依頼しました")

        result = self._wait_result(request.id, self.ack_timeout)
        if result is not None:
            return result
        try:
            inbox.unlink()
            return HostResult(request.id, False, "本体側の補助プログラムが応答しませんでした")
        except FileNotFoundError:
            pass  # 補助プログラムが受け取って処理している
        result = self._wait_result(request.id, self.ack_timeout)
        if result is not None:
            return result
        return HostResult(request.id, False, "本体側の補助プログラムの処理の結果を確かめられませんでした")

    def _wait_result(self, request_id: str, timeout: float) -> HostResult | None:
        assert self.base_dir is not None
        path = self.base_dir / HOSTCTL_RESULTS / f"{request_id}.json"
        deadline = time.monotonic() + timeout
        while True:
            try:
                return parse_result(path.read_bytes(), request_id)
            except FileNotFoundError:
                pass
            except (OSError, RequestError):
                pass  # 書きかけなど。もう一度読む
            if time.monotonic() >= deadline:
                return None
            self.sleep(_POLL_INTERVAL_SEC)
