"""collector が使う設定の読み込み(6.7、CF-02、CF-05)。

データベースから設定データを読み、collector の中で使いやすい形にする。
読み込みに失敗した場合は ConfigLoadError を出す。呼び出す側は、直前の設定のまま動作を続ける(6.7.3)。
"""

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from collector.cisco_nexus.commands import COMMANDS
from common.config_reader import get_settings, list_devices
from common.constants import (
    COMMAND_COLUMNS,
    SETTING_CONFIG_VERSION,
    SETTING_DEFAULTS,
    SETTING_LLD_INTERVAL_SEC,
    SETTING_LOG_LEVEL,
    SETTING_QUEUE_MAX_SIZE,
    SETTING_SELF_MONITOR_INTERVAL_SEC,
    SETTING_STALE_TIMEOUT_SEC,
    SETTING_TELEMETRY_UDP_PORT,
    SETTING_ZABBIX_PORT,
    SETTING_ZABBIX_SERVER,
)
from common.interfaces import InterfaceId
from common.validation import ValidationError, validate_settings

# 設定の版番号がまだない(web がまだ初期データを登録していない)ときの版番号
NO_CONFIG_VERSION = 0


class ConfigLoadError(Exception):
    """設定の読み込みに失敗した。"""


@dataclass(frozen=True)
class DeviceConfig:
    """collector が使う、機器1台分の登録内容。"""

    id: int
    hostname: str
    source_ip: str
    device_type: str
    commands: frozenset[str]
    """チェックした showコマンド名。"""
    targets: Mapping[str, tuple[InterfaceId, ...]] = field(default_factory=dict)
    """showコマンド名 → 監視対象インターフェース(インターフェースごとのコマンドで、チェックしたものだけ)。"""

    def targets_for(self, command: str) -> tuple[InterfaceId, ...]:
        return self.targets.get(command, ())

    def lld_signature(self) -> tuple:
        """LLD用データを送り直す必要があるかを判断するための、登録内容の要約(6.5.5)。"""
        return (self.hostname, self.commands, tuple(sorted(self.targets.items())))


@dataclass(frozen=True)
class CollectorConfig:
    """collector が使う設定の全体。"""

    version: int
    zabbix_server: str
    zabbix_port: int
    telemetry_udp_port: int
    stale_timeout_sec: int
    self_monitor_interval_sec: int
    lld_interval_sec: int
    queue_max_size: int
    log_level: str
    devices: tuple[DeviceConfig, ...] = ()

    @property
    def devices_by_ip(self) -> Mapping[str, DeviceConfig]:
        return MappingProxyType({device.source_ip: device for device in self.devices})


def build_config(settings: Mapping[str, str], devices: tuple[DeviceConfig, ...]) -> CollectorConfig:
    """設定値を検査して、CollectorConfig を作る。値が正しくなければ ConfigLoadError。

    まだ登録されていない設定名は、既定値を使う(web が初期データを登録する前に起動した場合など)。
    """
    merged = {key: value for key, value in SETTING_DEFAULTS.items() if key != SETTING_CONFIG_VERSION}
    merged.update({key: value for key, value in settings.items() if key != SETTING_CONFIG_VERSION})
    try:
        values = validate_settings(merged)
    except ValidationError as exc:
        raise ConfigLoadError("設定の値が正しくありません:" + " / ".join(exc.messages())) from None
    version_text = settings.get(SETTING_CONFIG_VERSION, str(NO_CONFIG_VERSION))
    try:
        version = int(version_text)
    except ValueError:
        raise ConfigLoadError(f"設定の版番号が正しくありません:{version_text[:20]}") from None
    return CollectorConfig(
        version=version,
        zabbix_server=values[SETTING_ZABBIX_SERVER],
        zabbix_port=int(values[SETTING_ZABBIX_PORT]),
        telemetry_udp_port=int(values[SETTING_TELEMETRY_UDP_PORT]),
        stale_timeout_sec=int(values[SETTING_STALE_TIMEOUT_SEC]),
        self_monitor_interval_sec=int(values[SETTING_SELF_MONITOR_INTERVAL_SEC]),
        lld_interval_sec=int(values[SETTING_LLD_INTERVAL_SEC]),
        queue_max_size=int(values[SETTING_QUEUE_MAX_SIZE]),
        log_level=values[SETTING_LOG_LEVEL],
        devices=devices,
    )


def read_config_version(conn: sqlite3.Connection) -> int:
    """設定の版番号だけを読む。まだない場合は NO_CONFIG_VERSION。"""
    try:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTING_CONFIG_VERSION,)).fetchone()
    except sqlite3.Error as exc:
        raise ConfigLoadError(f"データベースを読めません:{exc}") from None
    if row is None:
        return NO_CONFIG_VERSION
    try:
        return int(row["value"])
    except ValueError:
        raise ConfigLoadError("設定の版番号が正しくありません") from None


def load_config(conn: sqlite3.Connection) -> CollectorConfig:
    """データベースから設定データを読み、CollectorConfig を作る(CF-02)。

    設定と機器を同じ時点の内容で読むため、読み込み用のトランザクションの中で読む。
    """
    try:
        conn.execute("BEGIN")
        try:
            settings = get_settings(conn)
            records = list_devices(conn)
        finally:
            conn.execute("ROLLBACK")
    except sqlite3.Error as exc:
        raise ConfigLoadError(f"データベースを読めません:{exc}") from None

    devices = []
    for record in records:
        commands = frozenset(name for name, column in COMMAND_COLUMNS.items() if record.commands.get(column))
        targets = {}
        for name in commands:
            purpose = COMMANDS[name].interface_purpose
            if purpose is not None:
                targets[name] = record.interfaces.get(purpose, ())
        devices.append(
            DeviceConfig(
                id=record.id,
                hostname=record.hostname,
                source_ip=record.source_ip,
                device_type=record.device_type,
                commands=commands,
                targets=MappingProxyType(targets),
            )
        )
    return build_config(settings, tuple(devices))
