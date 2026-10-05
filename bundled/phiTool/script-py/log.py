import logging
import sys
import json
from typing import Optional, Dict, Any


class Logger:
    def __init__(self, name: Optional[str] = None, silent: bool = False, json_output: bool = False):
        self.silent = silent
        self.json_output = json_output
        self.logger = logging.getLogger(name or "phiTool")
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()

        if not silent:
            console_handler = logging.StreamHandler(sys.stderr)

            if json_output:
                formatter = logging.Formatter("%(message)s")
            else:
                log_format = (
                    "\033[36m%(asctime)s\033[0m "
                    "[phiTool] "
                    "\033[1;%(color)s%(levelname)-8s\033[0m "
                    "%(message)s"
                )

                level_colors = {
                    logging.DEBUG: "37m",
                    logging.INFO: "32m",
                    logging.WARNING: "33m",
                    logging.ERROR: "31m",
                    logging.CRITICAL: "41m",
                }

                class ColoredFormatter(logging.Formatter):
                    def format(self, record):
                        record.color = level_colors.get(record.levelno, "37m")
                        return super().format(record)

                formatter = ColoredFormatter(log_format, datefmt="%Y-%m-%d %H:%M:%S")

            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

        self.logger.propagate = True

    def _log(self, level: int, message: str, **kwargs):
        if self.silent:
            return

        if self.json_output:
            log_data: Dict[str, Any] = {
                "level": logging.getLevelName(level),
                "message": message
            }
            log_data.update(kwargs)
            print(json.dumps(log_data, ensure_ascii=False))
        else:
            self.logger.log(level, message)

    def debug(self, message: str, **kwargs):
        self._log(logging.DEBUG, message, **kwargs)

    def info(self, message: str, **kwargs):
        self._log(logging.INFO, message, **kwargs)

    def warning(self, message: str, **kwargs):
        self._log(logging.WARNING, message, **kwargs)

    def error(self, message: str, **kwargs):
        self._log(logging.ERROR, message, **kwargs)

    def critical(self, message: str, **kwargs):
        self._log(logging.CRITICAL, message, **kwargs)


def init_console_logger(
        name: Optional[str] = None,
        level: int = logging.INFO,
        log_format: Optional[str] = None
) -> logging.Logger:
    logger = logging.getLogger(name or "phiTool")
    logger.setLevel(level)
    logger.handlers.clear()

    if log_format is None:
        log_format = (
            "\033[36m%(asctime)s\033[0m "
            "[phiTool] "
            "\033[1;%(color)s%(levelname)-8s\033[0m "
            "%(message)s"
        )

    level_colors = {
        logging.DEBUG: "37m",
        logging.INFO: "32m",
        logging.WARNING: "33m",
        logging.ERROR: "31m",
        logging.CRITICAL: "41m",
    }

    class ColoredFormatter(logging.Formatter):
        def format(self, record):
            record.color = level_colors.get(record.levelno, "37m")
            return super().format(record)

    # GUI / redirected stdout 没有稳定的 console buffer；强行 TextIOWrapper 会炸。
    # 仅对真实终端尝试切 UTF-8。
    try:
        if hasattr(sys.stdout, "reconfigure") and hasattr(sys.stdout, "fileno"):
            try:
                sys.stdout.fileno()
            except Exception:
                pass
            else:
                sys.stdout.reconfigure(encoding="utf-8")
        elif sys.platform == "win32" and hasattr(sys.stdout, "buffer") and hasattr(sys.stdout, "fileno"):
            try:
                sys.stdout.fileno()
            except Exception:
                pass
            else:
                import io

                sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

    console_handler = logging.StreamHandler(sys.stderr)
    console_formatter = ColoredFormatter(
        log_format,
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    logger.propagate = True

    return logger
