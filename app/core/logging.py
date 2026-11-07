"""日志 —— 控制台 + 滚动文件，统一格式。

为什么要做这件事：
出问题时只看终端输出等于没有记录。运营跑出一次错误的诊断，
三天后要复盘"当时输入了什么、哪个环节报错"，必须能翻到日志。
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_DIR = BASE_DIR / "data" / "logs"

_CONFIGURED = False
_FMT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"


def setup_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(_FMT, "%Y-%m-%d %H:%M:%S")

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    # 5MB × 3 份，够查一周
    file_handler = RotatingFileHandler(
        LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    # 第三方库的噪音压掉
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("multipart").setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
