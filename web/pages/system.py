"""システム設定の画面のうち、設定のバックアップと復元(7.4.7.1、7.4.7.6、7.4.7.7、BK-01〜BK-05、UI-21)。

本体のIPアドレスとWebGUIのポート番号の変更は、段階8で作る。
"""

import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, Response

from common.backup import BackupError, content_summary, export_backup_bytes, parse_backup, restore_backup
from common.constants import MAX_BACKUP_BYTES
from web.auth import CurrentUser
from web.context import WebContext
from web.dashboard import JST

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


def register(app: FastAPI, ctx: WebContext) -> None:
    pending = PendingRestores()

    def system_page(
        request: Request,
        user: CurrentUser,
        errors: list[str] | None = None,
        message: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        return ctx.render(
            request,
            "system.html",
            {"errors": errors or [], "message": message, "max_mb": MAX_BACKUP_BYTES // (1024 * 1024)},
            user,
            status_code,
        )

    @app.get("/system")
    def get_system(request: Request, user: CurrentUser = Depends(ctx.require_admin)) -> HTMLResponse:
        return system_page(request, user)

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
    ) -> HTMLResponse:
        ctx.verify_csrf(user, csrf)
        if file is None or not file.filename:
            return system_page(request, user, ["バックアップファイルを選んでください"], status_code=400)
        raw = await file.read(MAX_BACKUP_BYTES + 1)
        try:
            content = parse_backup(raw)  # 中身をすべて検査する。この時点では何も変更しない(8.6.4)
        except BackupError as exc:
            return system_page(request, user, exc.errors[:50], status_code=400)
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
                request, user, ["確認画面の有効期限が切れました。もう一度ファイルを選んでください"], status_code=400
            )
        try:
            content = parse_backup(item.raw)  # 念のため、もう一度すべて検査する
        except BackupError as exc:
            return system_page(request, user, exc.errors[:50], status_code=400)
        restore_backup(db, content, audit_user=user.username, audit_target=item.filename or None)
        summary = content_summary(content)
        return system_page(request, user, message=f"設定を復元しました(機器 {len(summary['devices'])} 台と全体設定)")
