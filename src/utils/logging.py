"""统一日志（loguru）。"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from src.config import load_settings


_initialized = False


def get_logger():
    global _initialized
    if not _initialized:
        s = load_settings()
        logger.remove()
        logger.add(
            sys.stderr,
            level=s.log_level,
            format="<green>{time:HH:mm:ss}</green> | <level>{level: <7}</level> | "
                   "<cyan>{name}:{function}:{line}</cyan> | {message}",
        )
        log_file = Path(s.logs_dir) / "pipeline.log"
        logger.add(
            log_file,
            level="DEBUG",
            rotation="10 MB",
            retention=5,
            encoding="utf-8",
            enqueue=True,
        )
        _initialized = True
    return logger
