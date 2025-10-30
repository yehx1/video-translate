from sqlalchemy.orm import Session
from sqlalchemy import select, func, and_, or_
from . import models

def count_queued(db: Session) -> int:
    stmt = select(func.count()).select_from(models.Task).where(models.Task.status == "QUEUED")
    return int(db.execute(stmt).scalar() or 0)

def count_user_queued(db: Session, user_id: str, exclude_task_id: int | None = None) -> int:
    """
    统计该用户处于 QUEUED 状态的任务数量；可排除某个任务（用于 confirm/reburn/restart 时避免把自己算进去）
    """
    if not user_id:
        return 0
    cond = [models.Task.status == "QUEUED", models.Task.user_id == user_id]
    if exclude_task_id is not None:
        cond.append(models.Task.id != exclude_task_id)
    stmt = select(func.count()).select_from(models.Task).where(and_(*cond))
    return int(db.execute(stmt).scalar() or 0)

def queue_position_and_length(db: Session, task: models.Task) -> tuple[int, int]:
    if not task or task.status != "QUEUED":
        return 0, 0
    total = count_queued(db)

    # 使用 COALESCE(enqueued_at, created_at) 保证历史数据也能正确排序
    key = func.coalesce(models.Task.enqueued_at, models.Task.created_at)
    task_key = task.enqueued_at or task.created_at

    earlier_cnt_stmt = select(func.count()).select_from(models.Task).where(
        and_(
            models.Task.status == "QUEUED",
            or_(
                key < task_key,
                and_(key == task_key, models.Task.id < task.id)
            )
        )
    )
    ahead = int(db.execute(earlier_cnt_stmt).scalar() or 0)
    pos = min(total, ahead + 1) if total > 0 else 0
    return pos, total

def create_task(db: Session, **kwargs) -> models.Task:
    t = models.Task(**kwargs)
    db.add(t); db.flush(); db.refresh(t)
    return t

def get_task(db: Session, task_id: int) -> models.Task | None:
    return db.get(models.Task, task_id)

def list_tasks(db: Session, user_id: str | None = None):
    stmt = select(models.Task).order_by(models.Task.created_at.desc())
    if user_id:
        stmt = stmt.where(models.Task.user_id == user_id)
    return db.execute(stmt).scalars().all()

def list_queue(db: Session):
    order_key = func.coalesce(models.Task.enqueued_at, models.Task.created_at)
    stmt = (
        select(models.Task)
        .where(models.Task.status=="QUEUED")
        .order_by(order_key.asc(), models.Task.id.asc())
    )
    return db.execute(stmt).scalars().all()

def count_processing(db: Session) -> int:
    stmt = select(func.count()).select_from(models.Task).where(models.Task.status == "PROCESSING")
    return int(db.execute(stmt).scalar() or 0)

def get_subtitles(db: Session, task_id: int):
    return db.execute(select(models.Subtitle).where(models.Subtitle.task_id==task_id).order_by(models.Subtitle.sequence)).scalars().all()
