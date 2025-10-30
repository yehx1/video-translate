from sqlalchemy import Column, Integer, String, Float, Text, DateTime, Boolean, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime, timedelta
from .db import Base

TASK_STATUS = ("QUEUED","PROCESSING","REVIEW","SUCCESS","FAILED")
QUEUE_FOR   = ("prepare","finalize","reburn")

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(128), nullable=False, index=True)
    # queue
    queued_for = Column(String(16), default="prepare")
    status = Column(String(20), default="QUEUED", index=True)
    progress = Column(Integer, default=0)
    msg = Column(Text, nullable=True)
    error_msg = Column(Text, nullable=True)

    # business
    title = Column(String(255), nullable=False)
    video_file = Column(String(512), nullable=False)
    vocal_file = Column(String(512), nullable=True)
    bg_video_file = Column(String(512), nullable=True)
    tts_file = Column(String(512), nullable=True)
    final_video_file = Column(String(512), nullable=True)
    target_language = Column(String(16), default="zh-CN")
    target_language_display = Column(String(64), nullable=True)
    video_duration_seconds = Column(Float, nullable=True)

    # style / tts
    subtitle_format = Column(String(3), default="ass")
    burn_subtitle = Column(Boolean, default=True)
    sub_font_name = Column(String(64), default="Noto Sans CJK SC")
    sub_font_size = Column(Integer, default=20)
    sub_font_bold = Column(Boolean, default=True)
    sub_font_italic = Column(Boolean, default=False)
    sub_font_underline = Column(Boolean, default=False)
    sub_font_color = Column(String(16), default="#B9F3B9")
    sub_outline_color = Column(String(16), default="#000000")
    sub_back_color = Column(String(16), default="#000000")
    sub_outline_width = Column(Float, default=1.0)
    sub_back_opacity = Column(Float, default=1.0)
    sub_alignment = Column(Integer, default=2)
    bgm_volume = Column(Float, default=1.0)
    tts_volume = Column(Float, default=1.0)
    tts_gender = Column(String(6), default="female")
    tts_voice = Column(String(64), default="zh-f-001")
    tts_name = Column(String(128), default="zh-CN-YunfengNeural")

    translation_confirmed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    enqueued_at = Column(DateTime, nullable=True, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    subtitles = relationship("Subtitle", back_populates="task", cascade="all, delete-orphan", lazy="selectin")

    # --- 运行控制：租约 / 心跳 / 重试 ---
    worker_id    = Column(String(64), nullable=True, index=True)        # 哪个worker持有（如 host:pid）
    lease_until  = Column(DateTime, nullable=True, index=True)          # 占用截止时间（租约）
    heartbeat_at = Column(DateTime, nullable=True, index=True)          # 最近心跳
    processing_started_at = Column(DateTime, nullable=True, index=True) # 进入 PROCESSING 的时间，只统计处理阶段
    attempt      = Column(Integer, default=0)                           # 已尝试次数
    max_attempts = Column(Integer, default=0)                           # 最多自动重试次数

class Subtitle(Base):
    __tablename__ = "subtitles"
    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"))
    sequence = Column(Integer, nullable=False)
    start_time = Column(Float, nullable=False)
    end_time = Column(Float, nullable=False)
    start_time_srt = Column(String(12), nullable=False)  # "HH:MM:SS,mmm"
    end_time_srt   = Column(String(12), nullable=False)  # "HH:MM:SS,mmm"
    original_text = Column(Text, nullable=False)
    translated_text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    task = relationship("Task", back_populates="subtitles")
