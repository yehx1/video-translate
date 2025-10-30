# app/processors/utils.py
# -*- coding: utf-8 -*-
import os, re, time
import shutil, signal
from pathlib import Path
from subprocess import Popen, PIPE
from faster_whisper import WhisperModel
from app.cancel import is_stop_requested, register_process, unregister_process

from app.logs import get_logger
log = get_logger(__name__)

# 获取所在目录
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
MEDIA_ROOT = os.getenv("MEDIA_ROOT", os.path.abspath("./media"))
MODEL_PATH = os.getenv("WHISPER_MODEL_PATH", os.path.join(CURRENT_DIR, "../../../models/Systran/faster-whisper-large-v2"))
FRONTEND_MEDIA_ROOT = os.getenv("FRONTEND_MEDIA_ROOT", os.path.join(CURRENT_DIR, "../../../frontend/media")).strip()

class ValidationError(Exception):
    pass

def ensure_dir(p: str):
    os.makedirs(os.path.dirname(p), exist_ok=True)

def format_srt_timestamp(seconds: float) -> str:
    ms = max(0, int(round(seconds * 1000)))
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    ms %= 1000
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

# —— 通用解析：SRT 或 秒 —— #
def parse_time_to_seconds(v: str) -> float:
    """
    支持：
        - HH:MM:SS,mmm / HH:MM:SS.mmm
        - 纯秒数（整数或小数），例如 12 或 12.345
    """
    _re_srt = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})([.,](\d{1,3}))?$")
    v = (str(v) or "").strip()
    if not v:
        raise ValidationError("时间不能为空。")
    m = _re_srt.match(v)
    if m:
        h = int(m.group(1))
        mnt = int(m.group(2))
        s = int(m.group(3))
        ms = int((m.group(5) or "0").ljust(3, "0")[:3])  # 补足到毫秒3位
        return h * 3600 + mnt * 60 + s + ms / 1000.0
    # 纯秒
    try:
        sec = float(v)
        if sec < 0:
            raise ValidationError("时间不能为负数。")
        return sec
    except Exception:
        raise ValidationError(
            "时间格式不正确，请填写 SRT 时间（如 00:01:23,456）或秒数（如 83.456）。"
        )

# ---------------- 可取消子进程执行器 ----------------
def _start_popen(cmd: list[str]):
    """
    以**新进程组**启动子进程，便于整组 kill（Linux/macOS）。
    """
    kwargs = {}
    try:
        # 仅类 Unix 支持
        kwargs["preexec_fn"] = os.setsid  # type: ignore
    except Exception:
        pass
    return Popen(cmd, stdout=PIPE, stderr=PIPE, text=True, **kwargs)

def killable_run(cmd: list[str], task_id: int | None = None, check: bool = True) -> int:
    """
    可被 /stop 立即中断的子进程执行器（非阻塞读取版）。
    - 轮询进程状态；不在循环里 read()，避免阻塞；
    - 退出后再一次性 communicate() 取回输出；
    - 如 stop，则整组 TERM→KILL 并抛 RuntimeError("Cancelled")
    """
    p = _start_popen(cmd)
    if task_id is not None:
        register_process(task_id, p)
    try:
        while True:
            rc = p.poll()
            if rc is not None:
                # 进程已结束，再一次性取出输出，避免管道残留
                out, err = p.communicate()
                if check and rc != 0:
                    err_tail = (err or "")[-2000:]
                    raise RuntimeError(f"Command failed ({rc}): {' '.join(cmd)}\n{err_tail}")
                return rc

            if task_id is not None and is_stop_requested(task_id):
                try:
                    pgid = os.getpgid(p.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    pass
                time.sleep(0.3)
                try:
                    pgid = os.getpgid(p.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    pass
                raise RuntimeError("Cancelled")

            time.sleep(0.2)
    finally:
        if task_id is not None:
            unregister_process(task_id, p)


def killable_check_output(cmd: list[str], task_id: int | None = None) -> str:
    """
    非阻塞读取版：循环中不读管道；结束后统一 communicate()。
    """
    p = _start_popen(cmd)
    if task_id is not None:
        register_process(task_id, p)
    try:
        while True:
            rc = p.poll()
            if rc is not None:
                out, err = p.communicate()
                if rc != 0:
                    raise RuntimeError(f"Command failed ({rc}): {' '.join(cmd)}\n{(err or '')[-2000:]}")
                return out or ""

            if task_id is not None and is_stop_requested(task_id):
                try:
                    pgid = os.getpgid(p.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except Exception:
                    pass
                time.sleep(0.3)
                try:
                    pgid = os.getpgid(p.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except Exception:
                    pass
                raise RuntimeError("Cancelled")

            time.sleep(0.2)
    finally:
        if task_id is not None:
            unregister_process(task_id, p)
def get_media_duration_seconds(path: str, task_id: int | None = None) -> float:
    out = killable_check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
        task_id=task_id,
    )
    return float(out.strip())


def publish_to_frontend_media(local_abs_path: str, rel_subdir: str = "final_videos") -> str | None:
    """
    将后端本地生成的文件复制到“前端 MEDIA_ROOT/rel_subdir/文件名”。
    - 返回前端侧的相对路径（相对 FRONTEND_MEDIA_ROOT），形如 "final_videos/xxx.mp4"
    - 若未配置 FRONTEND_MEDIA_ROOT，则返回 None（走后端直出方案）
    - 采用 .part + os.replace 原子替换，避免被前端读到半文件
    """
    log.info(f"publish_to_frontend_media: {local_abs_path}")
    root = FRONTEND_MEDIA_ROOT
    if not root:
        return None
    os.makedirs(os.path.join(root, rel_subdir), exist_ok=True)
    fname = os.path.basename(local_abs_path)
    dst_dir = os.path.join(root, rel_subdir)
    dst = os.path.join(dst_dir, fname)
    tmp = dst + ".part"
    shutil.copy2(local_abs_path, tmp)     # 先复制到临时文件
    os.replace(tmp, dst)                  # 原子替换
    return os.path.relpath(dst, root)


def extract_audio_from_video(video_path: str, audio_path: str, task_id: int | None = None):
    ensure_dir(audio_path)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-v", "error", "-i", video_path, "-vn", "-ac", "2", "-ar", "48000", "-acodec", "pcm_s16le", audio_path]
    killable_run(cmd, task_id=task_id, check=True)


def separate_vocals_and_bgm(audio_path: str, vocal_save_path: str, bgm_save_path: str, task_id: int | None = None):
    # 与原逻辑一致：demucs two-stems
    cmd = ["demucs", "--name", "htdemucs", "--two-stems", "vocals", audio_path]
    killable_run(cmd, task_id=task_id, check=True)

    audio_filename = Path(audio_path).stem
    base_dir = Path("separated") / "htdemucs" / audio_filename
    v_path, nv_path = base_dir / "vocals.wav", base_dir / "no_vocals.wav"
    if not v_path.exists() or not nv_path.exists():
        raise FileNotFoundError("Demucs输出不存在")

    ensure_dir(vocal_save_path)
    ensure_dir(bgm_save_path)
    # 使用 Python 复制，避免再起子进程
    shutil.copy2(str(v_path), vocal_save_path)
    shutil.copy2(str(nv_path), bgm_save_path)
    # 清理 demucs 目录
    shutil.rmtree(base_dir.parent, ignore_errors=True)


def mux_video_with_audio(video_in: str, audio_in: str, video_out: str, task_id: int | None = None):
    ensure_dir(video_out)
    cmd = [
        "ffmpeg", "-y",
        "-hide_banner", "-nostats", "-v", "error",
        "-i", video_in,
        "-i", audio_in,
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        "-shortest",
        video_out,
    ]
    killable_run(cmd, task_id=task_id, check=True)


def transcribe_vocal_to_subtitles(vocal_path: str, model_path: str | None = None, task_id: int | None = None) -> list:
    """
    语音识别（支持在迭代过程中检测 stop 请求，尽快返回已识别片段）
    """
    model = WhisperModel(model_path or MODEL_PATH, device="auto", compute_type="float16")
    segments, _ = model.transcribe(vocal_path, beam_size=5, vad_filter=True, word_timestamps=False)
    out = []
    for i, seg in enumerate(segments, 1):
        # 片段粒度的 stop 检查
        if task_id is not None and is_stop_requested(task_id):
            break
        txt = (seg.text or "").strip()
        if not txt:
            continue
        out.append({"sequence": i, "start_time": seg.start, "end_time": seg.end, "original_text": txt})
    return out


def make_final_video(
    bg_video_path: str,
    tts_wav_path: str,
    subtitle_path: str,
    out_video_path: str,
    burn_subtitle: bool = True,
    bgm_volume: float = 1.0,
    tts_volume: float = 1.0,
    task_id: int | None = None,
):
    ensure_dir(out_video_path)

    def has_audio(p: str) -> bool:
        try:
            out = killable_check_output(
                ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=index", "-of", "csv=p=0", p],
                task_id=task_id,
            )
            return out.strip() != ""
        except Exception:
            return False

    bg_has_audio = has_audio(bg_video_path)

    def esc(p: str) -> str:
        return p.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")

    sub_filter = f"subtitles='{esc(os.path.abspath(subtitle_path))}'"

    if bg_has_audio:
        audio_chain = f"[0:a]volume={bgm_volume}[a0];[1:a]volume={tts_volume}[a1];[a0][a1]amix=inputs=2:duration=shortest:dropout_transition=2[aout]"
    else:
        audio_chain = f"[1:a]volume={tts_volume}[aout]"

    if burn_subtitle or subtitle_path.lower().endswith(".ass"):
        v_chain = f"[0:v]{sub_filter}[vout]"
        fc = f"{v_chain};{audio_chain}"
        cmd = [
            "ffmpeg", "-y",
            "-hide_banner", "-nostats", "-v", "error",
            "-i", bg_video_path,
            "-i", tts_wav_path,
            "-filter_complex", fc,
            "-map", "[vout]",
            "-map", "[aout]",
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "veryfast",
            "-c:a", "aac",
            "-b:a", "192k",
            "-shortest",
            out_video_path,
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-hide_banner", "-nostats", "-v", "error",
            "-i", bg_video_path,
            "-i", tts_wav_path,
            "-i", subtitle_path,
            "-filter_complex", audio_chain,
            "-map", "0:v:0",
            "-map", "[aout]",
            "-map", "2:0",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "192k",
            "-c:s", "mov_text",
            "-metadata:s:s:0", "language=zho",
            "-shortest",
            out_video_path,
        ]

    killable_run(cmd, task_id=task_id, check=True)


def _norm_hex_color_to_ass(c: str, alpha_0_255: int = 0) -> str:
    c = (c or "").strip()
    if c.startswith("&H"):
        body = c[2:]
        if len(body) == 6:
            return f"&H{alpha_0_255:02X}{body.upper()}"
        elif len(body) == 8:
            return f"&H{alpha_0_255:02X}{body[2:].upper()}"
        else:
            return f"&H{alpha_0_255:02X}FFFFFF"
    if c.startswith("#"):
        c = c[1:]
    if len(c) != 6:
        c = "FFFFFF"
    rr, gg, bb = c[0:2], c[2:4], c[4:6]
    return f"&H{alpha_0_255:02X}{bb.upper()}{gg.upper()}{rr.upper()}"


def dump_subtitles_to_ass(
    subtitles: list,  # 这里用 Python list[Subtitle]（SQLAlchemy 模型对象）
    ass_path: str,
    title: str = "Subtitles",
    font_name: str = "Noto Sans CJK SC",
    font_size: int = 36,
    font_bold: bool = True,
    font_italic: bool = False,
    font_underline: bool = False,
    font_color: str = "#FFFFFF",
    outline_color: str = "#000000",
    back_color: str = "#000000",
    outline_width: float = 0.0,
    back_opacity: float = 0.5,
    alignment: int = 2,
    margin_v: int = 10,
):
    back_alpha = int(round(max(0.0, min(1.0, back_opacity)) * 255))
    font_alpha = 0
    primary_colour = _norm_hex_color_to_ass(font_color, font_alpha)
    back_colour = _norm_hex_color_to_ass(back_color, back_alpha)
    outline_colour = _norm_hex_color_to_ass(outline_color, 0)
    bold_flag = -1 if font_bold else 0
    italic_flag = -1 if font_italic else 0
    underline_flag = -1 if font_underline else 0
    outline_px = max(0.0, min(10.0, float(outline_width)))

    header = f"""[Script Info]
Title: {title}
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.601

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: BoxBG,{font_name},{font_size},&HFFFFFFFF,&H00FFFFFF,{back_colour},&H00FFFFFF,{bold_flag},{italic_flag},{underline_flag},0,100,100,0,0,3,{outline_px},0,{alignment},10,10,{margin_v},1
Style: Stroke,{font_name},{font_size},{primary_colour},&H00FFFFFF,{outline_colour},{back_colour},{bold_flag},{italic_flag},{underline_flag},0,100,100,0,0,1,{outline_px},0,{alignment},10,10,{margin_v},1
[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def to_ass_time(sec: float) -> str:
        if sec < 0:
            sec = 0
        h = int(sec // 3600)
        m = int((sec % 3600) // 60)
        s = sec % 60
        return f"{h:d}:{m:02d}:{s:06.3f}".replace(".", ",")

    lines = [header]
    for sub in subtitles:
        start = to_ass_time(float(sub.start_time))
        end = to_ass_time(float(sub.end_time))
        text = ((sub.translated_text or sub.original_text or "").replace("\n", r"\N"))
        lines.append(f"Dialogue: 0,{start},{end},BoxBG,,0,0,{margin_v},,{{\\q2}}{text}")
        lines.append(f"Dialogue: 1,{start},{end},Stroke,,0,0,{margin_v},,{{\\q2}}{text}")

    ensure_dir(ass_path)
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
