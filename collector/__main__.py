"""collector の起動の入口。

使い方:
    python -m collector [--db データベースのパス] [--log-dir ログのフォルダ]
"""

import argparse
import asyncio
import signal
from pathlib import Path

from collector.app import CollectorApp
from common.constants import COMPONENT_COLLECTOR, DEFAULT_DB_PATH, DEFAULT_LOG_DIR
from common.logging_setup import setup_logging


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="collector", description="Telemetryの収集エンジン")
    parser.add_argument("--db", default=DEFAULT_DB_PATH, help=f"SQLiteデータベースのパス(既定値:{DEFAULT_DB_PATH})")
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help=f"動作ログのフォルダ(既定値:{DEFAULT_LOG_DIR})")
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    logger = setup_logging(COMPONENT_COLLECTOR, Path(args.log_dir))
    app = CollectorApp(args.db, logger)
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, signal_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, app.request_stop)
        except NotImplementedError:
            pass  # Windows では使えない(開発用PCでの起動時)
    await app.run_forever()


if __name__ == "__main__":
    asyncio.run(main())
