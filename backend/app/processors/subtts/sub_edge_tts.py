# subtts/sub_edge_tts.py
# -*- coding: utf-8 -*-
import os
import sys
import json
import pysrt
import tempfile
import threading
import asyncio
from typing import List, Tuple, Optional

import edge_tts

from app.cancel import is_stop_requested
from app.processors.utils import (
    killable_run,
    killable_check_output,
)

from app.logs import get_logger
log = get_logger(__name__)

SAMPLE_RATE = 48000
BASE_SPEED = 1.00
PAUSE_COMMA_MS = 250
PAUSE_PERIOD_MS = 500

RESOLVE_MODE = "shift"
SAFE_GAP = 0.02
MIN_DUR = 0.10

def run_cmd(cmd: List[str], task_id: Optional[int] = None) -> str:
    return killable_check_output(cmd, task_id=task_id)


def ffprobe_duration(path: str, task_id: Optional[int] = None) -> float:
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
    return float(run_cmd(cmd, task_id=task_id))


def atempo_chain_ratio(ratio: float) -> List[float]:
    steps: List[float] = []
    cur = 1.0
    while abs(cur - ratio) > 1e-4:
        step = max(0.5, min(2.0, ratio / cur))
        if 0.9999 < step < 1.0001:
            break
        steps.append(step)
        cur *= step
    return steps


def time_stretch_to(in_wav: str, target_sec: float, out_wav: str, task_id: Optional[int] = None) -> None:
    try:
        d = ffprobe_duration(in_wav, task_id=task_id)
    except Exception:
        killable_run(["ffmpeg", "-y", "-i", in_wav, out_wav], task_id=task_id, check=True)
        return
    if d <= 0 or target_sec <= 0 or d < target_sec:
        killable_run(["ffmpeg", "-y", "-i", in_wav, out_wav], task_id=task_id, check=True)
        return
    steps = atempo_chain_ratio(d / target_sec)
    filt = ",".join([f"atempo={s}" for s in steps]) if steps else "anull"
    killable_run(["ffmpeg", "-y", "-i", in_wav, "-filter:a", filt, out_wav], task_id=task_id, check=True)

def pad_to_start(in_wav: str, start_sec: float, out_wav: str, sr: int = SAMPLE_RATE, task_id: Optional[int] = None) -> None:
    if start_sec <= 0:
        killable_run(["ffmpeg", "-y", "-i", in_wav, out_wav], task_id=task_id, check=True)
        return
    tmp_sil = out_wav + ".sil.wav"
    killable_run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"anullsrc=r={sr}:cl=mono", "-t", f"{start_sec:.3f}", tmp_sil], task_id=task_id, check=True)
    killable_run(["ffmpeg", "-y", "-i", tmp_sil, "-i", in_wav, "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1", out_wav], task_id=task_id, check=True)
    try:
        os.remove(tmp_sil)
    except Exception:
        pass


def build_non_overlapping_timeline(
    items: List[Tuple[float, float, str]],
    mode: str = RESOLVE_MODE,
    safe_gap: float = SAFE_GAP,
) -> List[Tuple[float, float, str]]:
    result: List[Tuple[float, float, str]] = []
    prev_end = 0.0
    for start, end, text in items:
        start = max(0.0, float(start))
        end = max(start + MIN_DUR, float(end))
        orig_dur = end - start
        if mode == "shift":
            adj_start = max(start, prev_end + safe_gap)
            adj_end = adj_start + max(MIN_DUR, orig_dur)
        elif mode == "compress":
            adj_start = max(start, prev_end + safe_gap)
            adj_end = max(adj_start + MIN_DUR, min(end, adj_start + orig_dur))
        else:
            raise ValueError("RESOLVE_MODE 只能为 'shift' 或 'compress'")
        result.append((adj_start, adj_end, text))
        prev_end = adj_end
    return result


def _speed_to_rate(speed: float) -> str:
    pct = int(round((speed - 1.0) * 100))
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct}%"


async def _edge_tts_save_async(text: str, out_path: str, voice: str, rate: str) -> None:
    communicate = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await communicate.save(out_path)


def edge_tts_to_file(text: str, out_path: str, voice: str, rate: str, task_id: Optional[int] = None) -> None:
    # 在进入合成前检查一次
    if task_id is not None and is_stop_requested(task_id):
        raise RuntimeError("Cancelled")
    try:
        asyncio.get_running_loop()
        def _runner():
            asyncio.run(_edge_tts_save_async(text, out_path, voice, rate))
        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join()
    except RuntimeError:
        asyncio.run(_edge_tts_save_async(text, out_path, voice, rate))
    # 合成结束后再检查一次（尽快上抛取消）
    if task_id is not None and is_stop_requested(task_id):
        raise RuntimeError("Cancelled")


def srt_to_tts(
    srt_path: str,
    tts_name: str,
    out_path: str,
    language: str = "en",
    resolve_mode: str = RESOLVE_MODE,
    task_id: Optional[int] = None,  # <--- 新增
) -> Tuple[bool, str]:
    try:
        log.info(f"开始处理字幕文件edge：{srt_path}")
        if not os.path.exists(srt_path):
            return False, f"[错误] 找不到字幕文件：{srt_path}"

        subs = pysrt.open(srt_path, encoding="utf-8")
        raw_items: List[Tuple[float, float, str]] = [(s.start.ordinal/1000.0, s.end.ordinal/1000.0, s.text) for s in subs]
        schedule = build_non_overlapping_timeline(raw_items, resolve_mode, SAFE_GAP)

        rate = _speed_to_rate(BASE_SPEED)

        tmpdir = tempfile.mkdtemp(prefix="srt_tts_")
        segments: List[str] = []

        try:
            for i, (adj_start, adj_end, text) in enumerate(schedule, 1):
                log.info(f"字幕处理：{i}/{len(schedule)} {text}")
                if task_id is not None and is_stop_requested(task_id):
                    raise RuntimeError("Cancelled")
                target_dur = max(MIN_DUR, adj_end - adj_start)
                raw = os.path.join(tmpdir, f"raw_{i:04d}.wav")
                fit = os.path.join(tmpdir, f"fit_{i:04d}.wav")
                pad = os.path.join(tmpdir, f"pad_{i:04d}.wav")

                edge_tts_to_file(text=text, out_path=raw, voice=tts_name, rate=rate, task_id=task_id)

                if task_id is not None and is_stop_requested(task_id):
                    raise RuntimeError("Cancelled")

                time_stretch_to(raw, target_dur, fit, task_id=task_id)
                pad_to_start(fit, adj_start, pad, sr=SAMPLE_RATE, task_id=task_id)
                segments.append(pad)

            if not segments:
                return False, "[错误] 字幕为空，未生成任何音频。"

            cmd = ["ffmpeg", "-y"]
            for p in segments:
                cmd += ["-i", p]
            fc = "".join([f"[{i}:a]" for i in range(len(segments))]) + f"amix=inputs={len(segments)}:normalize=0[a]"
            cmd += ["-filter_complex", fc, "-map", "[a]", "-ar", str(SAMPLE_RATE), out_path]
            killable_run(cmd, task_id=task_id, check=True)

            try:
                total = ffprobe_duration(out_path, task_id=task_id)
                return True, f"[INFO] 生成成功：{out_path}（时长 {total:.3f}s）"
            except Exception:
                return True, f"[INFO] 生成成功：{out_path}"
        finally:
            try:
                for fn in os.listdir(tmpdir):
                    try:
                        os.remove(os.path.join(tmpdir, fn))
                    except Exception:
                        pass
                os.rmdir(tmpdir)
            except Exception:
                pass

    except RuntimeError as e:
        if str(e) == "Cancelled":
            return False, "[已停止] 任务被取消。"
        return False, f"[异常] {type(e).__name__}: {e}"
    except Exception as e:
        return False, f"[异常] {type(e).__name__}: {e}"
