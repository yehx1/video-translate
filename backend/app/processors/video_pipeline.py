# app/processors/video_pipeline.py
# -*- coding: utf-8 -*-
import os
import json
import tempfile
from datetime import datetime
from typing import List, Dict, Any, Optional

from sqlalchemy.orm import Session

from .utils import (                      # 这些函数内部应已接入 killable_run 等
    MEDIA_ROOT,
    get_media_duration_seconds,
    extract_audio_from_video,
    separate_vocals_and_bgm,
    mux_video_with_audio,
    transcribe_vocal_to_subtitles,
    dump_subtitles_to_ass,
    make_final_video,
    publish_to_frontend_media,
    format_srt_timestamp,
)
from app.logs import get_logger
from ..models import Task, Subtitle
from .srt_translate import translate_srt
from .subtts.sub_api_tts import srt_to_tts
from app.cancel import is_stop_requested

log = get_logger(__name__)

MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "300"))
MAX_PARALLEL_TASKS = int(os.getenv("MAX_PARALLEL_TASKS", "1"))


# ------------------------ 内部小工具 ------------------------ #
def _map_target_lang(lang: str) -> str:
    # 兼容 LLM/XTTS 的语言码差异
    return "zh" if lang == "zh-CN" else lang


def _cancel_if_needed(db: Session, task: Task, msg: str = "任务已被取消") -> bool:
    """
    若外部已请求停止，则把任务置为 CANCELLED 并返回 True。
    调用方据此应立刻 return 结束流程。
    """
    if is_stop_requested(task.id):
        # task.status = "CANCELLED"
        task.msg = msg
        db.add(task)
        db.commit()
        log.info(f"[task#{task.id}] CANCELLED: {msg}")
        return True
    return False


def _advance(db: Session, task: Task, progress: int, msg: str) -> bool:
    """
    原子更新进度 + 检查停止。若检测到取消，会同时写回取消状态并返回 True。
    """
    task.progress = max(progress, task.progress or progress)
    task.msg = msg
    db.add(task)
    db.commit()
    return _cancel_if_needed(db, task, msg=msg)


def _fail(db: Session, task: Task, err_msg: str, status: str) -> None:
    task.status = status
    task.error_msg = err_msg
    task.msg = err_msg
    db.add(task)
    db.commit()
    log.error(f"[task#{task.id}] FAILED: {err_msg}")


def _succeed(db: Session, task: Task, msg: str = "处理完成") -> None:
    task.status = "SUCCESS"
    task.progress = 100
    task.msg = msg
    db.add(task)
    db.commit()
    log.info(f"[task#{task.id}] SUCCESS: {msg}")


# ------------------------ 阶段一：准备 ------------------------ #
def process_prepare(db: Session, task: Task):
    """
    阶段一：提取→分离→无声视频→ASR→翻译→入库，并把任务置为 REVIEW
    整个阶段均支持取消。
    """
    try:
        task.status = "PROCESSING"
        task.progress = max(5, task.progress or 5)
        task.msg = "开始处理"
        db.add(task); db.commit(); db.flush()

        if _cancel_if_needed(db, task, "已停止：开始处理阶段"):
            return

        video_path = os.path.join(MEDIA_ROOT, task.video_file)

        # —— 读时长（保护：超限直接失败）—— #
        if task.video_duration_seconds < 0.1:
            if _advance(db, task, 8, "读取视频信息..."):  # + stop check
                return
            duration = get_media_duration_seconds(video_path, task_id=task.id)
            task.video_duration_seconds = duration
            if duration > MAX_VIDEO_SECONDS:
                _fail(db, task, f"视频过长：{duration:.1f}s（限制≤{MAX_VIDEO_SECONDS}s）", "FAILED")
                return
            db.add(task); db.commit()

        with tempfile.TemporaryDirectory() as tmp:
            # 1) 抽音
            if _advance(db, task, 10, "音频提取中..."):
                return
            audio_path = os.path.join(tmp, "audio.wav")
            extract_audio_from_video(video_path, audio_path, task_id=task.id)

            # 2) 分离人声/伴奏
            if _advance(db, task, 15, "人声分离中..."):
                return
            vocal_path = os.path.join(
                MEDIA_ROOT, "vocals", f"vocal_{task.id}_{task.created_at:%Y%m%d_%H%M%S}.wav"
            )
            bgm_path = os.path.join(
                MEDIA_ROOT, "bgm", f"bgm_{task.id}_{task.created_at:%Y%m%d_%H%M%S}.wav"
            )
            os.makedirs(os.path.dirname(vocal_path), exist_ok=True)
            os.makedirs(os.path.dirname(bgm_path), exist_ok=True)
            separate_vocals_and_bgm(audio_path, vocal_path, bgm_path, task_id=task.id)
            task.vocal_file = os.path.relpath(vocal_path, MEDIA_ROOT)
            db.add(task); db.commit()

            # 3) 生成去人声视频
            if _advance(db, task, 20, "背景视频合成中..."):
                return
            bg_video = os.path.join(
                MEDIA_ROOT, "videos_novocals", f"video_novocals_{task.id}_{task.created_at:%Y%m%d_%H%M%S}.mp4"
            )
            os.makedirs(os.path.dirname(bg_video), exist_ok=True)
            mux_video_with_audio(video_path, bgm_path, bg_video, task_id=task.id)
            task.bg_video_file = os.path.relpath(bg_video, MEDIA_ROOT)
            db.add(task); db.commit()

            # 4) ASR
            if _advance(db, task, 25, "音频识别中..."):
                return
            segs = transcribe_vocal_to_subtitles(vocal_path, task_id=task.id)

            # 清理旧字幕并入库新字幕
            task.subtitles.clear()
            db.flush()
            for s in segs:
                st = float(s["start_time"]); et = float(s["end_time"])
                task.subtitles.append(Subtitle(
                    sequence=s["sequence"],
                    start_time=st, end_time=et,
                    start_time_srt=format_srt_timestamp(st),
                    end_time_srt=format_srt_timestamp(et),
                    original_text=s["original_text"]
                ))
            db.flush()

            # 5) 翻译（支持取消）
            if _advance(db, task, 30, "字幕翻译中..."):
                return
            subs_for_llm = [
                {
                    "index": int(s["sequence"]),
                    "start_ordinal": int(round(s["start_time"] * 1000)),
                    "end_ordinal": int(round(s["end_time"] * 1000)),
                    "text": s["original_text"],
                }
                for s in segs
            ]

            # translate_srt 内部一般较快，不额外传 task_id；在调用前后做取消检测即可
            if _cancel_if_needed(db, task, "已停止：翻译前"):
                return
            mapping, ok, msg = translate_srt(
                subs_for_llm,
                target_lang=_map_target_lang(task.target_language),
                cps=15.0,
                exclude_spaces=False,
                batch_size=20,
                max_shift=1.0,
                min_gap=0.10,
                no_compress_pass=False,
            )
            if _cancel_if_needed(db, task, "已停止：翻译后"):
                return

            if not ok:
                _fail(db, task, msg or "字幕翻译失败", "FAILED")
                return

            for sub in task.subtitles:
                sub.translated_text = mapping.get(int(sub.sequence), sub.original_text)

            # —— 进入 REVIEW —— #
            task.status = "REVIEW"
            task.progress = 60
            task.msg = "翻译完成，等待人工确认"
            db.add(task); db.commit()
            log.info(f"[task#{task.id}] 进入 REVIEW")

    except Exception as e:
        if str(e) == "Cancelled":
            log.info(f"[task#{task.id}] 已停止：{str(e)[:80]}")
        else:
            _fail(db, task, f"阶段一异常：{type(e).__name__}: {e}", "FAILED")


# ------------------------ 阶段二：最终合成 ------------------------ #
def process_finalize(db: Session, task: Task):
    """
    阶段二：确认后 → TTS → 合成最终视频
    - 始终生成 SRT 喂给 TTS；
    - 根据 task.subtitle_format 决定最终视频使用 ASS（硬烧）还是 SRT（可选硬烧/软封装）；
    - 整个阶段支持取消。
    """
    try:
        if task.status not in ("REVIEW", "PROCESSING", "SUCCESS"):
            log.warning(f"[task#{task.id}] 状态不允许进入最终合成：{task.status}")
            # _fail(db, task, "状态不允许进入最终合成", task.status)
            return

        log.info(f"process_finalize #{task.id} from status={task.status}")

        task.status = "PROCESSING"
        task.progress = 70
        task.msg = "语音合成中..."
        task.translation_confirmed_at = datetime.utcnow()
        db.add(task); db.commit()

        if _cancel_if_needed(db, task, "已停止：准备合成"):
            return

        with tempfile.TemporaryDirectory() as tmp:
            # —— 输出 SRT（TTS 输入）—— #
            srt_dir = os.path.join(MEDIA_ROOT, "srts")
            os.makedirs(srt_dir, exist_ok=True)
            srt_path = os.path.join(srt_dir, f"subs_{task.id}_{task.created_at:%Y%m%d_%H%M%S}.srt")
            with open(srt_path, "w", encoding="utf-8") as f:
                for sub in sorted(task.subtitles, key=lambda x: x.sequence):
                    f.write(f"{sub.sequence}\n")
                    f.write(f"{format_srt_timestamp(sub.start_time)} --> {format_srt_timestamp(sub.end_time)}\n")
                    f.write(f"{(sub.translated_text or sub.original_text or '').strip()}\n\n")

            if _cancel_if_needed(db, task, "已停止：写入SRT后"):
                return

            # —— 如需 ASS，再额外生成用于最终视频烧录 —— #
            use_ass = (task.subtitle_format or "ass").lower() == "ass"
            if use_ass:
                ass_dir = os.path.join(MEDIA_ROOT, "ass")
                os.makedirs(ass_dir, exist_ok=True)
                ass_path = os.path.join(ass_dir, f"subs_{task.id}_{task.created_at:%Y%m%d_%H%M%S}.ass")
                dump_subtitles_to_ass(
                    subtitles=sorted(task.subtitles, key=lambda x: x.sequence),
                    ass_path=ass_path,
                    title=task.title,
                    font_name=task.sub_font_name,
                    font_size=task.sub_font_size,
                    font_bold=task.sub_font_bold,
                    font_italic=task.sub_font_italic,
                    font_underline=task.sub_font_underline,
                    font_color=task.sub_font_color,
                    outline_color=task.sub_outline_color,
                    back_color=task.sub_back_color,
                    outline_width=task.sub_outline_width,
                    back_opacity=task.sub_back_opacity,
                    alignment=task.sub_alignment or 2,
                    margin_v=10,
                )
                subtitle_for_video = ass_path
            else:
                subtitle_for_video = srt_path

            if _cancel_if_needed(db, task, "已停止：字幕准备就绪"):
                return

            # —— TTS —— #
            tts_dir = os.path.join(MEDIA_ROOT, "tts")
            os.makedirs(tts_dir, exist_ok=True)
            tts_out = os.path.join(tts_dir, f"tts_{task.id}_{task.created_at:%Y%m%d_%H%M%S}.wav")

            refp_or_tname = (
                os.path.join(MEDIA_ROOT, task.vocal_file)
                if (task.tts_voice == "auto" and task.vocal_file)
                else None
            )
            if task.tts_voice != "auto":
                refp_or_tname = task.tts_name

            if _advance(db, task, 75, "TTS 合成中..."):
                return

            ok, msg = srt_to_tts(
                srt_path=srt_path,
                out_path=tts_out,
                language=task.target_language.replace("zh-CN", "zh-cn"),
                engine="auto",
                refp_or_tname=refp_or_tname,
                resolve_mode=None,
                voiceid=(task.tts_voice or None),
                task_id=task.id,  # ★ 透传，内部支持取消
            )
            if not ok:
                if is_stop_requested(task.id):
                    # TTS 内部已因取消中断
                    task.status = "REVIEW"
                    task.msg = "已停止：TTS 合成中"
                    db.add(task); db.commit()
                    return
                _fail(db, task, f"TTS 失败：{msg}", "REVIEW")
                return

            task.tts_file = os.path.relpath(tts_out, MEDIA_ROOT)
            db.add(task); db.commit()
            log.info(f"[task#{task.id}] TTS done -> {task.tts_file}")

            if _cancel_if_needed(db, task, "已停止：TTS 完成后"):
                return

            # —— 合成最终视频 —— #
            if _advance(db, task, 90, "最终视频合成中..."):
                return

            if not task.bg_video_file:
                _fail(db, task, "缺少无声视频", "REVIEW")
                return

            final_dir = os.path.join(MEDIA_ROOT, "final_videos")
            os.makedirs(final_dir, exist_ok=True)
            final_out = os.path.join(final_dir, f"final_{task.id}_{task.created_at:%Y%m%d_%H%M%S}.mp4")

            make_final_video(
                bg_video_path=os.path.join(MEDIA_ROOT, task.bg_video_file),
                tts_wav_path=tts_out,
                subtitle_path=subtitle_for_video,
                out_video_path=final_out,
                burn_subtitle=(task.burn_subtitle or use_ass),
                bgm_volume=task.bgm_volume,
                tts_volume=task.tts_volume,
                task_id=task.id,  # ★ 透传，内部 ffmpeg 可被杀
            )

            if _cancel_if_needed(db, task, "已停止：最终视频生成后"):
                return

            task.final_video_file = os.path.relpath(final_out, MEDIA_ROOT)
            log.info(f"[task#{task.id}] Final video -> {task.final_video_file}")

            # 前端可访问路径
            rel_front = publish_to_frontend_media(final_out, "final_videos")
            log.info(f"[task#{task.id}] Published -> {rel_front}")

            _succeed(db, task, "处理完成")

    except Exception as e:
        if str(e) == "Cancelled":
            log.info(f"[task#{task.id}] 已停止：{str(e)[:80]}")
        else:
            _fail(db, task, f"阶段二异常：{type(e).__name__}: {e}", "REVIEW")


# ------------------------ 仅重烧 ------------------------ #
def process_reburn(db: Session, task: Task):
    """
    仅重新烧录（复用 bg_video + tts）
    - 根据 task.subtitle_format 生成所需字幕（ASS/SRT）；
    - 复用已存在的 TTS 音轨；
    - 支持取消。
    """
    try:
        if task.status not in ("SUCCESS", "REVIEW", "PROCESSING"):
            _fail(db, task, "当前状态不可仅重烧", "SUCCESS")
            return
        if not task.bg_video_file or not task.tts_file:
            _fail(db, task, "缺少去人声视频或已有TTS", "SUCCESS")
            return

        task.status = "PROCESSING"
        task.progress = 90
        task.msg = "字幕重新合成中..."
        db.add(task); db.commit()

        if _cancel_if_needed(db, task, "已停止：准备重烧"):
            return

        with tempfile.TemporaryDirectory() as tmp:
            # 生成字幕文件（ASS 或 SRT）
            use_ass = (task.subtitle_format or "ass").lower() == "ass"
            if use_ass:
                ass_tmp = os.path.join(tmp, "reburn.ass")
                dump_subtitles_to_ass(
                    subtitles=sorted(task.subtitles, key=lambda x: x.sequence),
                    ass_path=ass_tmp,
                    title=task.title,
                    font_name=task.sub_font_name,
                    font_size=task.sub_font_size,
                    font_bold=task.sub_font_bold,
                    font_italic=task.sub_font_italic,
                    font_underline=task.sub_font_underline,
                    font_color=task.sub_font_color,
                    outline_color=task.sub_outline_color,
                    back_color=task.sub_back_color,
                    outline_width=task.sub_outline_width,
                    back_opacity=task.sub_back_opacity,
                    alignment=task.sub_alignment or 2,
                    margin_v=10,
                )
                sub_path = ass_tmp
            else:
                srt_tmp = os.path.join(tmp, "reburn.srt")
                with open(srt_tmp, "w", encoding="utf-8") as f:
                    for sub in sorted(task.subtitles, key=lambda x: x.sequence):
                        f.write(f"{sub.sequence}\n")
                        f.write(f"{format_srt_timestamp(sub.start_time)} --> {format_srt_timestamp(sub.end_time)}\n")
                        f.write(f"{(sub.translated_text or sub.original_text or '').strip()}\n\n")
                sub_path = srt_tmp

            if _cancel_if_needed(db, task, "已停止：重烧字幕准备就绪"):
                return

            # 输出路径
            final_dir = os.path.join(MEDIA_ROOT, "final_videos")
            os.makedirs(final_dir, exist_ok=True)
            final_out = os.path.join(final_dir, f"final_reburn_{task.id}_{datetime.utcnow():%Y%m%d_%H%M%S}.mp4")

            # 合成（复用已有 TTS）
            make_final_video(
                bg_video_path=os.path.join(MEDIA_ROOT, task.bg_video_file),
                tts_wav_path=os.path.join(MEDIA_ROOT, task.tts_file),
                subtitle_path=sub_path,
                out_video_path=final_out,
                burn_subtitle=(task.burn_subtitle or use_ass),
                bgm_volume=task.bgm_volume,
                tts_volume=task.tts_volume,
                task_id=task.id,  # ★ 透传
            )

            if _cancel_if_needed(db, task, "已停止：重烧视频生成后"):
                return

            task.final_video_file = os.path.relpath(final_out, MEDIA_ROOT)
            rel_front = publish_to_frontend_media(final_out, "final_videos")
            _succeed(db, task, "处理完成")

    except Exception as e:
        if str(e) == "Cancelled":
            log.info(f"[task#{task.id}] 已停止：{str(e)[:80]}")
        else:
            _fail(db, task, f"仅重烧异常：{type(e).__name__}: {e}", "SUCCESS")
