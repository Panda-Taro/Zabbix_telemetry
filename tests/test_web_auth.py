"""WebGUI のログイン、権限、パスワード変更、セキュリティ対策の自動テスト(段階5)。

AU-01〜AU-10、UI-01〜UI-03、UI-19、UI-20、LG-05、11.3.3 を確かめる。
"""

import ast
import json
import re
from pathlib import Path

import pytest

from common.constants import SESSION_COOKIE_NAME
from common.database import STATUS_TABLES, connect
from common.passwords import verify_password
from tests.web_support import Browser, FakeNow, WebServer

REPO = Path(__file__).resolve().parent.parent
ADMIN_PAGES = ["/devices", "/settings", "/logs", "/logs/audit", "/system", "/users"]
USER_PAGES = ["/dashboard", "/dashboard/content", "/password"]


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "telemetry.db"
    now = FakeNow()
    server = WebServer(db, now)
    server.start()
    try:
        yield server, db, now
    finally:
        server.stop()


def browser(server: WebServer) -> Browser:
    return Browser(server.base_url)


def audit_rows(db: Path) -> list[dict]:
    conn = connect(db)
    try:
        return [dict(row) for row in conn.execute("SELECT * FROM audit_log ORDER BY id")]
    finally:
        conn.close()


# ---------------------------------------------------------------- AU-01
@pytest.mark.parametrize("path", USER_PAGES[:1] + USER_PAGES[2:] + ADMIN_PAGES + ["/"])
def test_au_01_login_required(env, path):
    """ログインしていない場合は、どの画面を開いてもログイン画面へ移る(AU-01、T2-29)。"""
    server, _, _ = env
    page = browser(server).get(path)
    assert page.status == 303
    assert page.location.endswith("/login") or page.location.endswith("/dashboard")


def test_au_01_auto_refresh_without_login(env):
    """自動更新の途中でログインが切れていた場合は、ログイン画面へ移るよう指示する。"""
    server, _, _ = env
    page = browser(server).get("/dashboard/content", headers={"HX-Request": "true"})
    assert page.headers.get("hx-redirect") == "/login"
    page = browser(server).get("/dashboard/content")
    assert page.status == 303


def test_au_01_post_without_login(env):
    server, _, _ = env
    page = browser(server).post("/password", {"current_password": "x", "new_password": "y", "new_password_confirm": "y"})
    assert page.status == 303
    assert page.location.endswith("/login")


# ---------------------------------------------------------------- AU-02、AU-07
@pytest.mark.parametrize(("username", "password"), [("admin", "admin"), ("user", "")])
def test_au_02_initial_accounts_can_login(env, username, password):
    """初期アカウント(admin/admin、user/空欄)でログインできる(AU-02、AU-07、T2-02)。"""
    server, _, _ = env
    client = browser(server)
    page = client.login(username, password)
    assert page.status == 303
    assert page.location.endswith("/dashboard")
    page = client.get("/dashboard")
    assert page.status == 200
    assert username in page.body


@pytest.mark.parametrize(("username", "password"), [("admin", "wrong"), ("admin", ""), ("nobody", "admin"), ("", "")])
def test_au_02_wrong_credentials(env, username, password):
    server, _, _ = env
    client = browser(server)
    page = client.login(username, password)
    assert page.status == 401
    assert "ユーザー名またはパスワードが違います" in page.body
    assert client.get("/dashboard").status == 303


# ---------------------------------------------------------------- AU-03、AU-04
def test_au_03_viewer_menu_shows_only_dashboard(env):
    """閲覧者の左ペインには、ダッシュボードだけを表示する(AU-03、T2-30)。"""
    server, _, _ = env
    viewer = browser(server)
    viewer.login("user", "")
    body = viewer.get("/dashboard").body
    nav = re.search(r'<nav class="sidebar".*?</nav>', body, re.S).group(0)
    assert 'href="/dashboard"' in nav
    for path in ADMIN_PAGES:
        assert f'href="{path}"' not in nav


def test_au_03_admin_menu(env):
    server, _, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    nav = re.search(r'<nav class="sidebar".*?</nav>', admin.get("/dashboard").body, re.S).group(0)
    for path in ["/dashboard", *ADMIN_PAGES]:
        assert f'href="{path}"' in nav
    for label in ("機器一覧", "全体設定", "動作ログ", "変更履歴", "システム設定", "ユーザー管理"):
        assert label in nav


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_au_04_viewer_cannot_open_admin_pages(env, path):
    """閲覧者が管理者用の画面のURLを直接開くと、拒否する(AU-04、T2-30)。"""
    server, _, _ = env
    viewer = browser(server)
    viewer.login("user", "")
    page = viewer.get(path)
    assert page.status == 403
    assert "管理者だけが使えます" in page.body


@pytest.mark.parametrize("path", ADMIN_PAGES)
def test_au_04_admin_can_open_admin_pages(env, path):
    server, _, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    assert admin.get(path).status == 200


def test_au_03_viewer_can_change_own_password(env):
    server, _, _ = env
    viewer = browser(server)
    viewer.login("user", "")
    assert viewer.get("/password").status == 200


# ---------------------------------------------------------------- AU-05
def test_au_05_lock_after_5_failures(env):
    """5回続けて失敗すると、15分間ログインできない(AU-05、T2-31)。"""
    server, db, now = env
    client = browser(server)
    for _ in range(5):
        assert client.login("admin", "wrong").status == 401
    # 正しいパスワードでも、ログインできない
    page = client.login("admin", "admin")
    assert page.status == 401
    assert "しばらくログインできません" in page.body
    now.advance(minutes=14, seconds=59)
    assert client.login("admin", "admin").status == 401
    now.advance(seconds=1)
    assert client.login("admin", "admin").status == 303


def test_au_05_success_resets_count(env):
    """途中でログインに成功したら、失敗の回数は0に戻る。"""
    server, db, _ = env
    client = browser(server)
    for _ in range(4):
        client.login("admin", "wrong")
    assert client.login("admin", "admin").status == 303
    other = browser(server)
    for _ in range(4):
        other.login("admin", "wrong")
    assert other.login("admin", "admin").status == 303


def test_au_05_lock_is_per_user(env):
    server, _, _ = env
    client = browser(server)
    for _ in range(5):
        client.login("admin", "wrong")
    assert client.login("user", "").status == 303


# ---------------------------------------------------------------- AU-06
def test_au_06_no_automatic_logout(env):
    """長い時間がたっても、自動ではログアウトしない(AU-06、T2-32)。"""
    server, _, now = env
    client = browser(server)
    client.login("user", "")
    now.advance(days=30)
    assert client.get("/dashboard").status == 200


def test_au_06_cookie_survives_browser_restart(env):
    """ブラウザを閉じてもログインしたままにするため、Cookieに長い有効期限を付ける(D-024)。"""
    server, _, _ = env
    client = browser(server)
    page = client.login("admin", "admin")
    session_cookie = next(c for c in page.set_cookies if c.startswith(SESSION_COOKIE_NAME + "="))
    max_age = int(re.search(r"Max-Age=(\d+)", session_cookie, re.I).group(1))
    assert max_age >= 365 * 24 * 60 * 60
    # 画面を開くたびに、有効期限を延ばす
    page = client.get("/dashboard")
    assert any(c.startswith(SESSION_COOKIE_NAME + "=") and "Max-Age" in c for c in page.set_cookies)


# ---------------------------------------------------------------- AU-08
def test_au_08_initial_password_notice(env):
    """初期パスワードのままの管理者には、パスワード変更を促す表示を出す(AU-08、T2-02)。変更は強制しない。"""
    server, _, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    page = admin.get("/dashboard")
    assert page.status == 200
    assert "初期パスワードのまま" in page.body
    viewer = browser(server)
    viewer.login("user", "")
    assert "初期パスワードのまま" not in viewer.get("/dashboard").body

    page = admin.get("/password")
    admin.post("/password", {"csrf": page.csrf(), "current_password": "admin", "new_password": "secret-1", "new_password_confirm": "secret-1"})
    assert "初期パスワードのまま" not in admin.get("/dashboard").body


# ---------------------------------------------------------------- UI-19、UI-20、AU-10
def change_password(client: Browser, current: str, new: str, confirm: str | None = None):  # noqa: ANN201
    page = client.get("/password")
    return client.post(
        "/password",
        {"csrf": page.csrf(), "current_password": current, "new_password": new, "new_password_confirm": new if confirm is None else confirm},
    )


def test_ui_19_current_password_required(env):
    """現在のパスワードが違うと、変更できない(UI-19、T2-34)。"""
    server, db, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    page = change_password(admin, "wrong", "secret-1")
    assert page.status == 400
    assert "現在のパスワードが違います" in page.body
    page = change_password(admin, "", "secret-1")
    assert page.status == 400
    assert browser(server).login("admin", "admin").status == 303


def test_ui_19_change_password(env):
    server, db, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    page = change_password(admin, "admin", "secret-1")
    assert page.status == 200
    assert "パスワードを変更しました" in page.body
    assert browser(server).login("admin", "admin").status == 401
    assert browser(server).login("admin", "secret-1").status == 303


def test_ui_19_confirmation_must_match(env):
    server, _, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    page = change_password(admin, "admin", "secret-1", "secret-2")
    assert page.status == 400
    assert "一致しません" in page.body


def test_ui_20_admin_password_cannot_be_empty(env):
    """管理者のパスワードは空欄にできない(UI-20、T2-34)。"""
    server, _, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    page = change_password(admin, "admin", "")
    assert page.status == 400
    assert "空欄にできません" in page.body


def test_ui_20_viewer_password_can_be_empty(env):
    """閲覧者のパスワードは空欄を許す(UI-20)。"""
    server, _, _ = env
    viewer = browser(server)
    viewer.login("user", "")
    assert change_password(viewer, "", "view-1").status == 200
    assert browser(server).login("user", "view-1").status == 303
    assert change_password(viewer, "view-1", "").status == 200
    assert browser(server).login("user", "").status == 303


def test_au_10_other_sessions_invalidated(env):
    """パスワードを変更すると、そのユーザーの他のログインが無効になる。自分のログインは続く(AU-10、T2-34)。"""
    server, _, _ = env
    first, second, other_user = browser(server), browser(server), browser(server)
    first.login("admin", "admin")
    second.login("admin", "admin")
    other_user.login("user", "")
    change_password(first, "admin", "secret-1")
    assert first.get("/dashboard").status == 200
    assert second.get("/dashboard").status == 303
    assert other_user.get("/dashboard").status == 200


# ---------------------------------------------------------------- ログアウト
def test_logout(env):
    server, _, _ = env
    client = browser(server)
    client.login("admin", "admin")
    page = client.logout()
    assert page.status == 303
    assert page.location.endswith("/login")
    assert client.get("/dashboard").status == 303


def test_logout_removes_only_this_session(env):
    server, _, _ = env
    first, second = browser(server), browser(server)
    first.login("admin", "admin")
    second.login("admin", "admin")
    first.logout()
    assert second.get("/dashboard").status == 200


# ---------------------------------------------------------------- LG-05
def test_lg_05_login_logout_failure_are_recorded(env):
    """ログイン、ログアウト、ログイン失敗を変更履歴に記録する。パスワードは記録しない(LG-05、T2-37)。"""
    server, db, _ = env
    client = browser(server)
    client.login("admin", "wrong-password-text")
    client.login("nobody", "x")
    client.login("admin", "admin")
    change_password(client, "admin", "secret-text-1")
    client.logout()
    rows = audit_rows(db)
    assert [(r["category"], r["action"], r["target"]) for r in rows] == [
        ("auth", "login_failed", "admin"),
        ("auth", "login_failed", "nobody"),
        ("auth", "login", "admin"),
        ("user", "update", "admin"),
        ("auth", "logout", "admin"),
    ]
    assert rows[3]["after_value"] == json.dumps({"password_changed": True})
    text = json.dumps(rows, ensure_ascii=False)
    for secret in ("wrong-password-text", "secret-text-1", "scrypt$"):
        assert secret not in text
    for row in rows:
        assert row["timestamp"].endswith("+00:00")


# ---------------------------------------------------------------- UI-01〜UI-03
def test_ui_01_02_layout(env):
    """ヘッダーにシステム名・ユーザー名・ログアウト、左ペインにメニューがあり、日本語で表示する(UI-01、UI-02、T2-35)。"""
    server, _, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    body = admin.get("/dashboard").body
    header = re.search(r'<header class="header">.*?</header>', body, re.S).group(0)
    assert "Telemetry 中間サーバー" in header
    assert "admin" in header
    assert 'href="/password"' in header  # ユーザー名を押すとパスワード変更画面(7.1.3)
    assert "ログアウト" in header
    assert '<nav class="sidebar"' in body
    assert '<html lang="ja">' in body


def test_ui_03_auto_refresh_every_10_seconds(env):
    """ダッシュボードは10秒ごとに自動で更新する(UI-03、T2-10)。"""
    server, _, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    body = admin.get("/dashboard").body
    assert 'hx-get="/dashboard/content"' in body
    assert 'hx-trigger="every 10s"' in body
    content = admin.get("/dashboard/content")
    assert content.status == 200
    assert "全体の状態" in content.body
    assert "<html" not in content.body


# ---------------------------------------------------------------- 11.3.3 セキュリティ
def test_cookie_is_httponly_and_samesite(env):
    """ログイン状態を示すCookieは、画面上のプログラムから読み取れない(11.3.3)。"""
    server, _, _ = env
    page = browser(server).login("admin", "admin")
    session_cookie = next(c for c in page.set_cookies if c.startswith(SESSION_COOKIE_NAME + "="))
    assert "httponly" in session_cookie.lower()
    assert "samesite=lax" in session_cookie.lower()


def test_session_token_not_stored_as_is(env):
    """データベースには、合言葉(セッショントークン)のハッシュ値だけを保存する(8.3.5)。"""
    server, db, _ = env
    client = browser(server)
    client.login("admin", "admin")
    token = client.cookie(SESSION_COOKIE_NAME)
    conn = connect(db)
    stored = [row[0] for row in conn.execute("SELECT token_hash FROM sessions")]
    conn.close()
    assert token not in stored
    assert len(stored) == 1


@pytest.mark.parametrize("csrf", [None, "", "wrong-token"])
def test_csrf_required_for_password_change(env, csrf):
    """設定を変更する操作には、不正な操作を防ぐための印が必要(CSRF対策、11.3.3)。"""
    server, db, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    data = {"current_password": "admin", "new_password": "secret-1", "new_password_confirm": "secret-1"}
    if csrf is not None:
        data["csrf"] = csrf
    page = admin.post("/password", data)
    assert page.status == 403
    assert browser(server).login("admin", "admin").status == 303  # 変わっていない


@pytest.mark.parametrize("csrf", [None, "wrong-token"])
def test_csrf_required_for_logout(env, csrf):
    server, _, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    page = admin.post("/logout", {} if csrf is None else {"csrf": csrf})
    assert page.status == 403
    assert admin.get("/dashboard").status == 200


def test_csrf_token_is_per_session(env):
    """他のユーザーの画面の印は使えない。"""
    server, _, _ = env
    viewer = browser(server)
    viewer.login("user", "")
    viewer_csrf = viewer.get("/dashboard").csrf()
    admin = browser(server)
    admin.login("admin", "admin")
    assert admin.post("/logout", {"csrf": viewer_csrf}).status == 403


def test_login_requires_login_screen_token(env):
    """ログインも、ログイン画面から送られたものだけを受け付ける。"""
    server, _, _ = env
    client = browser(server)
    page = client.post("/login", {"username": "admin", "password": "admin", "csrf": "forged"})
    assert page.status == 400
    assert client.get("/dashboard").status == 303


@pytest.mark.parametrize("username", ["admin' OR '1'='1", "admin'--", '" OR ""="', "admin; DROP TABLE users;"])
def test_sql_injection_in_login(env, username):
    """入力された文字列は、データベースへの問い合わせにそのまま埋め込まない(SQLインジェクション対策、11.3.3)。"""
    server, db, _ = env
    assert browser(server).login(username, "x").status == 401
    conn = connect(db)
    assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 2
    conn.close()


def test_security_headers(env):
    server, _, _ = env
    page = browser(server).get("/login")
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"


def test_no_external_resources(env):
    """画面の部品は同梱したものだけを使い、外部から取得しない(11.5.3)。"""
    server, _, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    for path in ("/login", "/dashboard", "/password"):
        body = (admin if path != "/login" else browser(server)).get(path).body
        for url in re.findall(r'(?:src|href)="([^"]+)"', body):
            assert url.startswith("/"), f"{path} が外部の部品 {url} を使っています"
    js = browser(server).get("/static/vendor/htmx.min.js")
    assert js.status == 200
    assert "htmx" in js.body[:200]


def test_password_hash_in_db_after_change(env):
    server, db, _ = env
    admin = browser(server)
    admin.login("admin", "admin")
    change_password(admin, "admin", "secret-1")
    conn = connect(db)
    stored = conn.execute("SELECT password_hash, is_initial_password FROM users WHERE username = 'admin'").fetchone()
    conn.close()
    assert stored["password_hash"] != "secret-1"
    assert verify_password("secret-1", stored["password_hash"])
    assert stored["is_initial_password"] == 0


# ---------------------------------------------------------------- CF-06
def test_cf_06_web_does_not_write_status_tables():
    """web は、状態データのテーブルに書き込まない(CF-06)。"""
    pattern = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(\w+)", re.IGNORECASE)
    for path in (REPO / "web").rglob("*.py"):
        for _, table in pattern.findall(path.read_text(encoding="utf-8")):
            assert table not in STATUS_TABLES, f"{path.name} が状態データのテーブル {table} に書き込んでいます"


def test_cf_06_collector_does_not_use_web():
    for path in (REPO / "collector").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not any(m == "web" or m.startswith("web.") for m in modules), f"{path} が web の部品を使っています"
