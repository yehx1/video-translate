# app/logs.py
# -*- coding: utf-8 -*-
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

# ---- colorama（可选）----
try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    _HAS_COLORAMA = True
except Exception:
    _HAS_COLORAMA = False
    Fore = None  # type: ignore
    Style = None  # type: ignore

# ---- 环境变量（可选）----
# 基础默认级别（当下面两个未设置时使用）
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()               # DEBUG/INFO/WARNING/ERROR

# 分别控制 控制台/文件 的级别（未设置则回退到 LOG_LEVEL）
LOG_LEVEL_CONSOLE = os.getenv("LOG_LEVEL_CONSOLE", LOG_LEVEL).upper()
LOG_LEVEL_FILE    = os.getenv("LOG_LEVEL_FILE", "WARNING").upper()

LOG_FILE = os.getenv("LOG_FILE", "../logs/be.log").strip()                     # 为空=不写文件
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", "10_485_760"))    # 10MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))       # 轮转文件数
LOG_COLOR = os.getenv("LOG_COLOR", "1") == "1"                   # 控制台彩色开关
LOG_UVICORN_WIRE = os.getenv("LOG_UVICORN_WIRE", "0") == "1"     # 是否放开 uvicorn access 全量日志

_CONFIGURED = False  # 防重复配置

# ---- 彩色 Formatter ----
class ColoredFormatter(logging.Formatter):
    COLOR_CODES = {
        'DEBUG': '\033[34m',     # 蓝
        'INFO': '\033[32m',      # 绿
        'WARNING': '\033[33m',   # 黄
        'ERROR': '\033[31m',     # 红
        'CRITICAL': '\033[35m',  # 洋红
    }
    if _HAS_COLORAMA and Fore is not None:
        COLOR_CODES = {
            'DEBUG': Fore.BLUE,
            'INFO': Fore.GREEN,
            'WARNING': Fore.YELLOW,
            'ERROR': Fore.RED,
            'CRITICAL': Fore.MAGENTA,
        }

    RESET = '\033[0m' if not _HAS_COLORAMA else (Style.RESET_ALL if Style else '\033[0m')

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        color = self.COLOR_CODES.get(record.levelname, '')
        reset = self.RESET if color else ''
        return f"{color}{msg}{reset}"

_PLAIN_FMT = "%(asctime)s | %(levelname)s | [%(name)s] [%(filename)s:%(lineno)d] - %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"

def _make_console_handler() -> logging.Handler:
    level = getattr(logging, LOG_LEVEL_CONSOLE, logging.INFO)
    h = logging.StreamHandler(sys.stdout)
    h.setLevel(level)
    if LOG_COLOR:
        h.setFormatter(ColoredFormatter(_PLAIN_FMT, _DATE_FMT))
    else:
        h.setFormatter(logging.Formatter(_PLAIN_FMT, _DATE_FMT))
    return h

def _make_file_handler() -> logging.Handler | None:
    if not LOG_FILE:
        return None
    level = getattr(logging, LOG_LEVEL_FILE, logging.INFO)
    dirpath = os.path.dirname(LOG_FILE) or "."
    os.makedirs(dirpath, exist_ok=True)
    fh = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8"
    )
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(_PLAIN_FMT, _DATE_FMT))  # 文件不带颜色
    return fh

def _silence_noisy_loggers():
    # Uvicorn/Starlette 噪声控制
    logging.getLogger("starlette").setLevel(logging.INFO)
    if LOG_UVICORN_WIRE:
        logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    else:
        logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    logging.getLogger("uvicorn").setLevel(logging.INFO)

    # 常见噪声组件
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

def configure_logging() -> None:
    """全局初始化（幂等）。在应用启动最早期调用一次。"""
    global _CONFIGURED
    if _CONFIGURED:
        return

    # 应用“根”logger（避免直接改 root），并设置一个基线级别（放最宽或取最低）
    app_logger = logging.getLogger("app")
    # 选择两个 handler 级别的较低者作为 logger 的 level，确保不过滤掉更详细的那端
    base_level = min(
        getattr(logging, LOG_LEVEL_CONSOLE, logging.INFO),
        getattr(logging, LOG_LEVEL_FILE, logging.INFO)
    )
    app_logger.setLevel(base_level)
    app_logger.propagate = False  # 不向上冒泡，避免重复

    # 清理旧 handler（热重载/多次导入时很重要）
    for h in list(app_logger.handlers):
        app_logger.removeHandler(h)

    # 控制台
    app_logger.addHandler(_make_console_handler())
    # 文件（可选）
    fh = _make_file_handler()
    if fh:
        app_logger.addHandler(fh)

    _silence_noisy_loggers()
    _CONFIGURED = True

def get_logger(name: str = __name__) -> logging.Logger:
    """模块内获取 logger：get_logger(__name__)"""
    if not _CONFIGURED:
        configure_logging()
    return logging.getLogger(f"app.{name}")

# ---- FastAPI 请求日志（可选）----
def attach_request_logger(app) -> None:
    """简易 HTTP 日志中间件。"""
    import time
    from starlette.requests import Request
    from starlette.responses import Response

    log = get_logger("http")

    @app.middleware("http")
    async def _log_mw(request: Request, call_next):
        start = time.perf_counter()
        response: Response | None = None
        try:
            response = await call_next(request)
            return response
        finally:
            cost = int((time.perf_counter() - start) * 1000)
            status = getattr(response, "status_code", 0) if response else 0
            log.info(
                "%s %s -> %s %sms",
                request.method,
                request.url.path,
                status,
                cost,
                extra={
                    "path": request.url.path,
                    "method": request.method,
                    "status": status,
                    "ms": cost
                },
            )
