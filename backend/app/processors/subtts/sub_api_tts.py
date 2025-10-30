# subtts/sub_api_tts.py
# -*- coding: utf-8 -*-
"""
统一的 SRT -> TTS 调用入口

用法（代码）：
    from subtts.sub_api_tts import srt_to_tts
    ok, msg = srt_to_tts(
        srt_path="path/to/input.srt",
        out_path="output/out.wav",
        language="en",
        engine="auto",             # "auto" | "edge-tts" | "xtts"
        ref_wav_path="ref.wav",    # 仅在 xtts 时有用，可为 None
        resolve_mode=None          # "shift"|"compress"，None 则走各后端默认
    )

用法（命令行）：
    python -m subtts.sub_api_tts \
        --srt ../output0.srt \
        --out output/api_out.wav \
        --lang en \
        --engine auto \
        --ref ../ref.wav \
        --mode shift
"""
import os
from typing import Optional, Tuple, Literal
from app.logs import get_logger
log = get_logger(__name__)

# 获取当前文件路径
CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))


_edge_mod = None
_xtts_mod = None
_edge_import_err = None
_xtts_import_err = None

try:
    from . import sub_edge_tts as _edge

    _edge_mod = _edge
except Exception as e:
    _edge_import_err = e
try:
    from . import sub_xtts as _xtts

    _xtts_mod = _xtts
except Exception as e:
    _xtts_import_err = e


EngineLiteral = Literal["auto", "edge-tts", "xtts"]

__all__ = ["srt_to_tts"]

DEFAULT_ENGINE: EngineLiteral = "auto"
DEFAULT_LANGUAGE = "en"
DEFAULT_MODE = "shift"  # 与两个后端默认一致


def _pick_engine(
    engine: EngineLiteral, has_edge: bool, has_xtts: bool, ref_wav: Optional[str]
) -> str:
    """
    根据入参和可用性选择后端：
    - engine = "edge-tts"/"xtts"：强制使用对应后端
    - engine = "auto"：
        若提供了 ref_wav 且 xtts 可用 -> 优先 xtts
        否则若 edge 可用 -> 用 edge
        否则若 xtts 可用 -> 用 xtts
        否则报错
    """
    if engine == "edge-tts":
        return "edge-tts"
    if engine == "xtts":
        return "xtts"
    # auto
    if ref_wav and has_xtts:
        return "xtts"
    if has_edge:
        return "edge-tts"
    if has_xtts:
        return "xtts"
    return "none"

def srt_to_tts(
    srt_path: str,
    out_path: str,
    language: str = DEFAULT_LANGUAGE,
    engine: EngineLiteral = DEFAULT_ENGINE,
    refp_or_tname: Optional[str] = None,
    resolve_mode: Optional[str] = None,
    voiceid: Optional[str] = None,
    task_id: Optional[int] = None,
) -> Tuple[bool, str]:
    """
    规则：
      - voiceid == "auto": 只使用 XTTS；若 XTTS 失败/不可用，直接返回错误。
      - voiceid != "auto": 先使用 edge-tts；只要失败/异常/不可用，再回退 XTTS
                           （XTTS 使用固定 sample 路径作为参考音色）。

    返回 (ok, msg)
    """
    log.info(f"[srt_to_tts] voiceid={voiceid} lang={language} refp={refp_or_tname}")
    has_edge = _edge_mod is not None
    has_xtts = _xtts_mod is not None
    log.info(f"has_edge={has_edge} has_xtts={has_xtts}")
    edge_mode = resolve_mode if resolve_mode else (getattr(_edge_mod, "RESOLVE_MODE", DEFAULT_MODE) if has_edge else DEFAULT_MODE)
    xtts_mode = resolve_mode if resolve_mode else (getattr(_xtts_mod, "RESOLVE_MODE", DEFAULT_MODE) if has_xtts else DEFAULT_MODE)

    # -------- 情况 A：voiceid == "auto" -> 只用 XTTS --------
    if voiceid == "auto":
        if not has_xtts:
            reason = (
                f"xtts 不可用：{type(_xtts_import_err).__name__}: {_xtts_import_err}"
                if _xtts_import_err
                else "xtts 模块未加载"
            )
            return False, f"[错误] 需要 XTTS，但 XTTS 不可用。{reason}"
        try:
            return _xtts_mod.srt_to_tts(
                srt_path=srt_path,
                ref_wav_path=refp_or_tname,  # 外部可传入或为 None
                out_path=out_path,
                language=language,
                resolve_mode=xtts_mode,
                task_id=task_id, 
            )
        except Exception as e:
            return False, f"[错误] XTTS 合成失败：{type(e).__name__}: {e}"

    # -------- 情况 B：voiceid != "auto" -> 先 edge，失败再 XTTS --------
    # 为 XTTS 构造参考音色（edge-tts 不需要）
    xtts_ref = (
        f"{CURRENT_DIR}/../../../../frontend/static/tts_samples/zh-CN/{voiceid}.mp3"
    )

    edge_err: Optional[str] = None
    log.info("[srt_to_tts] 使用 edge-tts 进行转换...")
    # 1) 先试 edge-tts
    if has_edge:
        try:
            ok, msg = _edge_mod.srt_to_tts(
                srt_path=srt_path,
                tts_name=refp_or_tname,
                out_path=out_path,
                language=language,
                resolve_mode=edge_mode,
                task_id=task_id, 
            )
            log.info(f"edge tts DONE -> {ok} {msg}")
            if ok:
                return True, msg
            edge_err = f"edge-tts 返回失败：{msg}"
        except Exception as e:
            edge_err = f"edge-tts 异常：{type(e).__name__}: {e}"
    else:
        edge_err = "edge-tts 不可用"

    if "已停止" in edge_err:
        raise RuntimeError("Cancelled")
    log.warning(f"[srt_to_tts] edge-tts 未成功，原因：{edge_err}，转 XTTS ...")

    # 2) 回退 XTTS
    if not has_xtts:
        reason = (
            f"xtts 不可用：{type(_xtts_import_err).__name__}: {_xtts_import_err}"
            if _xtts_import_err
            else "xtts 模块未加载"
        )
        return False, f"[错误] {edge_err}；且 XTTS 不可用：{reason}"

    try:
        ok2, msg2 = _xtts_mod.srt_to_tts(
            srt_path=srt_path,
            ref_wav_path=xtts_ref,
            out_path=out_path,
            language=language,
            resolve_mode=xtts_mode,
        )
        if ok2:
            return True, f"[edge-tts 未成功，已回退至 XTTS] {edge_err}"
        return False, f"[错误] {edge_err}；XTTS 返回失败：{msg2}"
    except Exception as e2:
        return False, f"[错误] {edge_err}；XTTS 异常：{type(e2).__name__}: {e2}"


# ------------------ 命令行入口（可选） ------------------
def _main():
    import argparse

    p = argparse.ArgumentParser(description="Unified SRT->TTS API")
    p.add_argument("--srt", required=True, help="输入 SRT 文件路径")
    p.add_argument("--out", required=True, help="输出 WAV 文件路径")
    p.add_argument(
        "--lang", default=DEFAULT_LANGUAGE, help="语言代码，如 en / zh-CN / ja 等"
    )
    p.add_argument(
        "--engine",
        default=DEFAULT_ENGINE,
        choices=["auto", "edge-tts", "xtts"],
        help="选择后端：auto | edge-tts | xtts（默认 auto）",
    )
    p.add_argument("--ref", default=None, help="XTTS 参考音色 wav（可选）")
    p.add_argument(
        "--mode",
        default=None,
        choices=["shift", "compress"],
        help="重叠消解策略（默认各后端的 RESOLVE_MODE）",
    )
    args = p.parse_args()

    ok, msg = srt_to_tts(
        srt_path=args.srt,
        out_path=args.out,
        language=args.lang,
        engine=args.engine,  # type: ignore
        refp_or_tname=args.ref,
        resolve_mode=args.mode,
    )
    log.info(f"XTTS DONE -> {ok} {msg}")

if __name__ == "__main__":
    _main()
