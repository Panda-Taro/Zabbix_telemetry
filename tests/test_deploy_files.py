"""導入の仕組みのファイル(deploy.sh、docker-compose.yml、Dockerfile、復旧用コマンド)の自動テスト。

これらは Ubuntu Server の上でしか動かせないため、この開発用PCでは、中身の決まりと、
プログラムの定数との食い違いがないことを確かめる(実際の動作は、中間サーバーで確かめる)。
"""

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from common import constants

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deploy.sh"
RECOVER = ROOT / "tools" / "telemetry-recover.sh"
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILES = (ROOT / "collector" / "Dockerfile", ROOT / "web" / "Dockerfile")
HOSTCTL = ROOT / "tools" / "telemetry_hostctl.py"
UBUNTU_FILES = (DEPLOY, RECOVER, COMPOSE, HOSTCTL, *DOCKERFILES)


def text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def services(compose: str) -> dict[str, str]:
    """docker-compose.yml を、サービスごとの文字列に分ける。"""
    body = compose.split("\nservices:\n", 1)[1]
    parts = re.split(r"\n  (?=[a-z]+:\n)", "\n" + body)
    return {part.split(":", 1)[0].strip(): part for part in parts if part.strip()}


def test_ubuntu_files_use_lf_and_are_listed_in_gitattributes():
    for path in UBUNTU_FILES:
        assert b"\r\n" not in path.read_bytes(), f"{path.name} の改行が CRLF です"
    attributes = text(ROOT / ".gitattributes")
    assert "*.sh text eol=lf" in attributes and "Dockerfile text eol=lf" in attributes


def test_deploy_runs_the_11_steps_in_order():
    """4.5.3 の表の順番どおりに行う(DL-09)。"""
    steps = re.findall(r'^step (\d+) "([^"]+)"', text(DEPLOY), re.MULTILINE)
    assert [int(number) for number, _ in steps] == list(range(1, 12))
    keywords = [
        ("実行環境の確認", "Ubuntu Server 24.04", "管理者権限", "インターネット"),
        ("Dockerの導入", "未導入の場合のみ"),
        ("データ用フォルダ",),
        ("時刻同期", "UDPの受信バッファ"),
        ("ファイアウォール", "WebGUI用", "Telemetry受信用"),
        ("コンテナの作成と起動",),
        ("OS起動時の自動起動",),
        ("本体のIPアドレスを変更するために必要な、本体側の設定",),
        ("復旧用コマンドの配置",),
        ("本体のIPアドレスの設定",),
        ("結果の表示",),
    ]
    for (number, title), words in zip(steps, keywords, strict=True):
        for word in words:
            assert word in title, f"手順{number} の見出しに「{word}」がありません:{title}"


def test_deploy_safety_rules():
    script = text(DEPLOY)
    assert script.startswith("#!/usr/bin/env bash\n")
    assert "set -Eeuo pipefail" in script
    assert "trap 'on_error" in script  # 失敗した手順と原因を表示して止まる(OP-12)
    # /data の中身を消さない(OP-10)
    assert not re.search(r"rm\s+-[a-zA-Z]*r[a-zA-Z]*\s+.*(DATA_DIR|/data)", script)
    assert "docker compose down" not in script and "docker volume rm" not in script
    # 本体のIPアドレスの反映は最後(OP-11):netplan apply は手順11の表示のあと
    assert script.rindex("netplan apply") > script.index('step 11 "')
    assert script.count("yes_no ") >= 2  # 変更するか、反映するかの確認
    # プロキシの対応は入れない(決定事項 D-030)
    assert "proxy" not in script.lower()


def test_deploy_constants_match_the_program():
    script = text(DEPLOY)

    def value(name: str) -> str:
        match = re.search(rf'^readonly {name}="([^"]*)"', script, re.MULTILINE)
        assert match, f"deploy.sh に {name} がありません"
        return match.group(1)

    assert int(value("RMEM_MAX")) == constants.UDP_RECEIVE_BUFFER_BYTES  # 決定事項 D-019
    assert int(value("CONTAINER_UID")) == constants.CONTAINER_UID
    assert int(value("FALLBACK_PORT")) == constants.WEBGUI_FALLBACK_PORT
    assert value("HOSTCTL_RUN_DIR") == constants.HOSTCTL_RUN_DIR
    assert value("DATA_DIR") + "/telemetry.db" == constants.DEFAULT_DB_PATH
    for subdir in (constants.HOSTCTL_SPOOL, constants.HOSTCTL_INBOX, constants.HOSTCTL_RESULTS, constants.HOSTCTL_STATE):
        assert f"${{HOSTCTL_RUN_DIR}}/{subdir} " in script
    # web が書けるのは spool と inbox だけ(結果と状態は補助プログラムだけが書く)
    assert re.search(r"/spool 0700 \$\{CONTAINER_UID\}", script) and re.search(r"/inbox 0700 \$\{CONTAINER_UID\}", script)
    assert re.search(r"/results 0755 root root", script) and re.search(r"/state 0755 root root", script)


def test_deploy_installs_every_module_the_helper_needs():
    """補助プログラムが読み込む common の部品を、deploy.sh がすべて本体に置くこと。"""
    script = text(DEPLOY)
    needed = {"common/__init__.py"}
    for path in (HOSTCTL, ROOT / "common" / "netconfig.py", ROOT / "common" / "constants.py"):
        for node in ast.walk(ast.parse(text(path))):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("common"):
                needed.add(node.module.replace(".", "/") + ".py")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = node.module if isinstance(node, ast.ImportFrom) else node.names[0].name
                top = (module or "").split(".")[0]
                assert top in {"common", "__future__"} or top in _stdlib(), f"{path.name} が標準以外の {module} を読み込んでいます"
    for module in needed:
        assert f"${{SCRIPT_DIR}}/{module}" in script, f"deploy.sh が {module} を本体に置いていません"


def _stdlib() -> set[str]:
    import sys

    return set(sys.stdlib_module_names) | {"yaml"}  # yaml は Ubuntu の python3-yaml(deploy.sh が確かめる)


def test_compose_services():
    compose = text(COMPOSE)
    blocks = services(compose)
    assert set(blocks) == {"collector", "web"}  # OP-01
    for name, block in blocks.items():
        assert "network_mode: host" in block  # 4.1.2
        assert "restart: always" in block  # OP-02、OP-03
        assert f'user: "{constants.CONTAINER_UID}:{constants.CONTAINER_UID}"' in block  # 決定事項 D-030
        assert "- /data:/data" in block  # OP-05
        assert "cap_drop:\n      - ALL" in block
        assert "no-new-privileges:true" in block
        assert f"dockerfile: {name}/Dockerfile" in block
    # 特別な権限は、どちらにも与えない(決定事項 D-029)
    for word in ("privileged", "cap_add", "pid: host", "/var/run/docker.sock", "/etc/netplan", "devices:"):
        assert word not in compose
    assert f"- {constants.HOSTCTL_RUN_DIR}:{constants.HOSTCTL_DIR_IN_CONTAINER}" in blocks["web"]
    assert constants.HOSTCTL_RUN_DIR not in blocks["collector"]


def test_dockerfiles():
    for path in DOCKERFILES:
        content = text(path)
        assert re.search(r"^FROM python:3\.12\.\d+-slim-bookworm$", content, re.MULTILINE), "Python の版を固定すること"
        assert f"USER {constants.CONTAINER_UID}:{constants.CONTAINER_UID}" in content
        assert "requirements.txt" in content
    web = text(ROOT / "web" / "Dockerfile")
    assert "tools/recovery.py" in web and 'CMD ["python", "-m", "web"]' in web
    assert 'CMD ["python", "-m", "collector"]' in text(ROOT / "collector" / "Dockerfile")


@pytest.mark.parametrize("requirements", ["collector/requirements.txt", "web/requirements.txt"])
def test_requirements_are_pinned(requirements):
    """ライブラリの版番号を固定する(11.4.5)。"""
    for line in text(ROOT / requirements).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            assert re.fullmatch(r"[A-Za-z0-9_.\-]+==[0-9][A-Za-z0-9.]*", line), line


def test_recover_script():
    script = text(RECOVER)
    assert "set -Eeuo pipefail" in script
    assert "reset-port" in script and "reset-password" in script
    assert f'CONTAINER_UID="{constants.CONTAINER_UID}"' in script
    assert f'FALLBACK_PORT="{constants.WEBGUI_FALLBACK_PORT}"' in script
    assert "--network none" in script and "python -m tools.recovery" in script
    # ダウンロードしたフォルダに頼らない(決定事項 D-030)
    assert "SCRIPT_DIR" not in script and "docker compose" not in script


def _bash() -> str | None:
    """構文の確認に使える bash(Windows では Git に付いている bash)。"""
    candidates = [shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                done = subprocess.run([candidate, "-c", "echo ok"], capture_output=True, text=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                continue
            if done.stdout.strip() == "ok":
                return candidate
    return None


@pytest.mark.parametrize("script", [DEPLOY, RECOVER], ids=lambda path: path.name)
def test_shell_syntax(script):
    bash = _bash()
    if bash is None:
        pytest.skip("bash が見つからないため、構文の確認ができません")
    done = subprocess.run([bash, "-n", str(script)], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
