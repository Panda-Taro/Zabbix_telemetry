"""動作ログの出力(8.5、LG-01〜LG-04)。collector と web の両方が使う。

- 1行を1件として、JSON形式で書き出す(8.5.1)
- 1ファイルが10MBに達したら切り替え、古いファイルは10世代まで残す(8.5.2)
- 同じ内容のログが大量に出る場合は、1分ごとに件数をまとめて出力する(8.5.3)
"""

import json
import logging
import sys
import time
from collections.abc import Callable
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from common.constants import LOG_AGGREGATION_INTERVAL_SEC, LOG_BACKUP_COUNT, LOG_LEVELS, LOG_MAX_BYTES

# ログの1件に、関係する機器のホスト名を付けるときの名前(logger.info(..., extra={HOSTNAME_FIELD: "..."}))
HOSTNAME_FIELD = "hostname"


class JsonLineFormatter(logging.Formatter):
    """ログを、1行1件のJSONにする(8.5.1)。"""

    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.exc_info:
            message += "\n" + self.formatException(record.exc_info)
        level = record.levelname if record.levelname in LOG_LEVELS else "ERROR"
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": level,
            "component": self.component,
            "hostname": str(getattr(record, HOSTNAME_FIELD, "") or ""),
            "message": message,
        }
        return json.dumps(entry, ensure_ascii=False)


def setup_logging(component: str, log_dir: Path | None, level: str = "INFO") -> logging.Logger:
    """component(collector / web)用の動作ログを用意する。

    log_dir を指定した場合は <log_dir>/<component>.log に書き出す。
    あわせて標準エラー出力にも同じ内容を出す(Dockerのログで見られるようにするため)。
    """
    logger = logging.getLogger(component)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = False
    formatter = JsonLineFormatter(component)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / f"{component}.log",
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    set_log_level(logger, level)
    return logger


def set_log_level(logger: logging.Logger, level: str) -> None:
    """ログレベルを変える(LG-04)。DEBUG / INFO / WARNING / ERROR 以外は INFO とする。"""
    name = level.upper() if level.upper() in LOG_LEVELS else "INFO"
    logger.setLevel(getattr(logging, name))


class LogAggregator:
    """同じ内容のログを、一定間隔ごとに件数をまとめて出力する(8.5.3、LG-03)。

    add() で数えておき、flush_if_due() を定期的に呼ぶと、間隔が過ぎた分をまとめて出力する。
    """

    def __init__(
        self,
        logger: logging.Logger,
        interval_sec: int = LOG_AGGREGATION_INTERVAL_SEC,
        now_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        self.logger = logger
        self.interval_ns = interval_sec * 1_000_000_000
        self.interval_sec = interval_sec
        self._now_ns = now_ns
        self._entries: dict[tuple[int, str, str], int] = {}
        self._window_started_ns = now_ns()

    def add(self, level: int, message: str, hostname: str = "", count: int = 1) -> None:
        """出力したい内容を数える。同じ level・message・hostname は1件にまとめる。"""
        key = (level, message, hostname)
        self._entries[key] = self._entries.get(key, 0) + count

    def flush_if_due(self) -> None:
        if self._now_ns() - self._window_started_ns >= self.interval_ns:
            self.flush()

    def flush(self) -> None:
        """数えた内容を、件数を付けて出力する。"""
        entries, self._entries = self._entries, {}
        self._window_started_ns = self._now_ns()
        for (level, message, hostname), count in entries.items():
            self.logger.log(
                level,
                "%s(直近%d秒間に%d件)",
                message,
                self.interval_sec,
                count,
                extra={HOSTNAME_FIELD: hostname},
            )
