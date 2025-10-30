# app/cancel.py
# -*- coding: utf-8 -*-
import os
import signal
import threading
from typing import Dict, Set, Optional
from subprocess import Popen

# 记录“请求取消”的任务 & 正在运行的子进程
_STOP_REQ: Set[int] = set()
_PROCS: Dict[int, Set[Popen]] = {}
_LOCK = threading.RLock()

def request_stop(task_id: int) -> None:
    """标记取消并尽最大努力终止该任务名下所有子进程（整组）"""
    with _LOCK:
        _STOP_REQ.add(task_id)
        procs = list(_PROCS.get(task_id, set()))
    # 进程组杀掉（优先 TERM，短暂等待后 KILL）
    for p in procs:
        try:
            pgid = os.getpgid(p.pid)
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            pass
    for p in procs:
        try:
            p.wait(timeout=2.0)
        except Exception:
            try:
                pgid = os.getpgid(p.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                pass
    # 清理登记
    with _LOCK:
        _PROCS.pop(task_id, None)

def clear_stop(task_id: int) -> None:
    """可选：任务真正结束后清理状态"""
    with _LOCK:
        _STOP_REQ.discard(task_id)
        _PROCS.pop(task_id, None)

def is_stop_requested(task_id: Optional[int]) -> bool:
    if task_id is None:
        return False
    with _LOCK:
        return task_id in _STOP_REQ

def register_process(task_id: int, p: Popen) -> None:
    with _LOCK:
        _PROCS.setdefault(task_id, set()).add(p)

def unregister_process(task_id: int, p: Popen) -> None:
    with _LOCK:
        s = _PROCS.get(task_id)
        if not s: 
            return
        s.discard(p)
        if not s:
            _PROCS.pop(task_id, None)
