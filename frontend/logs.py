# subtitle_processor/logs.py
# -*- coding: utf-8 -*-
"""
Django 前端通用日志：控制台级别与文件级别可分开设置
- 控制台：默认 INFO+
- 文件：默认 WARNING+（不保存 INFO）
- 轮转日志：10MB x 5
- 彩色控制台（colorama 自动检测）
- 幂等初始化，避免重复 handler
"""

from __future__ import annotations
import logging
import os
import sys
from typing import Optional
from logging.handlers import RotatingFileHandler

# ---------- 可选依赖：colorama ----------
try:
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
    _HAS_COLORAMA = True
except Exception:
    _HAS_COLORAMA = False

# ---------- 读取环境变量 ----------
LOG_COLOR = os.getenv("LOG_COLOR", "1") == "1"
LOG_LEVEL_CONSOLE = os.getenv("LOG_LEVEL_CONSOLE", "INFO").upper()    # 控制台级别
LOG_LEVEL_FILE = os.getenv("LOG_LEVEL_FILE", "WARNING").upper()       # 文件级别（默认 WARNING，不落 INFO）
LOG_FILE = os.getenv("LOG_FILE", "../logs/fe.log").strip()                          # 为空则默认放在 BASE_DIR/logs/django-frontend.log
LOG_MAX_BYTES = int(os.getenv("LOG_MAX_BYTES", "10485760"))           # 10MB
LOG_BACKUP_COUNT = int(os.getenv("LOG_BACKUP_COUNT", "5"))
LOG_CAPTURE_DJANGO_SERVER = os.getenv("LOG_CAPTURE_DJANGO_SERVER", "1") == "1"

# ---------- 全局状态 ----------
_CONFIGURED = False

# ---------- 基础格式 ----------
_PLAIN_FMT = "%(asctime)s | %(levelname)s | [%(name)s] [%(filename)s:%(lineno)d] - %(message)s"
_DATE_FMT = "%Y-%m-%d %H:%M:%S"


# ---------- 彩色 Formatter ----------
class ColoredFormatter(logging.Formatter):
    if _HAS_COLORAMA:
        COLOR_CODES = {
            "DEBUG": Fore.BLUE,
            "INFO": Fore.GREEN,
            "WARNING": Fore.YELLOW,
            "ERROR": Fore.RED,
            "CRITICAL": Fore.MAGENTA,
        }
        RESET = Style.RESET_ALL
    else:
        COLOR_CODES = {
            "DEBUG": "\033[34m",
            "INFO": "\033[32m",
            "WARNING": "\033[33m",
            "ERROR": "\033[31m",
            "CRITICAL": "\033[35m",
        }
        RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        color = self.COLOR_CODES.get(record.levelname, "")
        reset = self.RESET if color else ""
        return f"{color}{msg}{reset}"


# ---------- 工具：创建 handler ----------
def _level_from_name(name: str, default: int) -> int:
    return getattr(logging, (name or "").upper(), default)

def _resolve_log_file(default_name: str = "django-frontend.log") -> Optional[str]:
    if LOG_FILE:
        path = LOG_FILE
    else:
        # 默认放在 settings.BASE_DIR/logs/xxx.log
        try:
            from django.conf import settings
            base_dir = getattr(settings, "BASE_DIR", None)
            if not base_dir:
                # 兜底：当前工作目录
                base_dir = os.getcwd()
            path = os.path.join(str(base_dir), "logs", default_name)
        except Exception:
            path = os.path.join(os.getcwd(), "logs", default_name)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path

def _make_console_handler(level: int) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    if LOG_COLOR and sys.stdout.isatty():
        handler.setFormatter(ColoredFormatter(_PLAIN_FMT, _DATE_FMT))
    else:
        handler.setFormatter(logging.Formatter(_PLAIN_FMT, _DATE_FMT))
    return handler

def _make_file_handler(level: int) -> Optional[logging.Handler]:
    path = _resolve_log_file()
    if not path:
        return None
    fh = RotatingFileHandler(
        path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(level)
    fh.setFormatter(logging.Formatter(_PLAIN_FMT, _DATE_FMT))  # 文件不带颜色
    return fh


# ---------- 降噪：常见第三方 ----------
def _silence_noisy_loggers():
    # Django 常见噪声
    logging.getLogger("django.db.backends").setLevel(logging.WARNING)   # SQL 查询
    logging.getLogger("django.template").setLevel(logging.INFO)
    logging.getLogger("django.request").setLevel(logging.INFO)

    # runserver 内置的 "django.server"（访问日志）
    # - INFO: 一般访问
    # - WARNING: 仅警告/错误
    if LOG_CAPTURE_DJANGO_SERVER:
        logging.getLogger("django.server").setLevel(logging.INFO)
    else:
        logging.getLogger("django.server").setLevel(logging.WARNING)

    # 网络库
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)

    # 你如有前后端交互 SDK，可在此加上：
    # logging.getLogger("celery").setLevel(logging.INFO)
    # logging.getLogger("boto3").setLevel(logging.WARNING)
    # logging.getLogger("botocore").setLevel(logging.WARNING)


# ---------- 对外：初始化 & 获取 logger ----------
def configure_logging() -> None:
    """
    在 Django 启动早期调用（例如 AppConfig.ready()、或 settings 导入阶段）。
    幂等：多次调用只生效一次。
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    # 用一个应用根 logger，避免直接污染 root
    app_logger = logging.getLogger("frontend")
    app_logger.propagate = False

    # 清理旧 handler（热重载时很重要）
    for h in list(app_logger.handlers):
        app_logger.removeHandler(h)

    # 设置各自级别
    console_level = _level_from_name(LOG_LEVEL_CONSOLE, logging.INFO)
    file_level = _level_from_name(LOG_LEVEL_FILE, logging.WARNING)

    app_logger.setLevel(min(console_level, file_level))  # 基础 level 设为两者较低值

    # 控制台
    app_logger.addHandler(_make_console_handler(console_level))

    # 文件（可选）
    fh = _make_file_handler(file_level)
    if fh:
        app_logger.addHandler(fh)

    # 降噪
    _silence_noisy_loggers()

    # 让 Django 自家 logger 往我们这边走一份（可选）
    # 注意：此举可避免单独配置 settings.LOGGING；若你已有 settings.LOGGING，可不做这一步
    logging.getLogger().setLevel(logging.WARNING)  # root 稍微抬高，避免重复
    _CONFIGURED = True


def get_logger(name: str = __name__) -> logging.Logger:
    """
    其它模块取日志：
        from subtitle_processor.logs import get_logger
        log = get_logger(__name__)
    """
    if not _CONFIGURED:
        configure_logging()
    # 统一挂到 "frontend.*" 命名空间
    name = name if name and name != "__main__" else "app"
    return logging.getLogger(f"frontend.{name}")


# ---------- 可选：Django 请求日志中间件 ----------
class RequestLogMiddleware:
    """
    轻量请求日志：在 settings.MIDDLEWARE 中添加：
        'subtitle_processor.logs.RequestLogMiddleware',
    """
    def __init__(self, get_response):
        self.get_response = get_response
        self.log = get_logger("http")

    def __call__(self, request):
        import time
        start = time.perf_counter()
        resp = None
        try:
            resp = self.get_response(request)
            return resp
        finally:
            cost_ms = int((time.perf_counter() - start) * 1000)
            status = getattr(resp, "status_code", 0) if resp else 0
            path = request.path
            method = request.method
            user = getattr(request, "user", None)
            uid = getattr(user, "id", None)
            uname = getattr(user, "username", None)
            self.log.info(
                "%s %s -> %s %sms%s",
                method, path, status, cost_ms,
                f" (user={uid or '-'}:{uname or '-'})",
                extra={"path": path, "method": method, "status": status, "ms": cost_ms, "user_id": uid},
            )
