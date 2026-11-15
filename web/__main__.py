"""WebGUI の起動の入口。

使い方:
    python -m web [--db データベースのパス] [--log-dir ログのフォルダ] [--host 待ち受けるアドレス] [--port ポート番号]
                  [--hostctl-dir 補助プログラムとやり取りするフォルダ]

--port を指定しない場合は、保存された webgui_port(既定値 8080)を使う。
保存されたポート番号で待ち受けられない場合は、既定値の 8080 で起動し、ログに記録する(決定事項 D-029)。
"""

import argparse
import logging
import os
import socket
from pathlib import Path

import uvicorn

from common.config_reader import get_settings
from common.config_writer import seed_initial_data
from common.constants import (
    COMPONENT_WEB,
    DEFAULT_DB_PATH,
    DEFAULT_LOG_DIR,
    DEFAULT_WEB_HOST,
    HOSTCTL_DIR_IN_CONTAINER,
    SETTING_DEFAULTS,
    SETTING_WEBGUI_PORT,
    WEBGUI_FALLBACK_PORT,
)
from common.database import connect, initialize_schema
from common.logging_setup import setup_logging
from web.app import create_app
from web.hostctl import HostCtlClient


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="web", description="WebGUI")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLiteデータベースのパス(既定値:{DEFAULT_DB_PATH})")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help=f"動作ログのフォルダ(既定値:{DEFAULT_LOG_DIR})")
    parser.add_argument("--host", default=DEFAULT_WEB_HOST, help=f"待ち受けるアドレス(既定値:{DEFAULT_WEB_HOST})")
    parser.add_argument("--port", type=int, default=None, help="待ち受けるポート番号(既定値:保存された webgui_port)")
    parser.add_argument(
        "--hostctl-dir",
        default=HOSTCTL_DIR_IN_CONTAINER,
        help=f"本体側の補助プログラムとやり取りするフォルダ(既定値:{HOSTCTL_DIR_IN_CONTAINER})",
    )
    return parser.parse_args()


def _bind(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if os.name != "nt":
            # 再起動の直後でも同じ番号で待ち受けられるようにする(Windows では使用中の番号に割り込めてしまうため使わない)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
    except OSError:
        sock.close()
        raise
    return sock


def open_listen_socket(
    host: str, port: int, logger: logging.Logger, fallback_port: int = WEBGUI_FALLBACK_PORT
) -> tuple[socket.socket, int]:
    """待ち受けの準備をする。指定の番号が使えなければ、既定値の番号を使う。実際の番号も返す。"""
    try:
        return _bind(host, port), port
    except OSError as exc:
        if port == fallback_port:
            logger.error("WebGUIのポート番号 %d で待ち受けられませんでした(%s)", port, exc)
            raise
        logger.error(
            "保存されたWebGUIのポート番号 %d で待ち受けられませんでした(%s)。既定値の %d で起動します", port, exc, fallback_port
        )
    return _bind(host, fallback_port), fallback_port


def saved_port(db_path: str) -> int:
    """保存されたWebGUIのポート番号。初回の起動に備え、先にデータベースを準備する(D-012)。"""
    conn = connect(db_path)
    try:
        initialize_schema(conn)
        seed_initial_data(conn)
        return int(get_settings(conn).get(SETTING_WEBGUI_PORT, SETTING_DEFAULTS[SETTING_WEBGUI_PORT]))
    finally:
        conn.close()


def main() -> None:
    args = parse_args()
    logger = setup_logging(COMPONENT_WEB, Path(args.log_dir))
    hostctl = HostCtlClient(Path(args.hostctl_dir))
    port = args.port if args.port is not None else saved_port(args.db)
    sock, actual_port = open_listen_socket(args.host, port, logger)
    app = create_app(args.db, logger, log_dir=Path(args.log_dir), hostctl=hostctl, listen_port=actual_port)
    if not hostctl.available:
        logger.info("本体側の補助プログラムが見つからないため、本体のIPアドレスとWebGUIのポート番号は変更できません")
    logger.info("WebGUIを起動します(ポート番号 %d)", actual_port)
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=actual_port, log_config=None, access_log=False))
    try:
        server.run(sockets=[sock])
    finally:
        logger.info("WebGUIを停止しました")


if __name__ == "__main__":
    main()
