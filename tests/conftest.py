"""自動テストで共通に使う部品。

テストデータは tests/fixtures/ の架空の値に置き換えたJSONだけを使う。
"""

import copy
import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures"

FIXTURE_FILES = {
    "show interface counters brief": "show_interface_counters_brief.json",
    "show ptp brief": "show_ptp_brief.json",
    "show ptp clock": "show_ptp_clock.json",
    "show ptp parent": "show_ptp_parent.json",
}

# テストデータ内の架空のホスト名
TEST_HOSTNAME = "NEXUS-TEST-01"


def fixture_bytes(command: str) -> bytes:
    """テストデータを、受信したときと同じバイト列として読み込む。"""
    return (FIXTURES_DIR / FIXTURE_FILES[command]).read_bytes()


def fixture_document(command: str) -> dict[str, Any]:
    """テストデータを、書き換えてよい辞書として読み込む。"""
    return copy.deepcopy(json.loads(fixture_bytes(command).decode("utf-8")))


def to_payload(document: dict[str, Any], header: bytes = b"") -> bytes:
    """辞書を、UDPパケットの中身と同じ形のバイト列にする。"""
    return header + json.dumps(document, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def load_document():
    return fixture_document


@pytest.fixture
def db(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """テストごとに、一時フォルダに新しいデータベースを作り、初期データを登録する。"""
    from common.config_writer import seed_initial_data
    from common.database import connect, initialize_schema

    conn = connect(tmp_path / "telemetry.db")
    initialize_schema(conn)
    seed_initial_data(conn)
    try:
        yield conn
    finally:
        conn.close()
