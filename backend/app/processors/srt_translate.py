#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import math
import argparse
import requests
import pysrt
from tqdm import tqdm

# ============ 从 上上级目录/.env 读取 OPENAI 配置 ============
ENV_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"))
print(f"Loading .env from {ENV_PATH}...")
try:
    from dotenv import load_dotenv  # pip install python-dotenv
    # 不覆盖已有环境变量；只从特定路径加载
    load_dotenv(ENV_PATH, override=False)
except Exception:
    # 如果未安装或加载失败，不影响后续流程（但无法从 .env 自动加载）
    pass

# --------- 基础工具 ---------
def read_srt(path):
    subs_raw = pysrt.open(path, encoding="utf-8")
    subs = []
    for i in range(len(subs_raw)):
        sub = subs_raw[i]
        temp = {}
        temp["index"] = sub.index
        temp["start_ordinal"] = sub.start.ordinal
        temp["end_ordinal"] = sub.end.ordinal
        temp["text"] = sub.text
        subs.append(temp)
    return subs


def duration_seconds(sub):
    return max(0.0, (sub["end_ordinal"] - sub["start_ordinal"]) / 1000.0)


def visible_len(s, exclude_spaces=False):
    if exclude_spaces:
        return sum(1 for ch in s if not ch.isspace())
    return len(s)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def add_seconds_to_time(srt_time, seconds):
    ms = srt_time.ordinal + int(round(seconds * 1000))
    if ms < 0:
        ms = 0
    return pysrt.SubRipTime(milliseconds=ms)


# --------- LLM 接口（仅 OPENAI 兼容 Chat Completions） ---------
def call_chat_api(messages, model, api_base, api_key, temperature=0.0, max_tokens=2048):
    url = api_base.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json; charset=utf-8",
    }
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=120)
    if resp.status_code != 200:
        raise RuntimeError(f"Chat API error {resp.status_code}: {resp.text[:2000]}")
    data = resp.json()
    try:
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        raise RuntimeError(f"Unexpected API response: {data}") from e


# --------- 批量翻译（带每条最大字数约束） ---------
BATCH_SYS_PROMPT = """\
You are a professional subtitle translator. Translate EACH item independently into the target language.

Hard rules (must all be satisfied):
- Each id is self-contained. NEVER merge, borrow, anticipate, or repeat content from previous/next ids.
- If an item is a fragment or mid-sentence, translate ONLY that fragment as-is. Do not complete the sentence.
- Preserve line breaks per item (use '\\n' where shown).
- Do NOT output timings, only the translated text per id.
- Respect MAX_CHARS for each id; shorten politely if needed.
- Return strictly in JSON with id->text mapping. No extra commentary.
"""


def build_batch_prompt(items, target_lang):
    lines = []
    lines.append(f"Target language: {target_lang}")
    lines.append("For each item, obey MAX_CHARS characters (inclusive).")
    lines.append("Items:")
    for it in items:
        lines.append(
            f"- id={it['id']}, MAX_CHARS={it['max_chars']}\n<<<\n{it['text']}\n>>>"
        )
    lines.append('Output JSON like:\n{\n  "1": "...",\n  "2": "..."\n}')
    return "\n".join(lines)


def parse_json_mapping(txt):
    import json, re
    m = re.search(r"\{[\s\S]*\}", txt)
    if m:
        txt = m.group(0)
    return json.loads(txt)


def batch_translate(
    subs, start_idx, end_idx, cps, target_lang, model, api_base, api_key, exclude_spaces
):
    items = []
    for i in range(start_idx, end_idx):
        sub = subs[i]
        dur = max(0.1, duration_seconds(sub))
        max_chars = int(math.floor(dur * cps))
        text = sub["text"].replace("\r\n", "\n")
        items.append({"id": sub["index"], "max_chars": max_chars, "text": text})

    user_prompt = build_batch_prompt(items, target_lang)
    messages = [
        {"role": "system", "content": BATCH_SYS_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    out = call_chat_api(messages, model, api_base, api_key)
    mapping = parse_json_mapping(out)

    for i in range(start_idx, end_idx):
        sub = subs[i]
        if str(sub["index"]) not in mapping:
            raise RuntimeError(
                f"Missing translation for id {sub['index']} in batch response."
            )
        subs[i]["text"] = mapping[str(sub["index"])]


# --------- 逐条压缩（当超 CPS 时二次请求精简） ---------
COMPRESS_SYS_PROMPT = """\
You condense subtitles while preserving meaning and tone for on-screen reading.
Return only the condensed text with line breaks kept. No commentary.
"""


def compress_to_limit(text, max_chars, model, api_base, api_key):
    user_prompt = f"""\
Condense the following subtitle to at most {max_chars} characters (inclusive).
Keep it natural and readable. Maintain line breaks ('\\n') where they help readability.

<<<
{text}
>>>"""
    messages = [
        {"role": "system", "content": COMPRESS_SYS_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    out = call_chat_api(
        messages, model, api_base, api_key, temperature=0.0, max_tokens=512
    )
    return out.strip()[:max_chars]


# --------- 时间轴微调算法（pysrt 版） ---------
def adjust_timeline_for_cps(
    subs, idx, cps, exclude_spaces, min_gap=0.10, max_shift=1.0
):
    cur = subs[idx]
    cur_len = visible_len(cur.text, exclude_spaces)
    if cur_len == 0:
        return False

    cur_dur = max(0.01, duration_seconds(cur))
    needed = cur_len / cps
    if needed <= cur_dur + 1e-6:
        return False

    deficit = needed - cur_dur
    if deficit <= 0:
        return False

    extra_from_prev = 0.0
    if idx > 0:
        prev = subs[idx - 1]
        gap_prev = (cur.start.ordinal - prev.end.ordinal) / 1000.0
        extra_from_prev = max(0.0, gap_prev - min_gap)

    extra_from_next = 0.0
    if idx < len(subs) - 1:
        nxt = subs[idx + 1]
        gap_next = (nxt.start.ordinal - cur.end.ordinal) / 1000.0
        extra_from_next = max(0.0, gap_next - min_gap)

    extra_from_prev = min(extra_from_prev, max_shift)
    extra_from_next = min(extra_from_next, max_shift)

    borrow_prev = min(extra_from_prev, deficit / 2)
    borrow_next = min(extra_from_next, deficit - borrow_prev)

    if borrow_prev + borrow_next < deficit:
        rem = deficit - (borrow_prev + borrow_next)
        left_cap = extra_from_prev - borrow_prev
        right_cap = extra_from_next - borrow_next
        add_prev = min(left_cap, rem)
        add_next = min(right_cap, rem - add_prev)
        borrow_prev += add_prev
        borrow_next += add_next

    if borrow_prev + borrow_next <= 1e-6:
        return False

    cur.start = add_seconds_to_time(cur.start, -borrow_prev)
    cur.end = add_seconds_to_time(cur.end, +borrow_next)

    if idx > 0:
        prev = subs[idx - 1]
        if (cur.start.ordinal - prev.end.ordinal) / 1000.0 < min_gap:
            cur.start = add_seconds_to_time(prev.end, min_gap)
    if idx < len(subs) - 1:
        nxt = subs[idx + 1]
        if (nxt.start.ordinal - cur.end.ordinal) / 1000.0 < min_gap:
            cur.end = add_seconds_to_time(nxt.start, -min_gap)

    return True


def translate_srt(
    subs: list[dict],
    target_lang: str = "zh",
    cps: float = 15.0,
    exclude_spaces: bool = False,
    model: str = "gpt-4o-mini",
    api_base: str = "",
    api_key: str = "",
    batch_size: int = 20,
    max_shift: float = 1.0,
    min_gap: float = 0.10,
    no_compress_pass: bool = False,
):
    try:
        api_base = os.getenv("OPENAI_API_BASE") or args.api_base
        api_key = os.getenv("OPENAI_API_KEY") or ""
        model = os.getenv("LLM_MODEL") or args.model
        for start in range(0, len(subs), batch_size):
            end = min(len(subs), start + batch_size)
            batch_translate(
                subs=subs,
                start_idx=start,
                end_idx=end,
                cps=cps,
                target_lang=target_lang,
                model=model,
                api_base=api_base,
                api_key=api_key,
                exclude_spaces=exclude_spaces,
            )

        for i in range(len(subs)):
            sub = subs[i]
            dur = max(0.01, duration_seconds(sub))
            max_chars = int(math.floor(dur * cps))
            length = visible_len(sub["text"], exclude_spaces)
            if length <= max_chars:
                continue

            if not no_compress_pass:
                sub["text"] = compress_to_limit(
                    text=sub["text"],
                    max_chars=max_chars,
                    model=model,
                    api_base=api_base,
                    api_key=api_key,
                )
                length = visible_len(sub["text"], exclude_spaces)
                if length <= max_chars:
                    continue

            needed = length / cps
            deficit = needed - dur
            if deficit > 0:
                adjust_timeline_for_cps(
                    subs=subs,
                    idx=i,
                    cps=cps,
                    exclude_spaces=exclude_spaces,
                    min_gap=min_gap,
                    max_shift=max_shift,
                )

        still_violations = []
        for sub in subs:
            dur = max(0.01, duration_seconds(sub))
            max_chars = int(math.floor(dur * cps))
            length = visible_len(sub["text"], exclude_spaces)
            if length > max_chars:
                still_violations.append((sub["index"], length, max_chars))

        mapping = {int(sub["index"]): sub["text"] for sub in subs}

        if still_violations:
            head = ", ".join(
                [f"id={sid}:{L}>{M}" for sid, L, M in still_violations[:5]]
            )
            more = "" if len(still_violations) <= 5 else f" 等共 {len(still_violations)} 条"
            msg = f"已写出文件，但仍有部分字幕超过 CPS：{head}{more}（建议人工复核或放宽限制）。"
        else:
            msg = "翻译完成，所有字幕均满足 CPS 限制。"

        return mapping, True, msg

    except Exception as e:
        return {}, False, f"翻译失败：{e}"


def translate_srt_file(
    input_path: str,
    output_path: str,
    target_lang: str = "zh",
    cps: float = 15.0,
    exclude_spaces: bool = False,
    model: str = "gpt-4o-mini",
    api_base: str = "",
    api_key: str = "",
    batch_size: int = 20,
    max_shift: float = 1.0,
    min_gap: float = 0.10,
    no_compress_pass: bool = False,
):
    """
    返回: (mapping: dict[int, str], success: bool, message: str)
    """
    try:
        api_base = os.getenv("OPENAI_API_BASE") or args.api_base
        api_key = os.getenv("OPENAI_API_KEY") or ""
        model = os.getenv("LLM_MODEL") or args.model
        subs = read_srt(input_path)
        mapping, success, msg = translate_srt(
            subs=subs,
            target_lang=target_lang,
            cps=cps,
            exclude_spaces=exclude_spaces,
            model=model,
            api_base=api_base,
            api_key=api_key,
            batch_size=batch_size,
            max_shift=max_shift,
            min_gap=min_gap,
        )
        return mapping, success, msg
    except Exception as e:
        return {}, False, f"翻译失败：{e}"


# --------- 仅 OPENAI：从 .env / 环境变量解析配置 ---------
def resolve_openai_config(args):
    """
    仅使用 OPENAI 模式：
    - 必须提供：OPENAI_API_BASE 与 OPENAI_API_KEY（优先取 .env，其次取命令行参数）
    - 模型：优先 LLM_MODEL，其次命令行 --model
    """
    base = os.getenv("OPENAI_API_BASE") or args.api_base
    key = os.getenv("OPENAI_API_KEY") or ""
    model = os.getenv("LLM_MODEL") or args.model

    if not base:
        raise RuntimeError("OPENAI_API_BASE 未设置（请在上上级目录 .env 中配置 OPENAI_API_BASE）。")
    if not key:
        raise RuntimeError("OPENAI_API_KEY 未设置（请在上上级目录 .env 中配置 OPENAI_API_KEY）。")

    return model, base, key


# --------- 主流程 ---------
def main():
    parser = argparse.ArgumentParser(
        description="Translate SRT with CPS control and timeline tweaking (pysrt). [OPENAI only]"
    )
    parser.add_argument("--input", default="output.srt", help="Input .srt path")
    parser.add_argument("--output", default="output-t.srt", help="Output .srt path")
    parser.add_argument("--target-lang", default="zh", help="Target language, e.g., 'zh', 'en', 'ja'")
    parser.add_argument("--cps", type=float, default=15.0, help="Max characters per second")
    parser.add_argument("--exclude-spaces", action="store_true", help="CPS 统计时不计空白字符")
    parser.add_argument("--model", default="gpt-4o-mini", help="OpenAI chat model name")
    parser.add_argument("--api-base", default="https://api.openai.com/v1", help="OpenAI API base (可被 .env 覆盖)")
    parser.add_argument("--batch-size", type=int, default=20, help="Batch size for translation")
    parser.add_argument("--max-shift", type=float, default=1.0, help="单条字幕允许的最大时间微调(秒)")
    parser.add_argument("--min-gap", type=float, default=0.10, help="相邻字幕最小安全间隔(秒)")
    parser.add_argument("--no-compress-pass", action="store_true", help="不进行二次精简（仅微调时间轴）")
    args = parser.parse_args()

    model, base_url, api_key = resolve_openai_config(args)

    mapping, success, message = translate_srt_file(
        input_path=args.input,
        output_path=args.output,
        target_lang=args.target_lang,
        cps=args.cps,
        exclude_spaces=args.exclude_spaces,
        model=model,
        api_base=base_url,
        api_key=api_key,
        batch_size=args.batch_size,
        max_shift=args.max_shift,
        min_gap=args.min_gap,
        no_compress_pass=args.no_compress_pass,
    )

    print("\n=== 完成 ===")
    print(mapping)
    print(f"总条数: {len(mapping)}")
    print(f"成功: {success}")
    print(f"消息: {message}")


if __name__ == "__main__":
    main()
