import os
import uuid
import socket
import asyncio
from contextlib import suppress
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from .models import Task
from .db import SessionLocal
from .processors.video_pipeline import *
from .crud import list_queue, count_processing

# ----------------- 配置 -----------------
MAX_PARALLEL = int(os.getenv("MAX_PARALLEL_TASKS", "1"))

# 心跳 / 续租 / 判旧
LEASE_SECONDS = int(os.getenv("LEASE_SECONDS", "600"))            # 10 分钟
HEARTBEAT_SECONDS = int(os.getenv("HEARTBEAT_SECONDS", "15"))      # 15 秒
STALE_SECONDS = int(os.getenv("STALE_SECONDS", "120"))             # 120 秒（>2轮心跳）

# 仅统计 processing 段的最大时长
MAX_PROCESSING_SECONDS = int(os.getenv("MAX_PROCESSING_SECONDS", "600"))  # 10 分钟

# 自动重试（当模型无 attempt/max_attempts 字段时使用）
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "0"))

# 本 worker 唯一标识
WORKER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


# ----------------- 小工具 -----------------
def _now() -> datetime:
    return datetime.utcnow()


def _lease_deadline() -> datetime:
    return _now() + timedelta(seconds=LEASE_SECONDS)


def _hasattr_safe(obj, name: str) -> bool:
    try:
        getattr(obj, name)
        return True
    except Exception:
        return False


# ----------------- 内存兜底重试表 -----------------
_memory_attempts: dict[int, int] = {}


def _inc_memory_attempt(task_id: int) -> int:
    _memory_attempts[task_id] = _memory_attempts.get(task_id, 0) + 1
    return _memory_attempts[task_id]


def _reset_memory_attempt(task_id: int) -> None:
    with suppress(Exception):
        _memory_attempts.pop(task_id, None)


# ----------------- 核心执行（同步） -----------------
def _run_one_sync(task_id: int):
    """
    在线程里执行真正的重活；业务流程内部会多次 commit。
    这里负责错误收敛与兜底清理。
    """
    db: Session = SessionLocal()
    t = None
    try:
        t = db.get(Task, task_id)
        if not t:
            return

        # 若租约失效或被其它 worker 占用，直接退出
        if _hasattr_safe(t, "worker_id") and t.worker_id and t.worker_id != WORKER_ID:
            return
        if _hasattr_safe(t, "lease_until") and t.lease_until and t.lease_until < _now():
            return

        # 分派阶段
        if t.queued_for == "prepare":
            process_prepare(db, t)
        elif t.queued_for == "finalize":
            process_finalize(db, t)
        else:
            process_reburn(db, t)

        db.commit()
    except Exception as e:
        if t:
            t.status = "FAILED"
            t.error_msg = (str(e) or "task failed")[:500]
            db.add(t)
            db.commit()
    finally:
        # 结束统一清理占位信息（无论成功失败）
        try:
            t = db.get(Task, task_id)
            if t:
                if _hasattr_safe(t, "worker_id"):
                    t.worker_id = None
                if _hasattr_safe(t, "lease_until"):
                    t.lease_until = None
                if _hasattr_safe(t, "heartbeat_at"):
                    t.heartbeat_at = None
                if _hasattr_safe(t, "processing_started_at"):
                    t.processing_started_at = None
                db.add(t)
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()


# ----------------- 心跳与续租（异步） -----------------
async def _heartbeat_loop(task_id: int):
    """
    周期性刷新心跳与续租。
    ⚠️ 若发现任务已不在 PROCESSING（例如切到 REVIEW/QUEUED/SUCCESS/FAILED），
       立刻清空占位信息（包括 lease_until），然后退出心跳。
    """
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        db: Session = SessionLocal()
        try:
            t = db.get(Task, task_id)
            if not t:
                return
            # 不再由本 worker 持有，或状态不在 PROCESSING：清空并退出
            not_owner = _hasattr_safe(t, "worker_id") and t.worker_id and t.worker_id != WORKER_ID
            not_processing = t.status != "PROCESSING"
            if not_owner or not_processing:
                changed = False
                if _hasattr_safe(t, "worker_id") and t.worker_id is not None:
                    t.worker_id = None
                    changed = True
                if _hasattr_safe(t, "lease_until") and t.lease_until is not None:
                    t.lease_until = None
                    changed = True
                if _hasattr_safe(t, "heartbeat_at") and t.heartbeat_at is not None:
                    t.heartbeat_at = None
                    changed = True
                if _hasattr_safe(t, "processing_started_at") and t.processing_started_at is not None:
                    t.processing_started_at = None
                    changed = True
                if changed:
                    db.add(t)
                    db.commit()
                return

            # 仍在本 worker + PROCESSING：正常心跳与续租
            if _hasattr_safe(t, "heartbeat_at"):
                t.heartbeat_at = _now()
            if _hasattr_safe(t, "lease_until"):
                t.lease_until = _lease_deadline()
            db.add(t)
            db.commit()

        except Exception:
            db.rollback()
        finally:
            db.close()


# ----------------- 线程封装（异步） -----------------
async def _run_one(task_id: int):
    """
    启动线程跑同步重活；并发起心跳协程。
    """
    hb_task = asyncio.create_task(_heartbeat_loop(task_id))
    try:
        await asyncio.to_thread(_run_one_sync, task_id)
    finally:
        with suppress(Exception):
            hb_task.cancel()
            await hb_task


# ----------------- 救援/回收逻辑 -----------------
def _rescue_orphan_tasks(db: Session):
    """
    仅以 processing 段时长 + 心跳/租约来判定失联。
    - processing 段：从 processing_started_at 计时（若无则退化为 heartbeat_at/created_at）
    - 条件：租约过期 或 心跳过旧 或 processing 段超时
    命中后：优先回队列重试（清空占位与租约）；超出重试上限则 FAILED。
    """
    now = _now()
    stale_edge = now - timedelta(seconds=STALE_SECONDS)
    proc_tasks = db.query(Task).filter(Task.status == "PROCESSING").all()
    for t in proc_tasks:
        # 起点
        if _hasattr_safe(t, "processing_started_at") and t.processing_started_at:
            started = t.processing_started_at
        elif _hasattr_safe(t, "heartbeat_at") and t.heartbeat_at:
            started = t.heartbeat_at
        else:
            started = getattr(t, "created_at", now)
        processing_seconds = (now - started).total_seconds() if started else 0.0
        lease_expired = _hasattr_safe(t, "lease_until") and t.lease_until and t.lease_until < now
        heartbeat_stale = (not _hasattr_safe(t, "heartbeat_at")) or (not t.heartbeat_at) or (t.heartbeat_at < stale_edge)
        over_processing_cap = processing_seconds > MAX_PROCESSING_SECONDS

        if not (lease_expired or heartbeat_stale or over_processing_cap):
            continue

        reasons = []
        if lease_expired:
            reasons.append("lease_expired")
        if heartbeat_stale:
            reasons.append("heartbeat_stale")
        if over_processing_cap:
            reasons.append(f"processing>{MAX_PROCESSING_SECONDS}s")

        # 自动重试
        can_retry_by_model = _hasattr_safe(t, "attempt") and _hasattr_safe(t, "max_attempts") and (t.max_attempts is not None)
        if can_retry_by_model:
            t.attempt = (t.attempt or 0) + 1
            left = (t.max_attempts or 0) - (t.attempt or 0)
            if left >= 0:
                t.status = "QUEUED"
                t.enqueued_at = datetime.utcnow() 
                t.error_msg = f"检测到任务失联/超时（{', '.join(reasons)}），自动重试第 {t.attempt} 次。"
            else:
                t.status = "FAILED"
                t.error_msg = f"任务失联/超时并达到最大重试次数（{', '.join(reasons)}）。"
        else:
            attempt = _inc_memory_attempt(int(t.id))
            if attempt <= MAX_RETRIES:
                t.status = "QUEUED"
                t.enqueued_at = datetime.utcnow() 
                t.error_msg = f"检测到任务失联/超时（{', '.join(reasons)}），自动重试第 {attempt} 次。"
            else:
                t.status = "FAILED"
                t.error_msg = f"任务失联/超时并达到最大重试次数（{', '.join(reasons)}）。"

        # 清空占位信息（包含 lease_until）
        if _hasattr_safe(t, "worker_id"):
            t.worker_id = None
        if _hasattr_safe(t, "lease_until"):
            t.lease_until = None
        if _hasattr_safe(t, "heartbeat_at"):
            t.heartbeat_at = None
        if _hasattr_safe(t, "processing_started_at"):
            t.processing_started_at = None

        db.add(t)

        if t.status == "FAILED":
            _reset_memory_attempt(int(t.id))

    db.commit()


# ----------------- 分发器 -----------------
async def dispatcher():
    """
    每 2 秒调度一次：
      1) 救援失联任务；
      2) 按并发上限拉起 QUEUED 任务，进入 PROCESSING；
      3) 为每个启动的任务开线程执行，并在事件循环里维护心跳。
    """
    while True:
        db: Session = SessionLocal()
        try:
            # 1) 先救援
            _rescue_orphan_tasks(db)

            # 2) 拉起新任务
            running = count_processing(db)
            slots = max(0, MAX_PARALLEL - running)
            if slots > 0:
                queue = list_queue(db)[:slots]
                for t in queue:
                    if t.status != "QUEUED":
                        continue
                    # 占位并进入处理；仅此刻记录 processing_started_at（排队不计时）
                    t.status = "PROCESSING"
                    if _hasattr_safe(t, "worker_id"):
                        t.worker_id = WORKER_ID
                    if _hasattr_safe(t, "lease_until"):
                        t.lease_until = _lease_deadline()
                    if _hasattr_safe(t, "heartbeat_at"):
                        t.heartbeat_at = _now()
                    if _hasattr_safe(t, "processing_started_at"):
                        t.processing_started_at = _now()
                    db.add(t)
                    db.commit()

                    # 线程执行 + 心跳
                    asyncio.create_task(_run_one(t.id))
        except Exception:
            db.rollback()
        finally:
            db.close()

        await asyncio.sleep(2)
