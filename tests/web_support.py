"""WebGUI の自動テストで使う部品。

WebGUI を実際に起動し(uvicorn)、Python 標準の機能(urllib)でブラウザの代わりに操作する。
"""

import http.cookiejar
import logging
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import uvicorn

from web.app import create_app


class FakeNow:
    """進め方を自由に決められる時計(UTC)。"""

    def __init__(self) -> None:
        self.value = datetime(2027, 1, 15, 0, 0, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class WebServer:
    """テスト用に WebGUI を起動する。"""

    def __init__(self, db_path: Path, now: FakeNow, log_dir: Path | None = None) -> None:
        self.port = free_tcp_port()
        self.app = create_app(db_path, logging.getLogger("web-test"), now=now, log_dir=log_dir)
        config = uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_config=None, access_log=False)
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        self.thread.start()
        deadline = time.monotonic() + 10
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("WebGUIが起動しませんでした")
            time.sleep(0.02)

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003, ANN201
        return None


@dataclass
class Page:
    status: int
    headers: dict[str, str]
    body: str
    set_cookies: list[str]

    @property
    def location(self) -> str:
        return self.headers.get("location", "")

    def csrf(self) -> str:
        match = re.search(r'name="csrf" value="([^"]*)"', self.body)
        assert match, "画面に不正な操作を防ぐための印がありません"
        return match.group(1)


class Browser:
    """ブラウザの代わり。Cookie を覚え、転送(リダイレクト)は自動では追わない。"""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url
        self.cookies = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies), _NoRedirect())

    def _open(self, request: urllib.request.Request) -> Page:
        try:
            response = self.opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        body = response.read().decode("utf-8", errors="replace")
        headers = {k.lower(): v for k, v in response.headers.items()}
        set_cookies = response.headers.get_all("set-cookie") or []
        return Page(status=response.status, headers=headers, body=body, set_cookies=set_cookies)

    def get(self, path: str, headers: dict[str, str] | None = None) -> Page:
        return self._open(urllib.request.Request(self.base_url + path, headers=headers or {}))

    def post(self, path: str, data: dict[str, str]) -> Page:
        encoded = urllib.parse.urlencode(data).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=encoded,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        return self._open(request)

    def post_file(self, path: str, data: dict[str, str], field: str, filename: str, content: bytes) -> Page:
        """ファイルを添えて送る(multipart/form-data)。"""
        boundary = "----telemetry-test-boundary"
        parts: list[bytes] = []
        for name, value in data.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
            )
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            "Content-Type: application/json\r\n\r\n".encode("utf-8")
            + content
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        request = urllib.request.Request(
            self.base_url + path,
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        return self._open(request)

    def raw(self, path: str) -> tuple[int, dict[str, str], bytes]:
        """応答をバイト列のまま受け取る(ダウンロードの確認用)。"""
        try:
            response = self.opener.open(urllib.request.Request(self.base_url + path), timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()

    def cookie(self, name: str) -> str | None:
        for cookie in self.cookies:
            if cookie.name == name:
                return cookie.value
        return None

    def login(self, username: str, password: str) -> Page:
        page = self.get("/login")
        return self.post("/login", {"username": username, "password": password, "csrf": page.csrf()})

    def logout(self) -> Page:
        page = self.get("/dashboard")
        return self.post("/logout", {"csrf": page.csrf()})
