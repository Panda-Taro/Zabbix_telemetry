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
