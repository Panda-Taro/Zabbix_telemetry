"""設定のバックアップと復元(7.4.7.6、7.4.7.7、8.6、BK-01〜BK-04)。web だけが使う。

バックアップファイルの形(JSON):
{
  "format_version": 1,
  "exported_at": "2026-01-01T00:00:00.000+00:00",
  "devices": [
    {
      "hostname": "NEXUS-TEST-01",
      "source_ip": "192.0.2.1",
      "device_type": "cisco_nexus",
      "cmd_counters_brief": 1, "cmd_ptp_brief": 1, "cmd_ptp_clock": 1, "cmd_ptp_parent": 1,
      "interfaces": [ {"purpose": "counters", "slot": 1, "port": 1}, ... ]
    }
  ],
  "settings": { "zabbix_server": "192.0.2.100", "zabbix_port": "10051", ... }
}
項目名は、テーブルの列名(⑧)と同じにしている。
ユーザー、セッション、変更履歴、状態データ、本体のIPアドレス、WebGUIのポート番号は含めない(8.6.3)。
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from common.config_reader import get_settings, list_devices
from common.config_writer import bump_config_version, insert_device, write_settings
from common.constants import (
    BACKUP_EXCLUDED_SETTINGS,
    BACKUP_FORMAT_VERSION,
    COMMAND_COLUMNS,
    DEVICE_TYPE_CISCO_NEXUS,
    MAX_BACKUP_BYTES,
    PURPOSE_COMMAND_COLUMNS,
    SETTING_DEFAULTS,
)
from common.database import transaction
from common.interfaces import InterfaceId
from common.timeutil import utc_now_iso
from common.validation import DeviceData, DeviceIdentity, ValidationError, build_device, check_interface_range, validate_settings

# バックアップに含める設定名(8.6.2)
BACKUP_SETTING_KEYS: tuple[str, ...] = tuple(key for key in SETTING_DEFAULTS if key not in BACKUP_EXCLUDED_SETTINGS)

_TOP_LEVEL_KEYS = frozenset({"format_version", "exported_at", "devices", "settings"})
_DEVICE_KEYS = frozenset({"hostname", "source_ip", "device_type", "interfaces", *COMMAND_COLUMNS.values()})
_INTERFACE_KEYS = frozenset({"purpose", "slot", "port"})


class BackupError(Exception):
    """バックアップファイルに問題がある。errors にエラーの文言の一覧が入る。"""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(" / ".join(errors))


@dataclass(frozen=True)
class BackupContent:
    """検査に合格したバックアップファイルの中身。"""

    devices: tuple[DeviceData, ...]
    settings: dict[str, str]


# ======================================================================
# 書き出し(BK-01、BK-02)
# ======================================================================


def export_backup(conn: sqlite3.Connection) -> dict[str, Any]:
    """機器の登録内容と全体設定を、バックアップファイルの形にする。"""
    devices = []
    for record in list_devices(conn):
        interfaces = [
            {"purpose": purpose, "slot": interface.slot, "port": interface.port}
            for purpose in PURPOSE_COMMAND_COLUMNS
            for interface in record.interfaces.get(purpose, ())
        ]
        devices.append(
            {
                "hostname": record.hostname,
                "source_ip": record.source_ip,
                "device_type": record.device_type,
                **{column: int(record.commands[column]) for column in COMMAND_COLUMNS.values()},
                "interfaces": interfaces,
            }
        )
    settings = get_settings(conn)
    return {
        "format_version": BACKUP_FORMAT_VERSION,
        "exported_at": utc_now_iso(),
        "devices": devices,
        "settings": {key: settings[key] for key in BACKUP_SETTING_KEYS if key in settings},
    }


def export_backup_bytes(conn: sqlite3.Connection) -> bytes:
    """バックアップファイルの中身を、ファイルに書き出す形(UTF-8のJSON)にする。"""
    return json.dumps(export_backup(conn), ensure_ascii=False, indent=2).encode("utf-8")


# ======================================================================
# 検査(BK-04)
# ======================================================================


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_interfaces(raw: object, label: str, errors: list[str]) -> dict[str, tuple[InterfaceId, ...]] | None:
    if not isinstance(raw, list):
        errors.append(f"{label}:interfaces は一覧の形で指定してください")
        return None
    result: dict[str, list[InterfaceId]] = {purpose: [] for purpose in PURPOSE_COMMAND_COLUMNS}
    ok = True
    for index, item in enumerate(raw, start=1):
        where = f"{label}:interfaces の {index} 件目"
        if not isinstance(item, dict) or set(item) != _INTERFACE_KEYS:
            errors.append(f"{where}は、purpose・slot・port の3項目で指定してください")
            ok = False
            continue
        purpose, slot, port = item["purpose"], item["slot"], item["port"]
        if purpose not in result:
            errors.append(f"{where}:purpose は counters か ptp を指定してください")
            ok = False
            continue
        if not _is_int(slot) or not _is_int(port):
            errors.append(f"{where}:slot と port は整数で指定してください")
            ok = False
            continue
        problem = check_interface_range(slot, port)
        if problem is not None:
            errors.append(f"{where}:{problem}")
            ok = False
            continue
        interface = InterfaceId(slot, port)
        if interface in result[purpose]:
            errors.append(f"{where}:{purpose} の {interface.ifnum} が重複しています")
            ok = False
            continue
        result[purpose].append(interface)
    if not ok:
        return None
    return {purpose: tuple(sorted(values)) for purpose, values in result.items()}


def _parse_device(raw: object, index: int, accepted: list[DeviceIdentity], errors: list[str]) -> DeviceData | None:
    label = f"{index}台目の機器"
    if not isinstance(raw, dict):
        errors.append(f"{label}:形式が正しくありません")
        return None
    missing = _DEVICE_KEYS - set(raw)
    unknown = set(raw) - _DEVICE_KEYS
    if missing:
        errors.append(f"{label}:項目が足りません({'、'.join(sorted(missing))})")
    if unknown:
        errors.append(f"{label}:不明な項目があります")
    if missing or unknown:
        return None

    hostname, source_ip = raw["hostname"], raw["source_ip"]
    if isinstance(hostname, str):
        label = f"{index}台目の機器({hostname[:32]})"
    local_errors: list[str] = []
    if not isinstance(hostname, str) or not isinstance(source_ip, str):
        local_errors.append(f"{label}:hostname と source_ip は文字列で指定してください")
    if raw["device_type"] != DEVICE_TYPE_CISCO_NEXUS:
        local_errors.append(f"{label}:device_type は {DEVICE_TYPE_CISCO_NEXUS} を指定してください")
    commands: dict[str, bool] = {}
    for column in COMMAND_COLUMNS.values():
        value = raw[column]
        if not _is_int(value) or value not in (0, 1):
            local_errors.append(f"{label}:{column} は 0 か 1 で指定してください")
        else:
            commands[column] = value == 1
    interfaces = _parse_interfaces(raw["interfaces"], label, local_errors)
    if local_errors or interfaces is None:
        errors.extend(local_errors)
        return None

    try:
        data = build_device(
            hostname=hostname,
            source_ip=source_ip,
            commands=commands,
            interfaces=interfaces,
            existing=accepted,
            reject_unchecked_interfaces=True,
        )
    except ValidationError as exc:
        errors.extend(f"{label}:{message}" for message in exc.messages())
        return None
    if data.source_ip != source_ip:
        errors.append(f"{label}:送信元IPアドレスの表記が正しくありません(例:192.0.2.1)")
        return None
    accepted.append(DeviceIdentity(id=None, hostname=data.hostname, source_ip=data.source_ip))
    return data


def parse_backup(raw: bytes) -> BackupContent:
    """バックアップファイルの中身をすべて検査する。1か所でも問題があれば BackupError を出す(8.6.4)。

    この関数はデータベースを変更しない。
    """
    if len(raw) > MAX_BACKUP_BYTES:
        raise BackupError([f"ファイルが大きすぎます({MAX_BACKUP_BYTES // (1024 * 1024)}MBまで)"])
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except UnicodeDecodeError:
        raise BackupError(["ファイルの文字コードがUTF-8ではありません"]) from None
    except RecursionError:
        raise BackupError(["ファイルの形式が正しくありません(入れ子が深すぎます)"]) from None
    except ValueError:
        raise BackupError(["ファイルがJSONの形式ではありません"]) from None

    if not isinstance(document, dict):
        raise BackupError(["バックアップファイルの形式が正しくありません"])
    errors: list[str] = []
    missing = _TOP_LEVEL_KEYS - set(document)
    if missing:
        errors.append(f"項目が足りません({'、'.join(sorted(missing))})")
    if set(document) - _TOP_LEVEL_KEYS:
        errors.append("不明な項目があります")
    if errors:
        raise BackupError(errors)

    version = document["format_version"]
    if not _is_int(version) or version != BACKUP_FORMAT_VERSION:
        raise BackupError([f"対応していない形式の版番号です(対応している版:{BACKUP_FORMAT_VERSION})"])

    exported_at = document["exported_at"]
    try:
        if not isinstance(exported_at, str):
            raise ValueError
        datetime.fromisoformat(exported_at)
    except ValueError:
        errors.append("exported_at の日時の形式が正しくありません")

    devices: list[DeviceData] = []
    raw_devices = document["devices"]
    if not isinstance(raw_devices, list):
        errors.append("devices は一覧の形で指定してください")
    else:
        accepted: list[DeviceIdentity] = []
        for index, raw_device in enumerate(raw_devices, start=1):
            data = _parse_device(raw_device, index, accepted, errors)
            if data is not None:
                devices.append(data)

    settings: dict[str, str] = {}
    raw_settings = document["settings"]
    if not isinstance(raw_settings, dict):
        errors.append("settings は { } の形で指定してください")
    else:
        excluded = BACKUP_EXCLUDED_SETTINGS & set(raw_settings)
        if excluded:
            errors.append(f"settings に、バックアップの対象外の設定があります({'、'.join(sorted(excluded))})")
        else:
            try:
                settings = validate_settings(raw_settings, required_keys=BACKUP_SETTING_KEYS)
            except ValidationError as exc:
                errors.extend(f"settings:{message}" for message in exc.messages())

    if errors:
        raise BackupError(errors)
    return BackupContent(devices=tuple(devices), settings=settings)


# ======================================================================
# 復元(BK-03)
# ======================================================================


def restore_backup(conn: sqlite3.Connection, content: BackupContent) -> None:
    """検査済みのバックアップの内容で、機器の登録内容と全体設定を置き換える(7.4.7.7)。

    すべてを1つのトランザクションで行い、途中で失敗した場合は何も変更しない(8.6.4)。
    ユーザー、WebGUIのポート番号などは変えない(7.4.7.6)。設定の版番号は1増やす(CF-01)。
    """
    now = utc_now_iso()
    with transaction(conn):
        conn.execute("DELETE FROM devices")  # 監視対象インターフェースも一緒に削除される
        for data in content.devices:
            insert_device(conn, data, now)
        write_settings(conn, content.settings, now)
        bump_config_version(conn, now)


def restore_backup_bytes(conn: sqlite3.Connection, raw: bytes) -> BackupContent:
    """バックアップファイルを検査し、問題がなければ復元する。問題があれば何も変更せずに BackupError。"""
    content = parse_backup(raw)
    restore_backup(conn, content)
    return content
