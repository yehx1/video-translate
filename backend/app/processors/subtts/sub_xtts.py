# app/processors/subtts/sub_xtts.py
# -*- coding: utf-8 -*-
import os
import sys
import time
import pysrt
import queue
import tempfile
import multiprocessing as mp
from dataclasses import dataclass
from typing import List, Tuple, Optional

from app.cancel import is_stop_requested
from app.processors.utils import (
    killable_run,
    killable_check_output,
)
from app.logs import get_logger
log = get_logger(__name__)

# -------- XTTS 基本配置 --------
MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
SAMPLE_RATE = 48000
BASE_SPEED = 1.00         # 我们用时长对齐，不直接调速度
RESOLVE_MODE = "shift"    # "shift" | "compress"
SAFE_GAP = 0.02
MIN_DUR = 0.10

# -------- 小工具 --------
def run_cmd(cmd: List[str], task_id: Optional[int] = None) -> str:
    return killable_check_output(cmd, task_id=task_id)

def ffprobe_duration(path: str, task_id: Optional[int] = None) -> float:
    cmd = ["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",path]
    out = run_cmd(cmd, task_id=task_id).strip()
    return float(out) if out else 0.0

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

# -------- Worker 进程实现：常驻加载 XTTS 模型 --------
@dataclass
class SynthReq:
    text: str
    out_path: str
    language: str
    speaker_wav: Optional[str]

@dataclass
class SynthResp:
    ok: bool
    err: str

def _xtts_worker_main(req_q: mp.Queue, resp_q: mp.Queue):
    """
    子进程：常驻加载 XTTS；串行处理合成请求。
    说明：
      - 子进程里导入 TTS.api，避免父进程 GPU/线程状态污染；
      - 每条执行 tts_to_file；出错把错误文本写回；
      - 不做 stop 判断，父进程如需立即停止可直接 terminate 本进程。
    """
    try:
        # 惰性导入，保证父进程不持有大模型资源
        from TTS.api import TTS  # noqa
        tts = TTS(MODEL_NAME, gpu=True, progress_bar=False)
        while True:
            item = req_q.get()  # 阻塞，父进程会 terminate
            if item is None:
                resp_q.put(SynthResp(True, ""))  # 作为优雅退出的 ack
                break
            assert isinstance(item, SynthReq)
            os.makedirs(os.path.dirname(item.out_path), exist_ok=True)
            try:
                tts.tts_to_file(
                    text=item.text,
                    file_path=item.out_path,
                    speed=BASE_SPEED,
                    language=item.language,
                    speaker_wav=item.speaker_wav,
                )
                resp_q.put(SynthResp(True, ""))
            except Exception as e:
                resp_q.put(SynthResp(False, f"{type(e).__name__}: {e}"))
    except Exception as e:
        # 若模型加载失败或进程级异常，通知父进程
        try:
            resp_q.put(SynthResp(False, f"WorkerInitError: {type(e).__name__}: {e}"))
        except Exception:
            pass
        # 直接退出
        sys.exit(1)

class XTTSWorker:
    def __init__(self):
        ctx = mp.get_context("spawn")  # 更稳健；如在 Linux 也可用 "fork"
        self.req_q: mp.Queue = ctx.Queue(maxsize=4)
        self.resp_q: mp.Queue = ctx.Queue(maxsize=4)
        self.p: mp.Process = ctx.Process(target=_xtts_worker_main, args=(self.req_q, self.resp_q), daemon=True)
        self.p.start()

    def synth(self, text: str, out_path: str, language: str, speaker_wav: Optional[str], timeout: float | None) -> Tuple[bool, str]:
        self.req_q.put(SynthReq(text, out_path, language, speaker_wav))
        try:
            resp: SynthResp = self.resp_q.get(timeout=timeout)  # 等待单条返回
            return resp.ok, resp.err
        except queue.Empty:
            return False, "Timeout"

    def close(self, graceful: bool = True):
        try:
            if graceful and self.p.is_alive():
                self.req_q.put(None)  # 请求优雅退出
                try:
                    self.resp_q.get(timeout=2.0)
                except Exception:
                    pass
        except Exception:
            pass
        finally:
            if self.p.is_alive():
                try:
                    self.p.terminate()
                except Exception:
                    pass
                try:
                    self.p.join(timeout=2.0)
                except Exception:
                    pass

# -------- 对外主流程（与原函数签名一致） --------
def srt_to_tts(
    srt_path: str,
    ref_wav_path: Optional[str],
    out_path: str,
    language: str = "en",
    resolve_mode: str = RESOLVE_MODE,
    task_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    逐条字幕合成：
      - XTTS 在**程序内部**的常驻子进程中只加载一次；
      - 父进程之间隔每条都可检查 stop，若已请求停止，直接终止子进程实现**立即停止**；
      - 单条正在 tts_to_file 时若收到 stop，也直接 terminate 子进程实现即时打断；
      - 输出做时长对齐、前置静音、amix 汇总（48k）。
    """
    try:
        if not os.path.exists(srt_path):
            return False, f"[错误] 找不到字幕文件：{srt_path}"
        if ref_wav_path is not None and not os.path.exists(ref_wav_path):
            return False, f"[错误] 找不到参考音色：{ref_wav_path}"

        subs = pysrt.open(srt_path, encoding="utf-8")
        raw_items: List[Tuple[float, float, str]] = [
            (s.start.ordinal/1000.0, s.end.ordinal/1000.0, s.text) for s in subs
        ]
        schedule = build_non_overlapping_timeline(raw_items, resolve_mode, SAFE_GAP)

        # ---- 启动常驻 XTTS worker（程序内加载模型）----
        worker = XTTSWorker()
        tmpdir = tempfile.mkdtemp(prefix="srt_tts_")
        segments: List[str] = []
        # 单条合成最长等待（防卡死），可视需要调大
        per_item_timeout = 300.0

        try:
            for i, (adj_start, adj_end, text) in enumerate(schedule, 1):
                log.info(f"[XTTS] 处理 {i}/{len(schedule)}: {text}")

                # 进入前检查 stop
                if task_id is not None and is_stop_requested(task_id):
                    raise RuntimeError("Cancelled")

                target_dur = max(MIN_DUR, adj_end - adj_start)
                raw = os.path.join(tmpdir, f"raw_{i:04d}.wav")
                fit = os.path.join(tmpdir, f"fit_{i:04d}.wav")
                pad = os.path.join(tmpdir, f"pad_{i:04d}.wav")

                # --- 合成（仍为阻塞操作，但在子进程里；可通过 terminate 立停） ---
                ok, err = worker.synth(text=text, out_path=raw, language=language, speaker_wav=ref_wav_path, timeout=per_item_timeout)
                if not ok:
                    # 如果是初始化失败或进程退出，尝试**重启一次**并重试当前条
                    if "WorkerInitError" in (err or "") or err == "Timeout" or not worker.p.is_alive():
                        log.warning(f"[XTTS] worker 异常（{err}），尝试重启一次...")
                        worker.close(graceful=False)
                        worker = XTTSWorker()
                        ok2, err2 = worker.synth(text=text, out_path=raw, language=language, speaker_wav=ref_wav_path, timeout=per_item_timeout)
                        if not ok2:
                            raise RuntimeError(f"XTTS 合成失败：{err2 or err}")
                    else:
                        raise RuntimeError(f"XTTS 合成失败：{err}")

                # 合成后也检查是否被停止
                if task_id is not None and is_stop_requested(task_id):
                    raise RuntimeError("Cancelled")

                # 输出校验（偶发空文件）
                dur = 0.0
                try:
                    dur = ffprobe_duration(raw, task_id=task_id)
                except Exception:
                    pass
                if dur <= 0:
                    # 再给一次机会：重试当前条
                    log.warning(f"[XTTS] 输出无效或损坏，重试一次：{raw}")
                    ok3, err3 = worker.synth(text=text, out_path=raw, language=language, speaker_wav=ref_wav_path, timeout=per_item_timeout)
                    if not ok3:
                        raise RuntimeError(f"XTTS 输出无效或损坏：{raw}; 重试失败：{err3}")
                    dur = ffprobe_duration(raw, task_id=task_id)
                    if dur <= 0:
                        raise RuntimeError(f"XTTS 输出无效或损坏：{raw}")

                # --- 对齐长度 + 前置静音 ---
                time_stretch_to(raw, target_sec=target_dur, out_wav=fit, task_id=task_id)
                pad_to_start(fit, adj_start, pad, sr=SAMPLE_RATE, task_id=task_id)
                segments.append(pad)

            if not segments:
                return False, "[错误] 字幕为空，未生成任何音频。"

            # --- 汇总混音 ---
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
            # 先关 worker（如 stop 请求直接 terminate）
            try:
                if task_id is not None and is_stop_requested(task_id):
                    worker.close(graceful=False)
                else:
                    worker.close(graceful=True)
            except Exception:
                pass
            # 清理临时目录
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
