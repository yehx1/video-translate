from typing import Union
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from ..db import get_db
from ..models import Task, Subtitle
from ..processors.utils import parse_time_to_seconds, format_srt_timestamp

router = APIRouter(prefix="/api/subtitles", tags=["subtitles"])

class SubtitlePatch(BaseModel):
    start_time: Union[float, str]
    end_time: Union[float, str]
    translated_text: str

@router.patch("/{task_id}/{subtitle_id}")
def edit_subtitle(task_id:int, subtitle_id:int, body:SubtitlePatch, db: Session = Depends(get_db)):
    sub = db.get(Subtitle, subtitle_id)
    if not sub or sub.task_id != task_id: raise HTTPException(404, "Subtitle not found")
    st = parse_time_to_seconds(body.start_time)
    et = parse_time_to_seconds(body.end_time)
    if et <= st: raise HTTPException(400, "结束时间必须大于开始时间")
    sub.translated_text = body.translated_text
    sub.start_time = st; sub.end_time = et; 
    sub.start_time_srt = format_srt_timestamp(st); sub.end_time_srt = format_srt_timestamp(et)
    db.add(sub); return {"ok": True}
