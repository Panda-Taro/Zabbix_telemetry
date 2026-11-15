"""機器一覧と、機器の登録・編集・削除の画面(7.4.3、7.4.4、UI-07〜UI-11、UI-21、LG-05)。

入力内容の検査は、段階2で作った共通の検査(common.validation)を必ず通す。
"""

import sqlite3
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from common.config_reader import DeviceRecord, get_device, list_devices
from common.config_writer import delete_device, save_device
from common.constants import COMMAND_COLUMNS, PURPOSE_COUNTERS, PURPOSE_PTP
from common.validation import DeviceForm, ValidationError, format_interface_list
from web.auth import CurrentUser
from web.context import WebContext
from web.dashboard import MUTED, STATUS_DISPLAY, Badge

# 一覧の上に表示する結果の文言(URLには番号だけを載せ、任意の文言は表示しない)
MESSAGES = {"saved": "保存しました", "deleted": "削除しました", "notfound": "機器が見つかりません"}

# 画面に並べるコマンドの順番と、devices テーブルの列名
COMMAND_FIELDS: tuple[tuple[str, str], ...] = tuple(COMMAND_COLUMNS.items())


@dataclass(frozen=True)
class DeviceListRow:
    record: DeviceRecord
    status: Badge

    @property
    def counters_count(self) -> int:
        return len(self.record.interfaces.get(PURPOSE_COUNTERS, ()))

    @property
    def ptp_count(self) -> int:
        return len(self.record.interfaces.get(PURPOSE_PTP, ()))


def _form_values(record: DeviceRecord | None) -> dict[str, object]:
    """入力欄に表示する値。"""
    if record is None:
        return {
            "hostname": "",
            "source_ip": "",
            "commands": {column: True for column in COMMAND_COLUMNS.values()},
            "interfaces_counters": "",
            "interfaces_ptp": "",
        }
    return {
        "hostname": record.hostname,
        "source_ip": record.source_ip,
        "commands": dict(record.commands),
        "interfaces_counters": format_interface_list(record.interfaces.get(PURPOSE_COUNTERS, ())),
        "interfaces_ptp": format_interface_list(record.interfaces.get(PURPOSE_PTP, ())),
    }


def register(app: FastAPI, ctx: WebContext) -> None:
    # ------------------------------------------------------------------
    # 機器一覧(7.4.3)
    # ------------------------------------------------------------------
    @app.get("/devices")
    def get_devices(
        request: Request,
        q: str = "",
        sort: str = "asc",
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        keyword = q.strip()[:128]
        statuses = {row["device_id"]: row["status"] for row in db.execute("SELECT device_id, status FROM device_status")}
        rows = []
        for record in list_devices(db):
            if keyword and keyword.lower() not in record.hostname.lower():
                continue
            text, color = STATUS_DISPLAY.get(statuses.get(record.id, ""), ("不明", MUTED))
            rows.append(DeviceListRow(record=record, status=Badge(text, color)))
        descending = sort == "desc"
        rows.sort(key=lambda r: r.record.hostname.lower(), reverse=descending)
        return ctx.render(
            request,
            "devices.html",
            {
                "rows": rows,
                "q": keyword,
                "sort": "desc" if descending else "asc",
                "command_fields": COMMAND_FIELDS,
                "message": MESSAGES.get(request.query_params.get("message", ""), ""),
            },
            user,
        )

    # ------------------------------------------------------------------
    # 登録・編集(7.4.4)
    # ------------------------------------------------------------------
    def form_page(
        request: Request,
        user: CurrentUser,
        values: dict[str, object],
        device_id: int | None,
        errors: dict[str, list[str]] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return ctx.render(
            request,
            "device_form.html",
            {
                "values": values,
                "device_id": device_id,
                "errors": errors or {},
                "command_fields": COMMAND_FIELDS,
            },
            user,
            status_code,
        )

    @app.get("/devices/new")
    def get_new_device(request: Request, user: CurrentUser = Depends(ctx.require_admin)) -> HTMLResponse:
        return form_page(request, user, _form_values(None), None)

    @app.get("/devices/{device_id}/edit")
    def get_edit_device(
        request: Request,
        device_id: int,
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        record = get_device(db, device_id)
        if record is None:
            return RedirectResponse("/devices?message=notfound", status_code=303)
        return form_page(request, user, _form_values(record), device_id)

    def save(
        request: Request,
        user: CurrentUser,
        db: sqlite3.Connection,
        device_id: int | None,
        hostname: str,
        source_ip: str,
        commands: dict[str, bool],
        interfaces_counters: str,
        interfaces_ptp: str,
    ) -> Response:
        form = DeviceForm(
            hostname=hostname,
            source_ip=source_ip,
            commands=commands,
            interfaces_text={PURPOSE_COUNTERS: interfaces_counters, PURPOSE_PTP: interfaces_ptp},
        )
        values = {
            "hostname": hostname,
            "source_ip": source_ip,
            "commands": commands,
            "interfaces_counters": interfaces_counters,
            "interfaces_ptp": interfaces_ptp,
        }
        try:
            save_device(db, form, device_id, audit_user=user.username)
        except ValidationError as exc:
            return form_page(request, user, values, device_id, exc.errors, 400)
        except LookupError:
            return RedirectResponse("/devices?message=notfound", status_code=303)
        return RedirectResponse("/devices?message=saved", status_code=303)

    @app.post("/devices/new")
    def post_new_device(
        request: Request,
        hostname: str = Form(""),
        source_ip: str = Form(""),
        cmd_counters_brief: str = Form(""),
        cmd_ptp_brief: str = Form(""),
        cmd_ptp_clock: str = Form(""),
        cmd_ptp_parent: str = Form(""),
        interfaces_counters: str = Form(""),
        interfaces_ptp: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        ctx.verify_csrf(user, csrf)
        commands = {
            "cmd_counters_brief": cmd_counters_brief == "1",
            "cmd_ptp_brief": cmd_ptp_brief == "1",
            "cmd_ptp_clock": cmd_ptp_clock == "1",
            "cmd_ptp_parent": cmd_ptp_parent == "1",
        }
        return save(request, user, db, None, hostname, source_ip, commands, interfaces_counters, interfaces_ptp)

    @app.post("/devices/{device_id}/edit")
    def post_edit_device(
        request: Request,
        device_id: int,
        hostname: str = Form(""),
        source_ip: str = Form(""),
        cmd_counters_brief: str = Form(""),
        cmd_ptp_brief: str = Form(""),
        cmd_ptp_clock: str = Form(""),
        cmd_ptp_parent: str = Form(""),
        interfaces_counters: str = Form(""),
        interfaces_ptp: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        ctx.verify_csrf(user, csrf)
        commands = {
            "cmd_counters_brief": cmd_counters_brief == "1",
            "cmd_ptp_brief": cmd_ptp_brief == "1",
            "cmd_ptp_clock": cmd_ptp_clock == "1",
            "cmd_ptp_parent": cmd_ptp_parent == "1",
        }
        return save(request, user, db, device_id, hostname, source_ip, commands, interfaces_counters, interfaces_ptp)

    # ------------------------------------------------------------------
    # 削除(確認画面を表示してから削除する。7.4.3.3、UI-21)
    # ------------------------------------------------------------------
    @app.get("/devices/{device_id}/delete")
    def get_delete_device(
        request: Request,
        device_id: int,
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        record = get_device(db, device_id)
        if record is None:
            return RedirectResponse("/devices?message=notfound", status_code=303)
        return ctx.render(request, "device_delete.html", {"record": record}, user)

    @app.post("/devices/{device_id}/delete")
    def post_delete_device(
        device_id: int,
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        ctx.verify_csrf(user, csrf)
        deleted = delete_device(db, device_id, audit_user=user.username)
        return RedirectResponse(f"/devices?message={'deleted' if deleted else 'notfound'}", status_code=303)
