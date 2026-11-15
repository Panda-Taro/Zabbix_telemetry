"""WebGUI の起動の入口。

使い方:
    python -m web [--db データベースのパス] [--log-dir ログのフォルダ] [--host 待ち受けるアドレス] [--port ポート番号]

--port を指定しない場合は、全体設定の webgui_port(既定値 8080)を使う。
"""

import argparse
from pathlib import Path

import uvicorn

from common.config_reader import get_settings
from common.constants import (
    COMPONENT_WEB,
    DEFAULT_DB_PATH,
    DEFAULT_LOG_DIR,
    DEFAULT_WEB_HOST,
    SETTING_DEFAULTS,
    SETTING_WEBGUI_PORT,
)
from common.database import connect
from common.logging_setup import setup_logging
from web.app import create_app


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="web", description="WebGUI")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLiteデータベースのパス(既定値:{DEFAULT_DB_PATH})")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help=f"動作ログのフォルダ(既定値:{DEFAULT_LOG_DIR})")
    parser.add_argument("--host", default=DEFAULT_WEB_HOST, help=f"待ち受けるアドレス(既定値:{DEFAULT_WEB_HOST})")
    parser.add_argument("--port", type=int, default=None, help="待ち受けるポート番号(既定値:全体設定の webgui_port)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logger = setup_logging(COMPONENT_WEB, Path(args.log_dir))
    app = create_app(args.db, logger, log_dir=Path(args.log_dir))
    port = args.port
    if port is None:
        conn = connect(args.db)
        try:
            port = int(get_settings(conn).get(SETTING_WEBGUI_PORT, SETTING_DEFAULTS[SETTING_WEBGUI_PORT]))
        finally:
            conn.close()
    logger.info("WebGUIを起動します(ポート番号 %d)", port)
    try:
        uvicorn.run(app, host=args.host, port=port, log_config=None, access_log=False)
    finally:
        logger.info("WebGUIを停止しました")


if __name__ == "__main__":
    main()
