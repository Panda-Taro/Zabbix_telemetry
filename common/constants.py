"""本システム全体で使う定数を、1か所にまとめる(要件定義書 11.4.3)。

利用者がWebGUIで変更できる設定値(ポート番号、間隔など)は、ここではなく
データベースの settings テーブルで持つ。ここに置くのは、仕様として固定の値だけとする。
"""

from typing import Final

# ---- showコマンド名(6.3.1)。判別は完全一致で行う(6.4.2) ----
CMD_COUNTERS_BRIEF: Final[str] = "show interface counters brief"
CMD_PTP_BRIEF: Final[str] = "show ptp brief"
CMD_PTP_CLOCK: Final[str] = "show ptp clock"
CMD_PTP_PARENT: Final[str] = "show ptp parent"

# ---- 監視対象インターフェースの用途(8.3.2 device_interfaces.purpose) ----
PURPOSE_COUNTERS: Final[str] = "counters"
PURPOSE_PTP: Final[str] = "ptp"

# ---- インターフェース名 ----
# 登録した番号(例:1/1)から、機器での名前(例:Ethernet1/1)を組み立てるときの頭の文字(6.3.5)
ETHERNET_NAME_PREFIX: Final[str] = "Ethernet"
# 機器から届くインターフェース名として読み取る文字列の長さの上限(外部データの大きさの検査、11.3.3)
MAX_DEVICE_IFNAME_LENGTH: Final[int] = 64

# ---- Telemetryの受信データ(9.1.1) ----
# 1回分のデータの大きさの上限(バイト)。1つのUDPパケットに収まる前提(9.1.1.2)
MAX_TELEMETRY_PAYLOAD_BYTES: Final[int] = 65535

# ---- Zabbixへ送る値 ----
# 文字列の値の長さの上限(文字数)。Zabbixの文字列型アイテムが受け付ける長さに合わせる
MAX_TEXT_VALUE_LENGTH: Final[int] = 255
# 数値として読み取る文字列の長さの上限(外部データの大きさの検査、11.3.3)
MAX_NUMERIC_TEXT_LENGTH: Final[int] = 64
# 帯域の換算:MB/秒 → bps(9.2.3.2)
BANDWIDTH_MBPS_TO_BPS: Final[int] = 8_000_000

# ---- LLD(9.2.4) ----
LLD_KEY_SUFFIX: Final[str] = "discovery"
LLD_MACRO_IFNUM: Final[str] = "{#IFNUM}"
LLD_MACRO_IFNAME: Final[str] = "{#IFNAME}"

# ======================================================================
# データベース(⑧)
# ======================================================================

# SQLiteデータベースの置き場所(8.1.2)
DEFAULT_DB_PATH: Final[str] = "/data/telemetry.db"
# 他方のコンテナが書き込み中のときに待つ時間(秒)
SQLITE_BUSY_TIMEOUT_SEC: Final[float] = 5.0

# ---- 機器(8.3.1) ----
DEVICE_TYPE_CISCO_NEXUS: Final[str] = "cisco_nexus"

# showコマンド名と、devices テーブルの列名の対応(8.3.1)
COMMAND_COLUMNS: Final[dict[str, str]] = {
    CMD_COUNTERS_BRIEF: "cmd_counters_brief",
    CMD_PTP_BRIEF: "cmd_ptp_brief",
    CMD_PTP_CLOCK: "cmd_ptp_clock",
    CMD_PTP_PARENT: "cmd_ptp_parent",
}
# 監視対象インターフェースの用途と、そのインターフェースを使うコマンドの列名の対応(8.3.2)
PURPOSE_COMMAND_COLUMNS: Final[dict[str, str]] = {
    PURPOSE_COUNTERS: "cmd_counters_brief",
    PURPOSE_PTP: "cmd_ptp_brief",
}

# ---- 機器登録の入力の検査(6.3.7、7.4.4.3) ----
SLOT_MIN: Final[int] = 1
SLOT_MAX: Final[int] = 99
PORT_MIN: Final[int] = 1
PORT_MAX: Final[int] = 256
# インターフェース番号の入力欄の文字数の上限(外部データの大きさの検査、11.3.3)
MAX_INTERFACE_INPUT_LENGTH: Final[int] = 4096
# ホスト名の文字数の上限(Zabbixのホスト名と同じ)
HOSTNAME_MAX_LENGTH: Final[int] = 128
# 本システム自身のZabbixホスト名。機器のホスト名には使えない(5.4.4、7.4.4.3)
SELF_ZABBIX_HOSTNAME: Final[str] = "telemetry"
# 送信元IPアドレスの入力欄の文字数の上限(外部データの大きさの検査、11.3.3)
MAX_IP_INPUT_LENGTH: Final[int] = 64

# ---- 全体設定(8.3.3) ----
SETTING_ZABBIX_SERVER: Final[str] = "zabbix_server"
SETTING_ZABBIX_PORT: Final[str] = "zabbix_port"
SETTING_TELEMETRY_UDP_PORT: Final[str] = "telemetry_udp_port"
SETTING_STALE_TIMEOUT_SEC: Final[str] = "stale_timeout_sec"
SETTING_SELF_MONITOR_INTERVAL_SEC: Final[str] = "self_monitor_interval_sec"
SETTING_LLD_INTERVAL_SEC: Final[str] = "lld_interval_sec"
SETTING_QUEUE_MAX_SIZE: Final[str] = "queue_max_size"
SETTING_LOG_LEVEL: Final[str] = "log_level"
SETTING_WEBGUI_PORT: Final[str] = "webgui_port"
SETTING_CONFIG_VERSION: Final[str] = "config_version"

# 設定名と既定値(8.3.3.2)。値はすべて文字列で保存する(8.3.3.1)
SETTING_DEFAULTS: Final[dict[str, str]] = {
    SETTING_ZABBIX_SERVER: "",
    SETTING_ZABBIX_PORT: "10051",
    SETTING_TELEMETRY_UDP_PORT: "50051",
    SETTING_STALE_TIMEOUT_SEC: "300",
    SETTING_SELF_MONITOR_INTERVAL_SEC: "60",
    SETTING_LLD_INTERVAL_SEC: "3600",
    SETTING_QUEUE_MAX_SIZE: "100000",
    SETTING_LOG_LEVEL: "INFO",
    SETTING_WEBGUI_PORT: "8080",
    SETTING_CONFIG_VERSION: "1",
}

LOG_LEVELS: Final[tuple[str, ...]] = ("DEBUG", "INFO", "WARNING", "ERROR")
TCP_UDP_PORT_MIN: Final[int] = 1
TCP_UDP_PORT_MAX: Final[int] = 65535
# 時間・件数の設定値として受け付ける最大値(桁あふれを防ぐための上限)
SETTING_INTEGER_MAX: Final[int] = 2_147_483_647

# ---- ユーザー(8.3.4) ----
ROLE_ADMIN: Final[str] = "admin"
ROLE_VIEWER: Final[str] = "viewer"
# 初期アカウント(7.4.1.4)。(ユーザー名, パスワード, 権限)
INITIAL_ACCOUNTS: Final[tuple[tuple[str, str, str], ...]] = (
    ("admin", "admin", ROLE_ADMIN),
    ("user", "", ROLE_VIEWER),
)

# パスワードのハッシュ化(scrypt、8.3.4.1)の条件
SCRYPT_N: Final[int] = 2**14
SCRYPT_R: Final[int] = 8
SCRYPT_P: Final[int] = 1
SCRYPT_SALT_BYTES: Final[int] = 16
SCRYPT_DKLEN: Final[int] = 64

# ---- 変更履歴(8.3.6) ----
AUDIT_LOG_MAX_ROWS: Final[int] = 100_000
AUDIT_CATEGORIES: Final[tuple[str, ...]] = ("device", "settings", "system", "user", "auth", "restore")
AUDIT_ACTIONS: Final[tuple[str, ...]] = ("create", "update", "delete", "login", "logout", "login_failed", "restore")
# 変更履歴の変更前後の値に、決して含めない項目名(8.3.6.1)
AUDIT_FORBIDDEN_FIELDS: Final[frozenset[str]] = frozenset({"password", "password_hash"})

# ---- バックアップファイル(8.6) ----
BACKUP_FORMAT_VERSION: Final[int] = 1
# バックアップファイルの大きさの上限(バイト)(外部データの大きさの検査、11.3.3)
MAX_BACKUP_BYTES: Final[int] = 5 * 1024 * 1024
# バックアップに含めない設定名(8.6.2)
BACKUP_EXCLUDED_SETTINGS: Final[frozenset[str]] = frozenset({SETTING_WEBGUI_PORT, SETTING_CONFIG_VERSION})
