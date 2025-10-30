from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, Field

class SubtitleOut(BaseModel):
    id: int
    sequence: int
    start_time: float
    end_time: float
    start_time_srt: str
    end_time_srt: str
    original_text: str
    translated_text: Optional[str] = None
    class Config: from_attributes = True

class TaskCreate(BaseModel):
    user_id: str
    title: str
    target_language: str = "zh-CN"

class TaskOut(BaseModel):
    id: int
    user_id: str
    title: str
    status: str
    progress: int
    queued_for: str
    target_language: str
    target_language_display: Optional[str] = None
    final_video_file: Optional[str] = None
    tts_file: Optional[str] = None
    bg_video_file: Optional[str] = None
    video_duration_seconds: Optional[float] = None

    subtitle_format: str = "ass"
    burn_subtitle: bool = True
    sub_font_name: str = "Noto Sans CJK SC"
    sub_font_size: int = 20
    sub_font_bold: bool = True
    sub_font_italic: bool = False
    sub_font_underline: bool = False
    sub_font_color: str = "#B9F3B9"
    sub_outline_color: str = "#000000"
    sub_back_color: str = "#000000"
    sub_outline_width: float = 1.0
    sub_back_opacity: float = 1.0
    sub_alignment: int = 2
    bgm_volume: float = 1.0
    tts_volume: float = 1.0

    tts_gender: str = "female"
    tts_voice: str = "zh-f-001"
    tts_name: Optional[str] = None

    created_at: datetime
    updated_at: Optional[datetime] = None

    class Config: 
        from_attributes = True


class TaskDetail(TaskOut):
    subtitles: List[SubtitleOut] = []

class ProgressOut(BaseModel):
    state: str
    progress: int
    status: str
    task_status: str
    final_video_file: str = ""
    queue_position: int | None = None
    queue_length: int | None = None
    running_workers: int | None = None
    max_parallel: int | None = None
