"""
统一日志工具 —— 彩色控制台 + 文件轮转双输出
日志文件存放在 code/logs/ 目录
"""

import copy
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# 日志目录统一放在项目根目录 code/logs/
LOG_DIR = Path(__file__).resolve().parent.parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# ── 彩色格式化器 ──

_COLORS = {
    "DEBUG":    "\033[36m",   # 青色
    "INFO":     "\033[32m",   # 绿色
    "WARNING":  "\033[33m",   # 黄色
    "ERROR":    "\033[31m",   # 红色
    "CRITICAL": "\033[1;31m", # 粗体红
}
_RESET = "\033[0m"


class ColorFormatter(logging.Formatter):
    """终端彩色日志格式化器（不污染原始 record）"""

    def format(self, record: logging.LogRecord) -> str:
        record = copy.copy(record)
        color = _COLORS.get(record.levelname, "")
        record.levelname = f"{color}{record.levelname:<7}{_RESET}"
        return super().format(record)


def get_logger(name: str = "app") -> logging.Logger:
    """获取或创建带彩色控制台 + 文件轮转的 logger"""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    base_fmt = "[%(asctime)s] %(levelname)s %(name)s - %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    # 控制台（彩色）
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(ColorFormatter(base_fmt, datefmt=datefmt))
    logger.addHandler(sh)

    # 文件（轮转：单文件 5MB，保留 3 份）
    fh = RotatingFileHandler(
        LOG_DIR / "run.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(base_fmt, datefmt=datefmt))
    logger.addHandler(fh)

    return logger
