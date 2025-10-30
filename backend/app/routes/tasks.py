import os, shutil
from typing import Optional
from datetime import datetime
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException

from ..db import get_db
from .. import crud, models
from ..processors.utils import MEDIA_ROOT
from ..schemas import TaskCreate, TaskOut, TaskDetail, ProgressOut

from app.logs import get_logger
from app.cancel import request_stop
log = get_logger(__name__)

# === 每用户排队上限（仅统计 QUEUED），默认 1，可用环境变量覆盖 ===
MAX_QUEUED_PER_USER = int(os.getenv("MAX_QUEUED_PER_USER", "1"))

def _can_user_queue_now(db: Session, user_id: str, exclude_task_id: int | None = None) -> bool:
    """
    返回该用户是否仍可入队（未达到排队上限）。
    exclude_task_id: 在 confirm/reburn/restart 时避免把自己算进统计。
    """
    if not user_id:
        # 模型 Task.user_id 非空；这里兜底为不可入队
        return False
    cur = crud.count_user_queued(db, user_id=user_id, exclude_task_id=exclude_task_id)
    return cur < MAX_QUEUED_PER_USER

def _ensure_user_queue_slot_or_409(db: Session, user_id: str, exclude_task_id: int | None = None):
    """
    用于 confirm / reburn / restart 这些“需要入队”的接口：若已达上限则 409。
    """
    if not _can_user_queue_now(db, user_id=user_id, exclude_task_id=exclude_task_id):
        cur = crud.count_user_queued(db, user_id=user_id, exclude_task_id=exclude_task_id)
        raise HTTPException(
            status_code=409,
            detail=f"该用户当前已有 {cur} 个任务排队，已达到上限 {MAX_QUEUED_PER_USER}。请等排队任务完成或停止后再重试。"
        )

class TaskStyleUpdate(BaseModel):
    subtitle_format: Optional[str] = Field(default=None, pattern="^(srt|ass)$")
    burn_subtitle: Optional[bool] = None
    sub_font_name: Optional[str] = None
    sub_font_size: Optional[int] = None
    sub_font_bold: Optional[bool] = None
    sub_font_italic: Optional[bool] = None
    sub_font_underline: Optional[bool] = None
    sub_font_color: Optional[str] = None
    sub_outline_color: Optional[str] = None
    sub_back_color: Optional[str] = None
    sub_outline_width: Optional[float] = None
    sub_back_opacity: Optional[float] = None
    sub_alignment: Optional[int] = None
    bgm_volume: Optional[float] = None
    tts_volume: Optional[float] = None

    tts_gender: Optional[str] = None
    tts_voice: Optional[str] = None
    tts_name: Optional[str] = None

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

@router.post("", response_model=TaskOut)
async def create_task(
    user_id: str = Form(...),
    title: str = Form(...),
    target_language: str = Form("zh-CN"),
    target_language_display: str | None = Form(None),
    video_duration_seconds: float = Form(0.0),
    video: UploadFile = File(...),
    db: Session = Depends(get_db)
):
    """
    需求：创建提交永远成功
    - 若达到排队上限：任务存为 FAILED（可在“无排队任务”时使用 /restart 重新入队）
    - 否则：正常入队 QUEUED
    """
    log.info(f"{user_id} create task: {title}")

    os.makedirs(os.path.join(MEDIA_ROOT, "videos"), exist_ok=True)
    save_path = os.path.join(MEDIA_ROOT, "videos", video.filename)
    with open(save_path, "wb") as f: shutil.copyfileobj(video.file, f)

    can_queue = _can_user_queue_now(db, user_id=user_id)
    status = "QUEUED" if can_queue else "FAILED"
    msg = None
    err = None
    enq_time = datetime.utcnow() if can_queue else None
    progress = 0

    if not can_queue:
        msg = (f"已创建，但当前排队已达上限 {MAX_QUEUED_PER_USER}。"
               f"请等待本用户排队任务清空后，在任务列表中点击『重新开始』再入队。")
        err = "排队上限已满，任务暂未入队；可稍后重新开始。"

    t = crud.create_task(
        db,
        user_id=user_id,
        title=title,
        target_language=target_language,
        target_language_display=target_language_display,
        video_file=os.path.relpath(save_path, MEDIA_ROOT),
        video_duration_seconds=video_duration_seconds,
        queued_for="prepare",
        status=status,
        progress=progress,
        enqueued_at=enq_time,
    )

    # 回写提示信息（仅当 soft-fail 时）
    if not can_queue:
        t.msg = msg
        t.error_msg = err
        db.add(t); db.flush(); db.refresh(t)

    return t

@router.post("/by-path", response_model=TaskOut)
def create_task_by_path(
    user_id: str = Form(...),
    video_path: str = Form(..., description="服务器本机上的视频绝对路径或相对路径"),
    title: str | None = Form(None),
    target_language: str = Form("zh-CN"),
    target_language_display: str | None = Form(None),
    video_duration_seconds: float = Form(0.0),
    db: Session = Depends(get_db),
):
    """
    与上传接口一致的行为：提交总成功；满额则 soft-fail。
    """
    log.info(f"{user_id} create task by-path: {title}")
    abs_src = os.path.abspath(video_path)
    if not os.path.exists(abs_src) or not os.path.isfile(abs_src):
        raise HTTPException(status_code=400, detail=f"视频不存在或不可读：{video_path}")

    os.makedirs(os.path.join(MEDIA_ROOT, "videos"), exist_ok=True)
    abs_media = os.path.abspath(MEDIA_ROOT)

    if os.path.commonpath([abs_src, abs_media]) == abs_media:
        save_rel_path = os.path.relpath(abs_src, abs_media)
    else:
        fname = os.path.basename(abs_src)
        dst = os.path.join(MEDIA_ROOT, "videos", fname)
        shutil.copy2(abs_src, dst)
        save_rel_path = os.path.relpath(dst, MEDIA_ROOT)

    task_title = title or os.path.splitext(os.path.basename(abs_src))[0]

    can_queue = _can_user_queue_now(db, user_id=user_id)
    status = "QUEUED" if can_queue else "FAILED"
    msg = None
    err = None
    enq_time = datetime.utcnow() if can_queue else None
    progress = 0

    if not can_queue:
        msg = (f"已创建，但当前排队已达上限 {MAX_QUEUED_PER_USER}。"
               f"请等待本用户排队任务清空后，在任务列表中点击『重新开始』再入队。")
        err = "排队上限已满，任务暂未入队；可稍后重新开始。"

    t = crud.create_task(
        db,
        user_id=user_id,
        title=task_title,
        target_language=target_language,
        target_language_display=target_language_display,
        video_duration_seconds=video_duration_seconds,
        video_file=save_rel_path,
        queued_for="prepare",
        status=status,
        progress=progress,
        enqueued_at=enq_time,
    )

    if not can_queue:
        t.msg = msg
        t.error_msg = err
        db.add(t); db.flush(); db.refresh(t)

    return t

@router.get("", response_model=list[TaskOut])
def list_tasks(user_id: str | None = None, db: Session = Depends(get_db)):
    log.info(f"{user_id} list tasks")
    return crud.list_tasks(db, user_id=user_id)

@router.get("/{task_id}", response_model=TaskDetail)
def get_task(task_id: int, db: Session = Depends(get_db)):
    log.info(f"get task: {task_id}")
    t = crud.get_task(db, task_id)
    if not t: raise HTTPException(404, "Task not found")
    return t

@router.post("/{task_id}/confirm", response_model=TaskOut)
def confirm(task_id:int, db: Session = Depends(get_db)):
    log.info(f"confirm task: {task_id}")
    t = crud.get_task(db, task_id); 
    if not t: raise HTTPException(404, "Task not found")
    # 入队前硬性校验（confirm 必须能排队）
    _ensure_user_queue_slot_or_409(db, t.user_id, exclude_task_id=t.id)
    t.status="QUEUED"; t.progress=max(40, t.progress or 40); t.queued_for="finalize"
    t.enqueued_at = datetime.utcnow() 
    db.add(t); return t

@router.post("/{task_id}/reburn", response_model=TaskOut)
def reburn(task_id:int, db: Session = Depends(get_db)):
    t = crud.get_task(db, task_id)
    if not t: raise HTTPException(404, "Task not found")
    _ensure_user_queue_slot_or_409(db, t.user_id, exclude_task_id=t.id)
    t.status="QUEUED"; t.progress=max(40,t.progress or 40); t.queued_for="reburn"
    t.enqueued_at = datetime.utcnow() 
    db.add(t); return t

@router.post("/{task_id}/restart", response_model=TaskOut)
def restart(task_id:int, db: Session = Depends(get_db)):
    """
    无排队任务时允许‘重新开始’：这里仍做入队前校验。
    若满额则 409，提示用户稍后重试。
    """
    log.info(f"restart task: {task_id}")
    t = crud.get_task(db, task_id)
    if not t: raise HTTPException(404, "Task not found")
    _ensure_user_queue_slot_or_409(db, t.user_id, exclude_task_id=t.id)
    # 清理中间产物（物理文件可选，这里不删）
    t.vocal_file=t.bg_video_file=t.tts_file=t.final_video_file=None
    t.status="QUEUED"; t.progress=0; t.queued_for="prepare"
    t.error_msg=""; t.msg=""; t.worker_id=None;t.lease_until=None
    t.heartbeat_at=None; t.processing_started_at=None
    t.enqueued_at = datetime.utcnow() 
    # 删除字幕
    t.subtitles.clear()
    db.add(t); return t

@router.post("/{task_id}/stop", response_model=TaskOut)
def stop(task_id: int, db: Session = Depends(get_db)):
    log.warning(f"stop task: {task_id}")
    t = crud.get_task(db, task_id)
    log.warning(f"[task#{task_id}] STOPPING, {t.status}")
    if not t:
        raise HTTPException(404, "Task not found")
    if t.status not in ("PROCESSING", "QUEUED"):
        raise HTTPException(400, "任务不在运行或排队中")
    try:
        request_stop(task_id)
    except Exception as _: pass
    prev_status = t.status or ""
    phase = (t.queued_for or "").strip()  # prepare | finalize | reburn
    now = datetime.utcnow()
    now_str = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    for attr in ("worker_id", "lease_until", "heartbeat_at", "processing_started_at"):
        if hasattr(t, attr):
            setattr(t, attr, None)
    t.enqueued_at = None
    back_msg = ""
    if phase == "finalize":
        t.status = "REVIEW"
        t.progress = max(40, t.progress or 40)
        back_msg = "已回退到『待确认』，可再次检查字幕后重新生成。"
        t.msg = f"已停止：已回退到待确认（{now_str}）。"
    elif phase == "reburn":
        if t.final_video_file:
            t.status = "SUCCESS"
            t.progress = 100
            back_msg = "已回到『处理成功』，保留上一次生成的最终视频。"
            t.msg = f"已停止：已回到成功状态（{now_str}）。"
        else:
            t.status = "REVIEW"
            t.progress = max(40, t.progress or 40)
            back_msg = "未检测到历史成品，已回退到『待确认』。"
            t.msg = f"已停止：已回退到待确认（{now_str}）。"
    else:
        t.status = "FAILED"
        t.msg = f"已停止：阶段一处理已中止（{now_str}）。"
        back_msg = "阶段一已停止并标记为失败。"
    prefix = "用户手动停止任务"
    tail = f"（原状态={prev_status}，阶段={phase or 'unknown'}，时间={now_str}）"
    t.error_msg = f"{prefix}{tail}。{back_msg}".strip()
    log.info(t.msg);log.info(t.error_msg)
    db.add(t); db.flush(); db.refresh(t)
    return t

@router.delete("/{task_id}")
def delete_task(task_id:int, db: Session = Depends(get_db)):
    log.info(f"delete task: {task_id}")
    t = crud.get_task(db, task_id)
    if not t: raise HTTPException(404, "Task not found")
    if t.status in ("PROCESSING","QUEUED"):
        raise HTTPException(400, "任务进行中或排队中，请先停止")
    def _rm(relpath: str|None):
        try:
            if relpath:
                abspath = os.path.join(MEDIA_ROOT, relpath)
                if os.path.exists(abspath):
                    os.remove(abspath)
        except Exception:
            pass
    _rm(t.video_file); _rm(t.vocal_file); _rm(t.bg_video_file); _rm(t.tts_file); _rm(t.final_video_file)
    db.delete(t)
    return {"ok": True}

@router.get("/{task_id}/progress", response_model=ProgressOut)
def progress(task_id:int, db: Session = Depends(get_db)):
    t = crud.get_task(db, task_id)
    if not t: raise HTTPException(404, "Task not found")
    proc_secs = 0
    if t.status == "PROCESSING" and t.processing_started_at:
        proc_secs = int((datetime.utcnow() - t.processing_started_at).total_seconds())
    state = "PENDING" if t.status=="QUEUED" else ("SUCCESS" if t.status=="SUCCESS" else ("FAILED" if t.status=="FAILED" else "PROCESSING"))
    status_text = t.msg or ("排队中..." if t.status=="QUEUED" else "处理中..." )
    qp = ql = None
    running = crud.count_processing(db)
    if t.status == "QUEUED":
        pos, total = crud.queue_position_and_length(db, t)
        qp, ql = pos, total
        status_text = f"排队中（第 {pos} 位 / 共 {total} 个，运行中 {running}）"
    elif t.status == "FAILED" and t.error_msg:
        status_text = t.error_msg
    return {
        "state": state, "progress": t.progress or 0, "status": status_text, "processing_seconds": proc_secs,
        "task_status": t.status, "final_video_file": t.final_video_file or "",
        "queue_position": qp, "queue_length": ql, "running_workers": running, "max_parallel": int(os.getenv("MAX_PARALLEL_TASKS","1"))
    }
