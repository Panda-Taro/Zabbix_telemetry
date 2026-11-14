"""ダッシュボードの表示と、外部から届いた文字列の無害化の自動テスト(段階5、UI-04〜UI-06、11.3.3)。

状態データは、collector の代わりにテストが直接書き込む。
"""

import json
import re
from datetime import timedelta
from pathlib import Path

import pytest

from markupsafe import escape

from common.config_writer import save_device, update_settings
from common.database import connect, transaction
from common.validation import DeviceForm
from tests.web_support import Browser, FakeNow, WebServer
from web.dashboard import elapsed_text

ALL = {"cmd_counters_brief": True, "cmd_ptp_brief": True, "cmd_ptp_clock": True, "cmd_ptp_parent": True}


def iso(moment) -> str:  # noqa: ANN001
    return moment.isoformat(timespec="milliseconds")


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "telemetry.db"
    now = FakeNow()
    server = WebServer(db, now)
    server.start()
    client = Browser(server.base_url)
    client.login("admin", "admin")
    try:
        yield server, db, now, client
    finally:
        server.stop()


def write_system_status(db: Path, now, **overrides) -> None:  # noqa: ANN001
    row = {
        "id": 1,
        "collector_heartbeat_at": iso(now),
        "collector_started_at": iso(now - timedelta(hours=1)),
        "applied_config_version": 1,
        "config_error": None,
        "zabbix_connected": 1,
        "received_per_min": 1234,
        "sent_ok_per_min": 5678,
        "sent_failed_per_min": 0,
        "dropped_per_min": json.dumps(
            {"unknown_source": 0, "invalid_data": 0, "unknown_command": 0, "queue_full": 0, "zabbix_unreachable": 0}
        ),
        "skipped_per_min": 7,
        "queue_size": 42,
    }
    row.update(overrides)
    conn = connect(db)
    with transaction(conn):
        conn.execute("DELETE FROM system_status")
        conn.execute(
            f"INSERT INTO system_status ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values())
        )
    conn.close()


def add_device(db: Path, hostname: str, ip: str, **commands: bool) -> int:
    conn = connect(db)
    device_id = save_device(conn, DeviceForm(hostname, ip, {**ALL, **commands}, {"counters": "1/3", "ptp": "1/1"}))
    conn.close()
    return device_id


def write_device_status(db: Path, device_id: int, now, status: str, **columns) -> None:  # noqa: ANN001
    row = {"device_id": device_id, "status": status, "updated_at": iso(now), **columns}
    conn = connect(db)
    with transaction(conn):
        conn.execute(
            f"INSERT INTO device_status ({', '.join(row)}) VALUES ({', '.join('?' for _ in row)})", tuple(row.values())
        )
    conn.close()


def section(body: str, title: str) -> str:
    match = re.search(rf'<h2 class="panel-title">{title}.*?</section>', body, re.S)
    assert match, f"「{title}」の欄がありません"
    return match.group(0)


# ---------------------------------------------------------------- UI-04 全体の状態
def test_ui_04_overall_status(env):
    """全体の状態(稼働状態、接続状態、機器数、件数、キュー)を表示する(UI-04、7.4.2.2)。"""
    server, db, now, client = env
    write_system_status(db, now.value)
    body = client.get("/dashboard").body
    overall = section(body, "全体の状態")
    assert re.search(r'badge-ok">稼働中', overall)
    assert re.search(r'badge-ok">接続中', overall)
    assert "1,234 パケット" in overall
    assert "成功 5,678" in overall
    assert "失敗 0" in overall
    # 送信件数に、LLD用データと自己監視値を含むことを書き添える(D-016)
    assert "LLD用データと自己監視値の送信も含みます" in overall
    assert "7 パケット" in overall  # 対象外の件数
    assert "42 / 100,000 件" in overall  # 送信待ちキュー
    for label in ("未登録の送信元", "壊れたデータ", "4つ以外のコマンド", "送信待ちキューが満杯", "Zabbixに接続できない"):
        assert label in overall


def test_ui_04_collector_stopped(env):
    """collectorの最後の書き出しが30秒より古ければ「停止」と表示する(8.4.4、T2-24)。"""
    server, db, now, client = env
    write_system_status(db, now.value - timedelta(seconds=31))
    overall = section(client.get("/dashboard").body, "全体の状態")
    assert re.search(r'badge-error">停止', overall)
    assert "不明(collectorが停止しています)" in overall


def test_ui_04_no_status_yet(env):
    """collectorがまだ一度も書き出していない場合も、「停止」と表示する。"""
    server, db, now, client = env
    overall = section(client.get("/dashboard").body, "全体の状態")
    assert re.search(r'badge-error">停止', overall)


def test_ui_04_zabbix_unreachable(env):
    server, db, now, client = env
    write_system_status(db, now.value, zabbix_connected=0)
    assert re.search(r'badge-error">接続できない', client.get("/dashboard").body)


def test_ui_04_drop_colors(env):
    """破棄件数の色:未登録・壊れた・4つ以外は黄、キュー満杯・Zabbixに接続できないは赤(D-022)。"""
    server, db, now, client = env
    drops = {"unknown_source": 1, "invalid_data": 2, "unknown_command": 3, "queue_full": 4, "zabbix_unreachable": 5}
    write_system_status(db, now.value, dropped_per_min=json.dumps(drops))
    overall = section(client.get("/dashboard").body, "全体の状態")
    assert re.search(r'未登録の送信元</td><td class="num"><span class="text-warn">1<', overall)
    assert re.search(r'壊れたデータ</td><td class="num"><span class="text-warn">2<', overall)
    assert re.search(r'4つ以外のコマンド</td><td class="num"><span class="text-warn">3<', overall)
    assert re.search(r'送信待ちキューが満杯</td><td class="num"><span class="text-error">4<', overall)
    assert re.search(r'Zabbixに接続できない</td><td class="num"><span class="text-error">5<', overall)


def test_ui_04_config_error_banner(env):
    """設定の読み込みエラーは、ダッシュボードの上部に黄色の帯で表示する。閲覧者にも表示する(D-023、CF-05)。"""
    server, db, now, client = env
    write_system_status(db, now.value, config_error="設定の値が正しくありません")
    assert re.search(r'banner-warn".*?設定の値が正しくありません', client.get("/dashboard").body, re.S)
    viewer = Browser(server.base_url)
    viewer.login("user", "")
    assert "設定の値が正しくありません" in viewer.get("/dashboard").body


def test_ui_04_queue_max_from_settings(env):
    server, db, now, client = env
    conn = connect(db)
    update_settings(conn, {"queue_max_size": "2000"})
    conn.close()
    write_system_status(db, now.value)
    assert "42 / 2,000 件" in client.get("/dashboard").body


# ---------------------------------------------------------------- UI-05 機器ごとの状態
def test_ui_05_device_rows(env):
    """機器ごとの受信状態、最終受信時刻、コマンド別の受信状況、不整合を表示する(UI-05、7.4.2.3)。"""
    server, db, now, client = env
    write_system_status(db, now.value)
    t = now.value
    ok_id = add_device(db, "NEXUS-TEST-01", "192.0.2.1", cmd_ptp_parent=False)
    stale_id = add_device(db, "NEXUS-TEST-02", "192.0.2.2")
    add_device(db, "NEXUS-TEST-03", "192.0.2.3")
    write_device_status(
        db, ok_id, t, "ok",
        last_received_at=iso(t - timedelta(seconds=5)),
        last_counters_brief_at=iso(t - timedelta(seconds=5)),
        last_ptp_brief_at=iso(t - timedelta(seconds=301)),  # 途絶判定時間(300秒)を過ぎている
        last_ptp_clock_at=None,
        mismatch_interfaces=json.dumps([{"command": "show interface counters brief", "ifnum": "1/99"}]),
    )  # fmt: skip
    write_device_status(db, stale_id, t, "stale", last_received_at=iso(t - timedelta(minutes=10)))
    body = client.get("/dashboard").body
    devices = section(body, "機器ごとの状態")

    rows = re.findall(r"<tr>\s*<td>(NEXUS-TEST-0\d)</td>(.*?)</tr>", devices, re.S)
    by_host = dict(rows)
    assert list(by_host) == ["NEXUS-TEST-01", "NEXUS-TEST-02", "NEXUS-TEST-03"]

    first = by_host["NEXUS-TEST-01"]
    assert "192.0.2.1" in first
    assert re.search(r'badge-ok">正常', first)
    assert "2027-01-15 08:59:55" in first  # 日本時間で表示する(8.1.4)
    assert "5秒前" in first
    cells = re.findall(r'<td class="text-(\w+)">([^<]*)', first)
    # show interface counters brief:正常(緑)、show ptp brief:途絶判定時間を過ぎた(赤、D-025)、
    # show ptp clock:未受信(黄)、show ptp parent:チェックを外した(対象外)
    assert cells == [("ok", "2027-01-15 08:59:55"), ("error", "2027-01-15 08:54:59"), ("warn", "未受信"), ("muted", "対象外")]
    assert "show interface counters brief: 1/99" in first

    assert re.search(r'badge-error">途絶', by_host["NEXUS-TEST-02"])
    assert "10分0秒前" in by_host["NEXUS-TEST-02"]
    # collectorがまだ状態を書き出していない機器は「不明」
    assert re.search(r'badge-muted">不明', by_host["NEXUS-TEST-03"])

    overall = section(body, "全体の状態")
    assert "3 台" in overall
    assert "正常 1" in overall and "途絶 1" in overall and "受信待ち 0" in overall and "不明 1" in overall


def test_ui_05_waiting(env):
    """機器を登録した直後は「受信待ち」を黄で表示する(T2-21a、D-022)。"""
    server, db, now, client = env
    write_system_status(db, now.value)
    device_id = add_device(db, "NEXUS-TEST-01", "192.0.2.1")
    write_device_status(db, device_id, now.value, "waiting")
    assert re.search(r'badge-warn">受信待ち', client.get("/dashboard").body)


def test_ui_05_no_devices(env):
    server, db, now, client = env
    assert "登録されている機器はありません" in client.get("/dashboard").body


# ---------------------------------------------------------------- UI-06 未登録の送信元
def test_ui_06_unknown_sources(env):
    """未登録の送信元の一覧(送信元IPアドレス、データ内のホスト名、件数、最終受信時刻)を表示する(UI-06、T2-15)。"""
    server, db, now, client = env
    conn = connect(db)
    with transaction(conn):
        conn.execute(
            "INSERT INTO unknown_sources VALUES (?, ?, ?, ?)",
            ("192.0.2.99", "NEXUS-TEST-99", 1500, iso(now.value - timedelta(seconds=3))),
        )
        conn.execute("INSERT INTO unknown_sources VALUES (?, ?, ?, ?)", ("192.0.2.98", None, 2, iso(now.value)))
    conn.close()
    unknown = section(client.get("/dashboard").body, "未登録の送信元からの受信")
    assert 'badge-warn">2 件' in unknown
    assert "192.0.2.99" in unknown and "NEXUS-TEST-99" in unknown and "1,500" in unknown
    assert "3秒前" in unknown
    assert "(読み取れません)" in unknown


# ---------------------------------------------------------------- 11.3.3 無害化
XSS_STRINGS = [
    "<script>alert('xss')</script>",
    '"><img src=x onerror=alert(1)>',
    "<svg onload=alert(1)>",
    "javascript:alert(1)",
    "&lt;already-escaped&gt;",
]


@pytest.mark.parametrize("node_id", XSS_STRINGS)
def test_unknown_source_hostname_is_escaped(env, node_id):
    """未登録の送信元の「データ内のホスト名」は、外部から届いた任意の文字列のため、必ず無害化して表示する(11.3.3)。"""
    server, db, now, client = env
    conn = connect(db)
    with transaction(conn):
        conn.execute("INSERT INTO unknown_sources VALUES (?, ?, ?, ?)", ("192.0.2.99", node_id, 1, iso(now.value)))
    conn.close()
    for path in ("/dashboard", "/dashboard/content"):
        body = client.get(path).body
        assert node_id not in body or node_id == "javascript:alert(1)"
        assert str(escape(node_id)) in body
        assert "<script>alert" not in body
        assert "onerror=alert(1)>" not in body


@pytest.mark.parametrize("message", XSS_STRINGS)
def test_config_error_is_escaped(env, message):
    server, db, now, client = env
    write_system_status(db, now.value, config_error=message)
    body = client.get("/dashboard").body
    assert str(escape(message)) in body
    assert "<script>alert" not in body and "<svg onload" not in body


def test_mismatch_text_is_escaped(env):
    server, db, now, client = env
    write_system_status(db, now.value)
    device_id = add_device(db, "NEXUS-TEST-01", "192.0.2.1")
    write_device_status(
        db, device_id, now.value, "ok",
        mismatch_interfaces=json.dumps([{"command": "<script>alert(1)</script>", "ifnum": "<b>1/1</b>"}]),
    )  # fmt: skip
    body = client.get("/dashboard").body
    assert "<script>alert(1)</script>" not in body
    assert "&lt;b&gt;1/1&lt;/b&gt;" in body


def test_broken_status_data_does_not_break_dashboard(env):
    """状態データの形が壊れていても、ダッシュボードは表示できる。"""
    server, db, now, client = env
    write_system_status(db, now.value, dropped_per_min="not json", received_per_min=None)
    device_id = add_device(db, "NEXUS-TEST-01", "192.0.2.1")
    write_device_status(db, device_id, now.value, "ok", mismatch_interfaces="{broken", last_received_at="昨日")
    page = client.get("/dashboard")
    assert page.status == 200
    assert "NEXUS-TEST-01" in page.body


# ---------------------------------------------------------------- 表示の部品
@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0秒前"), (59, "59秒前"), (60, "1分0秒前"), (192, "3分12秒前"), (7500, "2時間5分前"), (3 * 86400, "3日前"), (-5, "0秒前")],
)
def test_elapsed_text(seconds, text):
    assert elapsed_text(seconds) == text
