"""全体設定の画面と、Zabbix接続テスト(7.4.5、UI-12、UI-13、LG-04、LG-05)。

入力内容の検査は、段階2で作った共通の検査(common.validation.validate_settings)を必ず通す。
"""

import sqlite3
from dataclasses import dataclass

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from common.config_reader import get_settings
from common.config_writer import update_settings
from common.constants import (
    LISTEN_PORT_MAX,
    LISTEN_PORT_MIN,
    LOG_LEVELS,
    SETTING_DEFAULTS,
    SETTING_LLD_INTERVAL_SEC,
    SETTING_LOG_LEVEL,
    SETTING_QUEUE_MAX_SIZE,
    SETTING_RANGES,
    SETTING_SELF_MONITOR_INTERVAL_SEC,
    SETTING_STALE_TIMEOUT_SEC,
    SETTING_TELEMETRY_UDP_PORT,
    SETTING_ZABBIX_PORT,
    SETTING_ZABBIX_SERVER,
    TCP_UDP_PORT_MAX,
    TCP_UDP_PORT_MIN,
)
from common.netconfig import ACTION_SYNC_FIREWALL
from common.validation import ValidationError, validate_settings
from web.auth import CurrentUser
from web.context import WebContext
from web.zabbix_check import check_zabbix


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    note: str


def _range_note(key: str, unit: str) -> str:
    minimum, maximum = SETTING_RANGES[key]
    return f"{minimum:,}〜{maximum:,} {unit}。既定値は {int(SETTING_DEFAULTS[key]):,} {unit}"


# 全体設定の画面に並べる項目(7.4.5.1)。WebGUIのポート番号はシステム設定の画面で扱う(7.4.7)
FIELDS: tuple[SettingField, ...] = (
    SettingField(SETTING_ZABBIX_SERVER, "ZabbixサーバーのIPアドレス", "必須。例:192.0.2.100"),
    SettingField(
        SETTING_ZABBIX_PORT,
        "Zabbixサーバーのポート番号",
        f"{TCP_UDP_PORT_MIN}〜{TCP_UDP_PORT_MAX}。既定値は {SETTING_DEFAULTS[SETTING_ZABBIX_PORT]}",
    ),
    SettingField(
        SETTING_TELEMETRY_UDP_PORT,
        "Telemetryの受信ポート番号(UDP)",
        f"{LISTEN_PORT_MIN}〜{LISTEN_PORT_MAX}。既定値は {SETTING_DEFAULTS[SETTING_TELEMETRY_UDP_PORT]}。"
        "変更すると、数秒間、受信が欠けます",
    ),
    SettingField(SETTING_STALE_TIMEOUT_SEC, "途絶判定時間", _range_note(SETTING_STALE_TIMEOUT_SEC, "秒")),
    SettingField(SETTING_SELF_MONITOR_INTERVAL_SEC, "自己監視値の送信間隔", _range_note(SETTING_SELF_MONITOR_INTERVAL_SEC, "秒")),
    SettingField(SETTING_LLD_INTERVAL_SEC, "LLD用データの定期送信間隔", _range_note(SETTING_LLD_INTERVAL_SEC, "秒")),
    SettingField(SETTING_QUEUE_MAX_SIZE, "送信待ちキューの上限", _range_note(SETTING_QUEUE_MAX_SIZE, "件")),
    SettingField(SETTING_LOG_LEVEL, "ログレベル", "DEBUG / INFO / WARNING / ERROR。既定値は INFO"),
)
FIELD_KEYS: tuple[str, ...] = tuple(field.key for field in FIELDS)


def register(app: FastAPI, ctx: WebContext) -> None:
    def settings_page(
        request: Request,
        user: CurrentUser,
        values: dict[str, str],
        errors: dict[str, list[str]] | None = None,
        message: str = "",
        status_code: int = 200,
    ) -> HTMLResponse:
        return ctx.render(
            request,
            "settings.html",
            {
                "fields": FIELDS,
                "values": values,
                "errors": errors or {},
                "log_levels": LOG_LEVELS,
                "message": message,
            },
            user,
            status_code,
        )

    @app.get("/settings")
    def get_settings_page(
        request: Request,
        saved: str = "",
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> HTMLResponse:
        current = get_settings(db)
        values = {key: current.get(key, SETTING_DEFAULTS[key]) for key in FIELD_KEYS}
        return settings_page(request, user, values, message="保存しました" if saved == "1" else "")

    @app.post("/settings")
    def post_settings(
        request: Request,
        zabbix_server: str = Form(""),
        zabbix_port: str = Form(""),
        telemetry_udp_port: str = Form(""),
        stale_timeout_sec: str = Form(""),
        self_monitor_interval_sec: str = Form(""),
        lld_interval_sec: str = Form(""),
        queue_max_size: str = Form(""),
        log_level: str = Form(""),
        confirm: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
        db: sqlite3.Connection = Depends(ctx.get_db),
    ) -> Response:
        ctx.verify_csrf(user, csrf)
        submitted = {
            SETTING_ZABBIX_SERVER: zabbix_server,
            SETTING_ZABBIX_PORT: zabbix_port,
            SETTING_TELEMETRY_UDP_PORT: telemetry_udp_port,
            SETTING_STALE_TIMEOUT_SEC: stale_timeout_sec,
            SETTING_SELF_MONITOR_INTERVAL_SEC: self_monitor_interval_sec,
            SETTING_LLD_INTERVAL_SEC: lld_interval_sec,
            SETTING_QUEUE_MAX_SIZE: queue_max_size,
            SETTING_LOG_LEVEL: log_level,
        }
        try:
            values = validate_settings(submitted, required_keys=FIELD_KEYS, require_zabbix_server=True)
        except ValidationError as exc:
            return settings_page(request, user, submitted, exc.errors, status_code=400)

        current = get_settings(db)
        port_changed = values[SETTING_TELEMETRY_UDP_PORT] != current.get(SETTING_TELEMETRY_UDP_PORT)
        if port_changed and confirm != "1":
            # 受信ポート番号を変えると数秒間受信が欠けることを、保存の前に知らせる(6.7.2)
            return ctx.render(
                request,
                "settings_confirm.html",
                {
                    "values": values,
                    "old_port": current.get(SETTING_TELEMETRY_UDP_PORT, ""),
                    "new_port": values[SETTING_TELEMETRY_UDP_PORT],
                },
                user,
            )
        update_settings(db, values, audit_user=user.username)
        if port_changed and ctx.hostctl.available:
            # ファイアウォールが有効なら、新しい受信ポート番号を開け、古い番号を閉じてもらう(決定事項 D-030)
            result = ctx.hostctl.submit(ctx.hostctl.new_request(ACTION_SYNC_FIREWALL), wait=False)
            if not result.ok:
                ctx.logger.error("ファイアウォールの調整を依頼できませんでした(%s)", result.message)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/zabbix-test")
    def post_zabbix_test(
        zabbix_server: str = Form(""),
        zabbix_port: str = Form(""),
        csrf: str = Form(""),
        user: CurrentUser = Depends(ctx.require_admin),
    ) -> HTMLResponse:
        """入力したZabbixサーバーに接続できるかを確かめ、結果を表示する(7.4.5.2)。"""
        ctx.verify_csrf(user, csrf)
        try:
            values = validate_settings(
                {SETTING_ZABBIX_SERVER: zabbix_server, SETTING_ZABBIX_PORT: zabbix_port},
                required_keys=(SETTING_ZABBIX_SERVER, SETTING_ZABBIX_PORT),
                require_zabbix_server=True,
            )
        except ValidationError as exc:
            return HTMLResponse(ctx.render_text("_zabbix_test_result.html", ok=False, message=" / ".join(exc.messages())))
        ok, message = check_zabbix(values[SETTING_ZABBIX_SERVER], int(values[SETTING_ZABBIX_PORT]))
        return HTMLResponse(ctx.render_text("_zabbix_test_result.html", ok=ok, message=message))
