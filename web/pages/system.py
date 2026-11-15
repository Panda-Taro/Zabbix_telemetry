"""システム設定の画面(7.4.7、UI-14〜UI-16、UI-21、BK-01〜BK-05)。

- 本体のIPアドレスとWebGUIのポート番号:保存して、中間サーバーを再起動して反映する(決定事項 D-029)。
  保存の前に、画面の中に確認を出す。選べるのは「保存して再起動する」と「キャンセル」だけで、
  「保存したが再起動していない」状態は作らない(補助プログラムが再起動を受け付けてから保存する)。
- 設定のバックアップと復元。
"""

import os
import secrets
import socket
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from common.backup import BackupError, content_summary, export_backup_bytes, parse_backup, restore_backup
from common.config_reader import get_settings
from common.config_writer import record_audit, update_settings
from common.constants import (
    DEFAULT_WEB_HOST,
    LISTEN_PORT_MAX,
    LISTEN_PORT_MIN,
    MAX_BACKUP_BYTES,
    NETWORK_DNS_MAX,
    NETWORK_PREFIX_MAX,
    NETWORK_PREFIX_MIN,
    REBOOT_DOWNTIME_TEXT,
    SETTING_DEFAULTS,
    SETTING_WEBGUI_PORT,
    WEBGUI_FALLBACK_PORT,
)
from common.netconfig import ACTION_REBOOT, NetworkConfig, NetworkInputError, validate_network
from common.validation import ValidationError, validate_settings
from web.auth import CurrentUser
from web.context import WebContext
from web.dashboard import JST
from web.hostctl import HostNetworkState

# 変更履歴に記録する、ファイル名の文字数の上限
_MAX_FILENAME_LENGTH = 100
# 確認画面を表示してから、確定するまでに預かっておく時間
_PENDING_TTL = timedelta(minutes=10)


@dataclass(frozen=True)
class _PendingRestore:
    raw: bytes
    filename: str
    user_id: int
    expires_at: datetime


class PendingRestores:
    """検査を済ませたバックアップファイルを、確認画面で確定されるまで預かる(メモリ上)。"""

    def __init__(self) -> None:
        self._items: dict[str, _PendingRestore] = {}
        self._lock = threading.Lock()

    def put(self, raw: bytes, filename: str, user_id: int, now: datetime) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._items = {k: v for k, v in self._items.items() if v.expires_at > now}
            self._items[token] = _PendingRestore(raw, filename, user_id, now + _PENDING_TTL)
        return token

    def take(self, token: str, user_id: int, now: datetime) -> _PendingRestore | None:
        """預かったものを取り出す。預けた本人で、期限内のときだけ返す。取り出したら消す。"""
        with self._lock:
            item = self._items.get(token)
            if item is None or item.user_id != user_id or item.expires_at <= now:
                return None
            del self._items[token]
            return item


# ======================================================================
# 本体のIPアドレスとWebGUIのポート番号(UI-14〜UI-16、決定事項 D-029、D-030)
# ======================================================================

# 入力欄の名前
HOST_FIELDS: tuple[str, ...] = ("address", "gateway", "dns1", "dns2", "webgui_port")
# 本体のIPアドレスの入力欄(すべて空なら、IPアドレスは変えない)
_NETWORK_FIELDS: tuple[str, ...] = ("address", "gateway", "dns1", "dns2")
if len(_NETWORK_FIELDS) - 2 != NETWORK_DNS_MAX:
    raise RuntimeError("DNSサーバーの入力欄の数を、NETWORK_DNS_MAX に合わせてください")


def port_is_free(port: int) -> bool:
    """そのポート番号で待ち受けられるか(ほかのプログラムが使っていないか)を確かめる。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != "nt":
            # Windows では、この指定をすると使用中のポートにも割り込めてしまうため、Linux だけで使う
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((DEFAULT_WEB_HOST, port))
        except OSError:
            return False
    return True


@dataclass(frozen=True)
class HostChange:
    """検査に合格した、本体のIPアドレスとWebGUIのポート番号の変更。"""

    network: NetworkConfig | None
    """変える本体のIPアドレス(変えない場合は None)"""
    webgui_port: int | None
    """新しいWebGUIのポート番号(変えない場合は None)"""
    form: dict[str, str]

    @property
    def empty(self) -> bool:
        return self.network is None and self.webgui_port is None


class HostInputError(Exception):
    def __init__(self, errors: dict[str, list[str]]) -> None:
        super().__init__("入力に誤りがあります")
        self.errors = errors


def evaluate_host_change(
    form: dict[str, str],
    saved_port: int,
    state: HostNetworkState | None,
    listen_port: int | None,
    port_free: Callable[[int], bool] = port_is_free,
) -> HostChange:
    """入力を検査し、今の設定から何が変わるかを決める。問題があれば HostInputError。"""
    errors: dict[str, list[str]] = {}
    new_port: int | None = None
    try:
        new_port = int(validate_settings({SETTING_WEBGUI_PORT: form["webgui_port"]})[SETTING_WEBGUI_PORT])
    except ValidationError as exc:
        errors["webgui_port"] = exc.messages()

    network: NetworkConfig | None = None
    if any(form[name].strip() for name in _NETWORK_FIELDS):
        try:
            network = validate_network(form["address"], form["gateway"], [form["dns1"], form["dns2"]])
        except NetworkInputError as exc:
            errors.update(exc.errors)
    if network is not None and state is not None and not state.dhcp and not state.error:
        unchanged = (state.address, state.gateway, state.dns) == (network.address, network.gateway, network.dns)
        if unchanged:
            network = None

    port_change: int | None = None
    if new_port is not None and new_port != saved_port:
        port_change = new_port
        if new_port != listen_port and not port_free(new_port):
            errors.setdefault("webgui_port", []).append(
                f"ポート番号 {new_port} は、ほかのプログラムが使っています。別の番号にしてください"
            )
    if errors:
        raise HostInputError(errors)
    return HostChange(network=network, webgui_port=port_change, form=form)


def register(app: FastAPI, ctx: WebContext) -> None:
    pending = PendingRestores()

    def saved_webgui_port(db: sqlite3.Connection) -> int:
        return int(get_settings(db).get(SETTING_WEBGUI_PORT, SETTING_DEFAULTS[SETTING_WEBGUI_PORT]))

    def current_form(db: sqlite3.Connection, state: HostNetworkState | None) -> dict[str, str]:
        dns = list(state.dns) if state else []
        return {
            "address": state.address if state else "",
            "gateway": state.gateway if state else "",
            "dns1": dns[0] if len(dns) > 0 else "",
            "dns2": dns[1] if len(dns) > 1 else "",
            "webgui_port": str(saved_webgui_port(db)),
        }

    def system_page(
        request: Request,
        user: CurrentUser,
        db: sqlite3.Connection,
        errors: list[str] | None = None,
        message: str = "",
        status_code: int = 200,
        host_form: dict[str, str] | None = None,
        host_errors: dict[str, list[str]] | None = None,
        host_message: str = "",
    ) -> HTMLResponse:
        state = ctx.hostctl.network_state()
        saved_port = saved_webgui_port(db)
        return ctx.render(
            request,
            "system.html",
            {
                "errors": errors or [],
                "message": message,
                "max_mb": MAX_BACKUP_BYTES // (1024 * 1024),
                "state": state,
                "hostctl_available": ctx.hostctl.available,
                "host_form": host_form or current_form(db, state),
                "host_errors": host_errors or {},
                "host_message": host_message,
                "saved_port": saved_port,
                "listen_port": ctx.listen_port,
                "fallback_port": WEBGUI_FALLBACK_PORT,
                "port_min": LISTEN_PORT_MIN,
                "port_max": LISTEN_PORT_MAX,
                "prefix_min": NETWORK_PREFIX_MIN,
                "prefix_max": NETWORK_PREFIX_MAX,
                "downtime": REBOOT_DOWNTIME_TEXT,
            },
            user,
            status_code,
        )

    @app.get("/system")
    def get_system(
        request: Request,
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        return system_page(request, user, db)

    def new_url(request: Request, change: HostChange, saved_port: int) -> str:
        host = change.network.ip if change.network else (request.url.hostname or "")
        port = change.webgui_port if change.webgui_port is not None else (request.url.port or saved_port)
        return f"http://{host}:{port}/"

    def read_host_form(
        address: str, gateway: str, dns1: str, dns2: str, webgui_port: str
    ) -> dict[str, str]:
        # 入力欄の文字数は、検査の中で確かめる(外部データの大きさの検査、11.3.3)
        return {"address": address, "gateway": gateway, "dns1": dns1, "dns2": dns2, "webgui_port": webgui_port}

    def check_host_change(
        request: Request, user: CurrentUser, db: sqlite3.Connection, form: dict[str, str]
    ) -> HostChange | HTMLResponse:
        """入力を検査する。問題があれば、エラーを付けたシステム設定の画面を返す。"""
        if not ctx.hostctl.available:
            return system_page(
                request, user, db, host_form=form, status_code=400,
                host_errors={"_": ["本体側の補助プログラムが見つからないため、変更できません。deploy.sh で導入した中間サーバーで操作してください"]},
            )  # fmt: skip
        try:
            change = evaluate_host_change(form, saved_webgui_port(db), ctx.hostctl.network_state(), ctx.listen_port)
        except HostInputError as exc:
            return system_page(request, user, db, host_form=form, host_errors=exc.errors, status_code=400)
        if change.empty:
            return system_page(request, user, db, host_form=form, host_message="変更がありません")
        return change

    @app.post("/system/host")
    def post_host(
        request: Request,
        address: str = Form(""),
        gateway: str = Form(""),
        dns1: str = Form(""),
        dns2: str = Form(""),
        webgui_port: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        """入力を検査し、画面の中に確認を出す(UI-16)。この時点では何も保存しない。"""
        ctx.verify_csrf(user, csrf)
        form = read_host_form(address, gateway, dns1, dns2, webgui_port)
        checked = check_host_change(request, user, db, form)
        if isinstance(checked, HTMLResponse):
            return checked
        state = ctx.hostctl.network_state()
        saved_port = saved_webgui_port(db)
        return ctx.render(
            request,
            "system_host_confirm.html",
            {
                "change": checked,
                "state": state,
                "saved_port": saved_port,
                "new_url": new_url(request, checked, saved_port),
                "downtime": REBOOT_DOWNTIME_TEXT,
            },
            user,
        )

    @app.post("/system/host/confirm")
    def post_host_confirm(
        request: Request,
        address: str = Form(""),
        gateway: str = Form(""),
        dns1: str = Form(""),
        dns2: str = Form(""),
        webgui_port: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        """「保存して再起動する」(D-029)。補助プログラムが再起動を受け付けてから、設定を保存する。

        受け付けられなかった場合は、何も保存しない(「保存したが再起動していない」状態を作らない)。
        """
        ctx.verify_csrf(user, csrf)
        form = read_host_form(address, gateway, dns1, dns2, webgui_port)
        checked = check_host_change(request, user, db, form)  # 確認の画面のあとで変わっていないか、もう一度検査する
        if isinstance(checked, HTMLResponse):
            return checked
        state = ctx.hostctl.network_state()
        saved_port = saved_webgui_port(db)
        host_request = ctx.hostctl.new_request(ACTION_REBOOT, network=checked.network, webgui_port=checked.webgui_port)
        result = ctx.hostctl.submit(host_request, wait=True)
        if not result.ok:
            ctx.logger.error("本体のIPアドレスまたはWebGUIのポート番号の変更を、補助プログラムが受け付けませんでした(%s)", result.message)
            return system_page(
                request, user, db, host_form=form, status_code=500,
                host_errors={"_": [f"中間サーバーの再起動を受け付けられませんでした。何も保存していません。({result.message})"]},
            )  # fmt: skip

        # 補助プログラムは、数秒後に本体を再起動する。その前に、設定と変更履歴を保存する
        if checked.webgui_port is not None:
            update_settings(db, {SETTING_WEBGUI_PORT: str(checked.webgui_port)}, audit_user=user.username, audit_category="system")
        if checked.network is not None:
            before: dict[str, Any] | None = None
            if state is not None and not state.error:
                before = {"address": state.address, "gateway": state.gateway, "dns": list(state.dns)}
            record_audit(
                db, username=user.username, category="system", action="update", target="本体のIPアドレス",
                before=before, after=checked.network.to_dict(),
            )  # fmt: skip
        ctx.logger.warning("システム設定の変更のため、中間サーバーを再起動します(%s)", user.username)
        return ctx.render(
            request,
            "system_rebooting.html",
            {"new_url": new_url(request, checked, saved_port), "downtime": REBOOT_DOWNTIME_TEXT},
            user,
        )

    # ------------------------------------------------------------------
    # バックアップ(BK-01、BK-02)
    # ------------------------------------------------------------------
    @app.get("/system/backup")
    def download_backup(
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        stamp = ctx.now().astimezone(JST).strftime("%Y%m%d-%H%M%S")
        return Response(
            export_backup_bytes(db),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="telemetry-backup-{stamp}.json"'},
        )

    # ------------------------------------------------------------------
    # 復元(ファイルを検査してから確認画面を表示し、確定したら置き換える。BK-03〜BK-05、UI-21)
    # ------------------------------------------------------------------
    @app.post("/system/restore")
    async def post_restore_upload(
        request: Request,
        file: UploadFile | None = File(None),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        ctx.verify_csrf(user, csrf)
        if file is None or not file.filename:
            return system_page(request, user, db, ["バックアップファイルを選んでください"], status_code=400)
        raw = await file.read(MAX_BACKUP_BYTES + 1)
        try:
            content = parse_backup(raw)  # 中身をすべて検査する。この時点では何も変更しない(8.6.4)
        except BackupError as exc:
            return system_page(request, user, db, exc.errors[:50], status_code=400)
        filename = file.filename[:_MAX_FILENAME_LENGTH]
        token = pending.put(raw, filename, user.id, ctx.now())
        return ctx.render(
            request,
            "restore_confirm.html",
            {"summary": content_summary(content), "filename": filename, "restore_token": token},
            user,
        )

    @app.post("/system/restore/confirm")
    def post_restore_confirm(
        request: Request,
        restore_token: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        ctx.verify_csrf(user, csrf)
        item = pending.take(restore_token[:64], user.id, ctx.now())
        if item is None:
            return system_page(
                request, user, db, ["確認画面の有効期限が切れました。もう一度ファイルを選んでください"], status_code=400
            )
        try:
            content = parse_backup(item.raw)  # 念のため、もう一度すべて検査する
        except BackupError as exc:
            return system_page(request, user, db, exc.errors[:50], status_code=400)
        restore_backup(db, content, audit_user=user.username, audit_target=item.filename or None)
        summary = content_summary(content)
        return system_page(request, user, db, message=f"設定を復元しました(機器 {len(summary['devices'])} 台と全体設定)")
