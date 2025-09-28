import os
import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler

LOGDIR = "log"

def _ensure_logdir():
    os.makedirs(LOGDIR, exist_ok=True)

def _daily_log_path():
    day = datetime.now().strftime("%Y%m%d")
    return os.path.join(LOGDIR, f"{day}_app.log")

def get_app_logger():
    """既存運用の形式に寄せたアプリロガー"""
    _ensure_logdir()
    logger = logging.getLogger("gmaps_mvp")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)

    handler = RotatingFileHandler(_daily_log_path(), maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fmt = logging.Formatter("[%(asctime)s][%(levelname)s] %(message)s", datefmt="%Y:%m:%d %H:%M:%S")
    handler.setFormatter(fmt)
    logger.addHandler(handler)

    # コンソールにも出す
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    return logger
