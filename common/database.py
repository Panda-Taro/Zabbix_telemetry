"""SQLiteデータベースへの接続と、テーブルの作成(⑧)。

テーブル名と列名は、要件定義書の⑧に書かれた名前をそのまま使う。
テーブルごとに書き込むコンテナを1つに決める(8.1.3):
  - 設定データのテーブル(devices、device_interfaces、settings、users、sessions、audit_log)は web だけが書き込む
  - 状態データのテーブル(device_status、system_status、unknown_sources)は collector だけが書き込む
"""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from common.constants import SQLITE_BUSY_TIMEOUT_SEC

SCHEMA_SQL = """
-- ===== 設定データ(書き込むのは web だけ) =====

-- 登録した機器(8.3.1)
-- id は削除後も使い回さない(AUTOINCREMENT)。状態データの device_id と取り違えないため
-- hostname の重複は、大文字・小文字を区別せずに判定する(COLLATE NOCASE)
CREATE TABLE IF NOT EXISTS devices (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    hostname            TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    source_ip           TEXT    NOT NULL UNIQUE,
    device_type         TEXT    NOT NULL,
    cmd_counters_brief  INTEGER NOT NULL CHECK (cmd_counters_brief IN (0, 1)),
    cmd_ptp_brief       INTEGER NOT NULL CHECK (cmd_ptp_brief IN (0, 1)),
    cmd_ptp_clock       INTEGER NOT NULL CHECK (cmd_ptp_clock IN (0, 1)),
    cmd_ptp_parent      INTEGER NOT NULL CHECK (cmd_ptp_parent IN (0, 1)),
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL
);

-- 機器ごとの監視対象インターフェース(8.3.2)
CREATE TABLE IF NOT EXISTS device_interfaces (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   INTEGER NOT NULL REFERENCES devices (id) ON DELETE CASCADE,
    purpose     TEXT    NOT NULL CHECK (purpose IN ('counters', 'ptp')),
    slot        INTEGER NOT NULL,
    port        INTEGER NOT NULL,
    UNIQUE (device_id, purpose, slot, port)
);

-- 全体設定(8.3.3)
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- WebGUIのユーザー(8.3.4)
CREATE TABLE IF NOT EXISTS users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT    NOT NULL UNIQUE,
    password_hash        TEXT    NOT NULL,
    role                 TEXT    NOT NULL CHECK (role IN ('admin', 'viewer')),
    is_initial_password  INTEGER NOT NULL DEFAULT 0 CHECK (is_initial_password IN (0, 1)),
    failed_count         INTEGER NOT NULL DEFAULT 0,
    locked_until         TEXT,
    created_at           TEXT    NOT NULL,
    updated_at           TEXT    NOT NULL
);

-- ログイン中のセッション(8.3.5)
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    created_at  TEXT    NOT NULL
);

-- 変更履歴(8.3.6)
CREATE TABLE IF NOT EXISTS audit_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,
    username      TEXT NOT NULL,
    category      TEXT NOT NULL,
    action        TEXT NOT NULL,
    target        TEXT,
    before_value  TEXT,
    after_value   TEXT
);

-- ===== 状態データ(書き込むのは collector だけ) =====

-- 機器ごとの受信状態(8.4.3)
-- devices を参照する外部キーは付けない。付けると、web が機器を削除したときに
-- この表の行も web によって消され、「状態データは collector だけが書き込む」(8.1.3)に反するため
CREATE TABLE IF NOT EXISTS device_status (
    device_id               INTEGER PRIMARY KEY,
    status                  TEXT NOT NULL CHECK (status IN ('ok', 'stale', 'waiting')),
    last_received_at        TEXT,
    last_counters_brief_at  TEXT,
    last_ptp_brief_at       TEXT,
    last_ptp_clock_at       TEXT,
    last_ptp_parent_at      TEXT,
    mismatch_interfaces     TEXT,
    updated_at              TEXT NOT NULL
);

-- 本システム全体の状態(8.4.4)。行は1行のみ
CREATE TABLE IF NOT EXISTS system_status (
    id                      INTEGER PRIMARY KEY CHECK (id = 1),
    collector_heartbeat_at  TEXT,
    collector_started_at    TEXT,
    applied_config_version  INTEGER,
    config_error            TEXT,
    zabbix_connected        INTEGER CHECK (zabbix_connected IN (0, 1)),
    received_per_min        INTEGER,
    sent_ok_per_min         INTEGER,
    sent_failed_per_min     INTEGER,
    dropped_per_min         TEXT,
    skipped_per_min         INTEGER,
    queue_size              INTEGER
);

-- 未登録の送信元(8.4.5)
CREATE TABLE IF NOT EXISTS unknown_sources (
    source_ip         TEXT    PRIMARY KEY,
    node_id           TEXT,
    count             INTEGER NOT NULL,
    last_received_at  TEXT    NOT NULL
);
"""

# 設定データのテーブル(web だけが書き込む)
CONFIG_TABLES: tuple[str, ...] = ("devices", "device_interfaces", "settings", "users", "sessions", "audit_log")
# 状態データのテーブル(collector だけが書き込む)
STATUS_TABLES: tuple[str, ...] = ("device_status", "system_status", "unknown_sources")


def connect(path: str | Path) -> sqlite3.Connection:
    """データベースに接続する。

    - WALモードにする(4.2)
    - 外部キー(機器を削除したらインターフェースも削除、など)を有効にする
    - 自動でのトランザクション開始は行わず、書き込みは transaction() の中で行う
    """
    conn = sqlite3.connect(str(path), timeout=SQLITE_BUSY_TIMEOUT_SEC, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def initialize_schema(conn: sqlite3.Connection) -> None:
    """⑧のすべてのテーブルを作る。すでにある場合は何もしない。

    テーブルの作成は、collector と web のどちらが先に起動しても行えるようにする。
    初期データ(既定値、初期アカウント)の登録は、web だけが行う(config_writer.seed_initial_data)。
    """
    conn.executescript(SCHEMA_SQL)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """書き込みのまとまりを1つのトランザクションで行う。

    途中で例外が起きた場合は、すべて取り消す(途中まで書き換わった状態を作らない、8.6.4)。
    """
    if conn.in_transaction:
        raise RuntimeError("トランザクションの中で、さらにトランザクションを始めることはできません")
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
