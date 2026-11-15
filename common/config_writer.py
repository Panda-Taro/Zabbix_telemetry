"""設定データを書き込む部品。web だけが使う(8.1.3、CF-06)。

collector はこのファイルを読み込まない(自動テストで確かめている)。
devices、device_interfaces、settings を変更したときは、同じトランザクションの中で
設定の版番号(config_version)を1増やす(8.3.3.3、CF-01)。
"""

import json
import sqlite3
from collections.abc import Mapping
from typing import Any

from common.config_reader import get_device, list_device_identities
from common.constants import (
    AUDIT_ACTIONS,
    AUDIT_CATEGORIES,
    AUDIT_FORBIDDEN_FIELDS,
    AUDIT_LOG_MAX_ROWS,
    INITIAL_ACCOUNTS,
    PURPOSE_COUNTERS,
    PURPOSE_PTP,
    SETTING_CONFIG_VERSION,
    SETTING_DEFAULTS,
)
from common.database import transaction
from common.passwords import hash_password
from common.timeutil import utc_now_iso
from common.validation import DeviceData, DeviceForm, format_interface_list, validate_device_form


# ======================================================================
# 初期データ(8.3.3.2、8.3.4.2、AU-07)
# ======================================================================


def seed_initial_data(conn: sqlite3.Connection) -> None:
    """初期データを登録する。web の起動時に毎回呼んでよい。

    - 全体設定:まだない設定名だけを、既定値で登録する(登録済みの値は変えない)
    - ユーザー:ユーザーが1人もいないとき(初回起動時)だけ、初期アカウントを作る(7.4.1.4)
    """
    now = utc_now_iso()
    with transaction(conn):
        for key, value in SETTING_DEFAULTS.items():
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, now),
            )
        user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if user_count == 0:
            for username, password, role in INITIAL_ACCOUNTS:
                conn.execute(
                    "INSERT INTO users (username, password_hash, role, is_initial_password,"
                    " failed_count, locked_until, created_at, updated_at)"
                    " VALUES (?, ?, ?, 1, 0, NULL, ?, ?)",
                    (username, hash_password(password), role, now, now),
                )


# ======================================================================
# 設定の版番号(8.3.3.3、CF-01)
# ======================================================================


def bump_config_version(conn: sqlite3.Connection, now: str) -> None:
    """設定の版番号を1増やす。トランザクションの中で呼ぶこと。"""
    cursor = conn.execute(
        "UPDATE settings SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT), updated_at = ? WHERE key = ?",
        (now, SETTING_CONFIG_VERSION),
    )
    if cursor.rowcount != 1:
        raise LookupError("設定の版番号(config_version)が見つかりません")


# ======================================================================
# 機器(8.3.1、8.3.2)
# ======================================================================


def _insert_interfaces(conn: sqlite3.Connection, device_id: int, data: DeviceData) -> None:
    for purpose, interfaces in data.interfaces.items():
        conn.executemany(
            "INSERT INTO device_interfaces (device_id, purpose, slot, port) VALUES (?, ?, ?, ?)",
            [(device_id, purpose, interface.slot, interface.port) for interface in interfaces],
        )


def insert_device(conn: sqlite3.Connection, data: DeviceData, now: str) -> int:
    """機器を1台追加する。トランザクションの中で呼ぶこと。版番号は増やさない。"""
    cursor = conn.execute(
        "INSERT INTO devices (hostname, source_ip, device_type, cmd_counters_brief, cmd_ptp_brief,"
        " cmd_ptp_clock, cmd_ptp_parent, created_at, updated_at)"
        " VALUES (:hostname, :source_ip, :device_type, :cmd_counters_brief, :cmd_ptp_brief,"
        " :cmd_ptp_clock, :cmd_ptp_parent, :now, :now)",
        {
            "hostname": data.hostname,
            "source_ip": data.source_ip,
            "device_type": data.device_type,
            **{column: int(checked) for column, checked in data.commands.items()},
            "now": now,
        },
    )
    device_id = int(cursor.lastrowid)  # type: ignore[arg-type]
    _insert_interfaces(conn, device_id, data)
    return device_id


def device_audit_value(conn: sqlite3.Connection, device_id: int) -> dict[str, Any] | None:
    """変更履歴に記録する、機器1台分の登録内容(8.3.6)。"""
    record = get_device(conn, device_id)
    if record is None:
        return None
    return {
        "hostname": record.hostname,
        "source_ip": record.source_ip,
        **{column: int(checked) for column, checked in record.commands.items()},
        "interfaces_counters": format_interface_list(record.interfaces.get(PURPOSE_COUNTERS, ())),
        "interfaces_ptp": format_interface_list(record.interfaces.get(PURPOSE_PTP, ())),
    }


def save_device(
    conn: sqlite3.Connection,
    form: DeviceForm,
    device_id: int | None = None,
    *,
    audit_user: str | None = None,
) -> int:
    """画面から届いた機器の内容を検査して保存する(新規登録または編集)。

    検査と保存を1つのトランザクションで行い、重複の検査と保存の間に別の変更が入らないようにする。
    問題があれば ValidationError を出し、何も保存しない。保存した機器の id を返す。
    audit_user を指定した場合は、同じトランザクションで変更履歴を記録する(LG-05)。
    """
    now = utc_now_iso()
    with transaction(conn):
        before = None
        if device_id is not None:
            before = device_audit_value(conn, device_id)
            if before is None:
                raise LookupError(f"機器(id={device_id})が見つかりません")
        data = validate_device_form(form, list_device_identities(conn), self_id=device_id)

        if device_id is None:
            device_id = insert_device(conn, data, now)
        else:
            conn.execute(
                "UPDATE devices SET hostname = :hostname, source_ip = :source_ip, device_type = :device_type,"
                " cmd_counters_brief = :cmd_counters_brief, cmd_ptp_brief = :cmd_ptp_brief,"
                " cmd_ptp_clock = :cmd_ptp_clock, cmd_ptp_parent = :cmd_ptp_parent, updated_at = :now"
                " WHERE id = :id",
                {
                    "id": device_id,
                    "hostname": data.hostname,
                    "source_ip": data.source_ip,
                    "device_type": data.device_type,
                    **{column: int(checked) for column, checked in data.commands.items()},
                    "now": now,
                },
            )
            conn.execute("DELETE FROM device_interfaces WHERE device_id = ?", (device_id,))
            _insert_interfaces(conn, device_id, data)

        bump_config_version(conn, now)
        if audit_user is not None:
            insert_audit(
                conn,
                username=audit_user,
                category="device",
                action="create" if before is None else "update",
                target=data.hostname,
                before=before,
                after=device_audit_value(conn, device_id),
            )
    return device_id


def delete_device(conn: sqlite3.Connection, device_id: int, *, audit_user: str | None = None) -> bool:
    """機器を削除する。監視対象インターフェースも一緒に削除される(8.3.2)。削除したら True。"""
    now = utc_now_iso()
    with transaction(conn):
        before = device_audit_value(conn, device_id)
        cursor = conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
        if cursor.rowcount == 0:
            return False
        bump_config_version(conn, now)
        if audit_user is not None and before is not None:
            insert_audit(
                conn, username=audit_user, category="device", action="delete", target=before["hostname"],
                before=before, after=None,
            )  # fmt: skip
    return True


# ======================================================================
# 全体設定(8.3.3)
# ======================================================================


def write_settings(conn: sqlite3.Connection, values: Mapping[str, str], now: str) -> None:
    """検査済みの設定値を書き込む。トランザクションの中で呼ぶこと。版番号は増やさない。"""
    for key, value in values.items():
        if key == SETTING_CONFIG_VERSION:
            raise ValueError("設定の版番号は、直接書き換えられません")
        if key not in SETTING_DEFAULTS:
            raise ValueError(f"不明な設定名です:{key}")
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, value, now),
        )


def update_settings(
    conn: sqlite3.Connection,
    values: Mapping[str, str],
    *,
    audit_user: str | None = None,
    audit_category: str = "settings",
) -> None:
    """検査済みの設定値を保存し、設定の版番号を1増やす(CF-01)。

    値は common.validation.validate_settings で検査したものを渡すこと。
    audit_user を指定した場合は、変わった設定名と、変更前後の値を変更履歴に記録する(LG-05)。
    値が1つも変わっていない場合は、変更履歴は記録しない。
    """
    now = utc_now_iso()
    with transaction(conn):
        current = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}
        changed = {key: value for key, value in values.items() if current.get(key) != value}
        write_settings(conn, values, now)
        bump_config_version(conn, now)
        if audit_user is not None and changed:
            insert_audit(
                conn,
                username=audit_user,
                category=audit_category,
                action="update",
                target=", ".join(sorted(changed)),
                before={key: current.get(key) for key in sorted(changed)},
                after={key: changed[key] for key in sorted(changed)},
            )


# ======================================================================
# 変更履歴(8.3.6、LG-06)
# ======================================================================


def _to_audit_json(value: Mapping[str, Any] | None) -> str | None:
    if value is None:
        return None
    forbidden = AUDIT_FORBIDDEN_FIELDS & set(value)
    if forbidden:
        # パスワードやハッシュ値は、変更履歴に決して含めない(8.3.6.1)
        raise ValueError("変更履歴に、パスワードに関する項目は含められません")
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def insert_audit(
    conn: sqlite3.Connection,
    *,
    username: str,
    category: str,
    action: str,
    target: str | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    max_rows: int = AUDIT_LOG_MAX_ROWS,
) -> None:
    """変更履歴を1件記録し、上限を超えた分を古いものから削除する。トランザクションの中で呼ぶこと。

    設定の変更と同じトランザクションで記録できるよう、トランザクションは呼び出す側で始める。
    """
    if category not in AUDIT_CATEGORIES:
        raise ValueError(f"不明な画面の区分です:{category}")
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"不明な操作の種類です:{action}")
    conn.execute(
        "INSERT INTO audit_log (timestamp, username, category, action, target, before_value, after_value)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (utc_now_iso(), username, category, action, target, _to_audit_json(before), _to_audit_json(after)),
    )
    trim_audit_log(conn, max_rows)


def trim_audit_log(conn: sqlite3.Connection, max_rows: int = AUDIT_LOG_MAX_ROWS) -> None:
    """変更履歴が上限を超えていたら、古いもの(idの小さいもの)から削除する(8.3.6.2)。"""
    conn.execute(
        "DELETE FROM audit_log WHERE id NOT IN (SELECT id FROM audit_log ORDER BY id DESC LIMIT ?)",
        (max_rows,),
    )


def record_audit(
    conn: sqlite3.Connection,
    *,
    username: str,
    category: str,
    action: str,
    target: str | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    max_rows: int = AUDIT_LOG_MAX_ROWS,
) -> None:
    """変更履歴を1件記録する(単独のトランザクションで行う)。"""
    with transaction(conn):
        insert_audit(
            conn,
            username=username,
            category=category,
            action=action,
            target=target,
            before=before,
            after=after,
            max_rows=max_rows,
        )
