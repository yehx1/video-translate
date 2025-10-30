# frontend/subtitle_processor/seed_data.py
# -*- coding: utf-8 -*-
from __future__ import annotations
import os, json
from typing import Dict, List, Tuple, Any

try:
    # 若可用，读取 Django settings，便于定位 STATIC 目录
    from django.conf import settings
except Exception:
    settings = None  # 迁移时也能兜底

# 目标语言显示（保持不变）
DEFAULT_LANGUAGES: List[Tuple[str, str]] = [
    ("zh-CN","中文（简体）"),
    ("en",   "英语"),
    ("ja",   "日语"),
    ("ko",   "韩语"),
    ("fr",   "法语"),
    ("de",   "德语"),
]

# —— 旧的硬编码 VOICE_BANK（作为兜底）——
_FALLBACK_VOICE_BANK: Dict[str, List[Dict[str, Any]]] = {
    "zh-CN": [
        {"code":"auto",     "tts_name":"auto", "name":"视频原声·声音克隆", "gender":"auto",   "sample":""},
        {"code":"zh-f-001", "tts_name":"zh-CN-XiaochenMultilingualNeural", "name":"女声·清新A", "gender":"female", "sample":"/static/tts_samples/zh-f-001.mp3"},
        {"code":"zh-f-002", "tts_name":"zh-CN-XiaochenNeural",            "name":"女声·亲和B", "gender":"female", "sample":"/static/tts_samples/zh-f-002.mp3"},
        {"code":"zh-f-003", "tts_name":"zh-CN-XiaohanNeural",             "name":"女声·亲和C", "gender":"female", "sample":"/static/tts_samples/zh-f-003.mp3"},
        {"code":"zh-m-001", "tts_name":"zh-CN-YunfengNeural",             "name":"男声·稳重A", "gender":"male",   "sample":"/static/tts_samples/zh-m-001.mp3"},
        {"code":"zh-m-002", "tts_name":"zh-CN-YunhaoNeural",              "name":"男声·磁性B", "gender":"male",   "sample":"/static/tts_samples/zh-m-002.mp3"},
    ]
}

# —— lang 规范化：把 tts_map.json 的 "lang" 映射到我们的语言代码 —— #
_LANG_NORM = {
    "zh": "zh-CN",
    "zh-cn": "zh-CN",
    "en": "en",
    "ja": "ja",
    "jp": "ja",
    "ko": "ko",
    "kr": "ko",
    "fr": "fr",
    "de": "de",
}

def _normalize_lang(lang: str) -> str:
    if not lang:
        return lang
    key = str(lang).strip().lower()
    return _LANG_NORM.get(key, lang)

def _guess_display_name(code: str, gender: str, zhname: str, mark: str) -> str:
    """
    为 VoiceProfile.name 生成一个友好的展示名。
    优先使用 gender + mark 的首个短语；没有则回退到 code。
    """
    g = (gender or "").lower()
    g_disp = "女声" if g == "female" else ("男声" if g == "male" else "原声/克隆")
    tag = (mark or zhname).strip()
    if tag:
        return f"{g_disp}·{tag}"
    return code

def _resolve_tts_map_path() -> str | None:
    """
    寻找 static/assets/tts_map.json 的可能位置：
    - 优先 settings.BASE_DIR/static/assets/tts_map.json
    - 其次 settings.STATIC_ROOT 或 STATICFILES_DIRS
    - 再次 当前工作目录/static/assets/tts_map.json
    """
    candidates: List[str] = []

    # 1) BASE_DIR/static/assets/tts_map.json
    try:
        if settings and getattr(settings, "BASE_DIR", None):
            candidates.append(os.path.join(str(settings.BASE_DIR), "static", "assets", "tts_map.json"))
    except Exception:
        pass

    # 2) STATICFILES_DIRS（开发场景更常用）
    try:
        if settings and getattr(settings, "STATICFILES_DIRS", None):
            for d in settings.STATICFILES_DIRS:
                candidates.append(os.path.join(str(d), "assets", "tts_map.json"))
    except Exception:
        pass

    # 3) STATIC_ROOT（collectstatic 后）
    try:
        if settings and getattr(settings, "STATIC_ROOT", None):
            candidates.append(os.path.join(str(settings.STATIC_ROOT), "assets", "tts_map.json"))
    except Exception:
        pass

    # 4) CWD 兜底
    candidates.append(os.path.join(os.getcwd(), "static", "assets", "tts_map.json"))

    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None

def _load_tts_map() -> Dict[str, Any] | None:
    """
    读取 JSON；失败返回 None
    """
    path = _resolve_tts_map_path()
    if not path:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        return None
    return None

def _group_voices_by_lang(tts_map: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    """
    把 { code -> {...} } 的结构转换为 { lang_code -> [ {code, tts_name, name, gender, sample} ] }
    同时为每个语言自动添加一个 "auto"（视频原声/声音克隆）选项。
    """
    grouped: Dict[str, List[Dict[str, Any]]] = {}

    # 先把所有项按 lang 聚合
    for code, info in tts_map.items():
        if not isinstance(info, dict):
            continue
        lang_raw = info.get("lang") or ""
        lang_code = _normalize_lang(lang_raw)
        if not lang_code:
            continue

        tts_name = info.get("voice") or code
        gender = (info.get("gender") or "auto").lower()
        zhname = info.get("zhname") or ""
        enname = info.get("enname") or ""
        mark = info.get("mark") or ""
        name = _guess_display_name(code, gender, zhname, mark)
        enname = f"{gender}·{enname}"
        # 示例音频的约定路径（可按需更改你的静态资源组织）
        sample = f"/static/tts_samples/{code}.mp3"

        grouped.setdefault(lang_code, []).append({
            "code": code,
            "tts_name": tts_name,
            "name": name,
            "enname": enname,
            "gender": gender,
            "sample": sample,
        })

    # 为已出现的语言补充 "auto" 选项（置顶）
    for lang_code, voices in grouped.items():
        has_auto = any(v["code"] == "auto" for v in voices)
        if not has_auto:
            voices.insert(0, {
                "code": "auto",
                "tts_name": "auto",
                "name": "视频原声·声音克隆",
                "gender": "auto",
                "sample": "",
            })
    return grouped

def build_default_voice_bank_from_file() -> Dict[str, List[Dict[str, Any]]] | None:
    data = _load_tts_map()
    if not data:
        return None
    return _group_voices_by_lang(data)

def get_default_voice_bank() -> Dict[str, List[Dict[str, Any]]]:
    """
    对外主入口：优先读 tts_map.json，失败时回退 _FALLBACK_VOICE_BANK
    """
    loaded = build_default_voice_bank_from_file()
    return loaded or _FALLBACK_VOICE_BANK

# 兼容原有导入路径：仍然暴露一个 DEFAULT_VOICE_BANK，但其内容来自文件（或回退）
DEFAULT_VOICE_BANK = get_default_voice_bank()
