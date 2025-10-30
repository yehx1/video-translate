import os, json, signal
import requests, tempfile, subprocess
from pathlib import Path
from functools import lru_cache
from django.conf import settings
from django.utils import timezone
from django.contrib import messages
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_POST
from django.core.files.storage import FileSystemStorage
from django.shortcuts import render, redirect, get_object_or_404
from django.http import JsonResponse, HttpResponse, HttpResponseBadRequest

from logs import get_logger
from .forms import VideoUploadForm
from .models import Language, VoiceProfile
from .seed_data import DEFAULT_LANGUAGES
log = get_logger(__name__)

API = settings.BACKEND_BASE_URL.rstrip("/")

MAX_VIDEO_SECONDS = int(os.getenv("MAX_VIDEO_SECONDS", "300"))

@lru_cache(maxsize=1)
def _lang_map():
    rows = Language.objects.all().values_list("target_language", "target_language_display")
    data = dict(rows) or dict(DEFAULT_LANGUAGES)
    return data

def _safe_delete_file(relpath):
    """根据 FileField 的相对路径在 MEDIA_ROOT 下删除物理文件（若存在）"""
    if not relpath:
        return
    abs_path = os.path.join(settings.MEDIA_ROOT, str(relpath))
    try:
        if os.path.exists(abs_path):
            os.remove(abs_path)
    except Exception:
        # 忽略物理文件删除失败，不影响任务记录删除
        pass

def task_list(request):
    data = {"user_id": "test01"}
    r = requests.get(f"{API}/api/tasks", data=data, timeout=30)
    tasks = r.json() if r.ok else []
    for t in tasks:
        t["created_at"] = parse_datetime(t["created_at"])
    return render(request, "subtitle_processor/task_list.html", {"tasks": tasks})

def help_center(request):
    """简洁帮助中心"""
    return render(request, "help_center.html")

def _get_media_duration_seconds(path: str) -> float:
    r = subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",path],capture_output=True,text=True)
    if r.returncode!=0: raise RuntimeError(r.stderr.strip() or "ffprobe failed")
    return float(r.stdout.strip())


def video_upload(request):
    user_id = "test01"
    if request.method == "POST":
        form = VideoUploadForm(request.POST, request.FILES)
        if form.is_valid():
            fe_dur = (request.POST.get("frontend_duration_seconds") or "").strip()
            fe_probe = (request.POST.get("frontend_probe") or "").strip()
            log.info(f"上传视频，前端视频时长检测结果：{fe_dur}-{fe_probe}")
            if False and fe_probe == "frontend_ok": 
                f = request.FILES["video_file"]
                files = {"video": (f.name, f.read(), f.content_type)}
                lang_code = form.cleaned_data["target_language"]
                lang_map = _lang_map()
                lang_display = lang_map.get(lang_code, lang_code)
                data = {"user_id": user_id, "title": form.cleaned_data["title"], "target_language": lang_code,  
                        "target_language_display": lang_display, "video_duration_seconds": float(fe_dur)}
                r = requests.post(f"{API}/api/tasks", data=data, files=files, timeout=180)
                if r.status_code == 200:
                    messages.success(request, "任务创建成功，已进入队列。")
                    return redirect("task_list")
                messages.error(request, f"后端创建失败：{r.text[:200]}")
            f = request.FILES["video_file"]
            # === 先写入临时文件用于时长探测 ===
            suffix = os.path.splitext(getattr(f, "name", ""))[1] or ".mp4"
            tmp_dir = Path(settings.MEDIA_ROOT) / "tmp"
            tmp_dir.mkdir(parents=True, exist_ok=True) 
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix, dir=str(tmp_dir)) as tmp:
                tmp_path = tmp.name
                for chunk in f.chunks():
                    tmp.write(chunk)
            try:
                dur = _get_media_duration_seconds(tmp_path)
                if dur is not None and dur > MAX_VIDEO_SECONDS:
                    # 超出上限：拦截并提示
                    mm = int(dur // 60)
                    ss = int(dur % 60)
                    limit_mm = int(MAX_VIDEO_SECONDS // 60)
                    limit_ss = int(MAX_VIDEO_SECONDS % 60)
                    messages.error(
                        request,
                        f"视频过长：{mm:02d}:{ss:02d}，上限 {limit_mm:02d}:{limit_ss:02d}。请裁剪后再上传。"
                    )
                    # 重新渲染页面（保留表单其他字段）
                    return render(request, "subtitle_processor/video_upload.html", {
                        "form": form,
                        "max_video_seconds": MAX_VIDEO_SECONDS,
                    })
                if dur is None:
                    # 读不到时长：放行，但提醒（后端仍有兜底）
                    dur = 0.0
                    messages.warning(request, "未能读取视频时长，已放行提交；若过长后端会拒绝。")
                # === 通过校验，转发到后端 ===
                # 用临时文件重新打开，避免重复读内存
                with open(tmp_path, "rb") as fp:
                    files = {"video": (getattr(f, "name", "video.mp4"), fp, getattr(f, "content_type", "video/mp4"))}
                    lang_code = form.cleaned_data["target_language"]
                    lang_map = _lang_map()
                    lang_display = lang_map.get(lang_code, lang_code)
                    data = {
                        "user_id": user_id, "title": form.cleaned_data["title"],
                        "target_language": lang_code, "target_language_display": lang_display,
                        "video_duration_seconds": float(dur)
                    }
                    r = requests.post(f"{API}/api/tasks", data=data, files=files, timeout=180)
                if r.status_code == 200:
                    messages.success(request, "任务创建成功，已进入队列。")
                    return redirect("task_list")
                messages.error(request, f"后端创建失败：{r.text[:200]}")
            finally: 
                try:
                    os.remove(tmp_path) # 清理临时文件
                except Exception: pass
    else:
        form = VideoUploadForm()

    return render(request, "subtitle_processor/video_upload.html", {
        "form": form,
        "max_video_seconds": MAX_VIDEO_SECONDS,
    })

def task_progress_api(request, celery_task_id):
    # 可以让页面把 task_id 传来，或沿用你原来的“id-{task_id}”规则：
    task_id = int(str(celery_task_id).split("id-")[-1])
    r = requests.get(f"{API}/api/tasks/{task_id}/progress", timeout=10)
    return JsonResponse(r.json() if r.ok else {
        "state":"UNKNOWN","progress":0,"status":"查询失败","task_status":"FAILED","final_video_file":""
    })

def task_detail(request, task_id):
    r = requests.get(f"{API}/api/tasks/{task_id}", timeout=30)
    if not r.ok:
        messages.error(request, "任务不存在"); return redirect("task_list")
    dto = r.json()
    # 保存样式（表单字段名需与你模板中的 input/select name 一致）
    if request.method=="POST" and request.POST.get("action")=="save_style":
        try:
            payload = {
                "subtitle_format": request.POST.get("subtitle_format") or None,
                "burn_subtitle": request.POST.get("burn_subtitle") == "on",
                "sub_font_name": request.POST.get("sub_font_name") or None,
                "sub_font_size": int(request.POST.get("sub_font_size")) if request.POST.get("sub_font_size") else None,
                "sub_font_bold": request.POST.get("sub_font_bold") == "on",
                "sub_font_italic": request.POST.get("sub_font_italic") == "on",
                "sub_font_underline": request.POST.get("sub_font_underline") == "on",
                "sub_font_color": request.POST.get("sub_font_color") or None,
                "sub_outline_color": request.POST.get("sub_outline_color") or None,
                "sub_back_color": request.POST.get("sub_back_color") or None,
                "sub_outline_width": float(request.POST.get("sub_outline_width")) if request.POST.get("sub_outline_width") else None,
                "sub_back_opacity": float(request.POST.get("sub_back_opacity")) if request.POST.get("sub_back_opacity") else None,
                "sub_alignment": int(request.POST.get("sub_alignment")) if request.POST.get("sub_alignment") else None,
                "bgm_volume": float(request.POST.get("bgm_volume")) if request.POST.get("bgm_volume") else None,
                "tts_volume": float(request.POST.get("tts_volume")) if request.POST.get("tts_volume") else None,
                # 若你的样式表单里也有 TTS 选项，可放开下面两行
                "tts_gender": request.POST.get("tts_gender") or None,
                "tts_voice": request.POST.get("tts_voice") or None,
            }
            # 去掉 None（避免把空值写入）
            payload = {k:v for k,v in payload.items() if v is not None}
            rr = requests.patch(f"{API}/api/tasks/{task_id}/style", json=payload, timeout=30)
            if rr.ok:
                messages.success(request, "样式已保存到后端（数据库）并将在合成时生效。")
                return redirect("task_detail", task_id=task_id)
            messages.error(request, f"样式保存失败：{rr.text[:200]}")
        except Exception as e:
            messages.error(request, f"样式保存异常：{str(e)[:200]}")

    # 保存音色
    if request.method=="POST" and request.POST.get("action")=="save_tts":
        try:
            payload = {
                "tts_gender": request.POST.get("tts_gender") or None,
                "tts_voice": request.POST.get("tts_voice") or None,
            }
            vp = (
                VoiceProfile.objects
                .filter(language_code=lang_code, code=voice_code, enabled=True)
                .only("tts_name")
                .first()
            )
            payload["tts_name"] = vp.tts_name if vp else None
            payload = {k: v for k, v in payload.items() if v is not None}
            rr = requests.patch(f"{API}/api/tasks/{task_id}/style", json=payload, timeout=30)
            if rr.ok:
                messages.success(request, "音色已保存，将在合成时生效。")
                return redirect("task_detail", task_id=task_id)
            messages.error(request, f"保存音色失败：{rr.text[:200]}")
        except Exception as e:
            messages.error(request, f"保存音色异常：{str(e)[:200]}")
    # 编辑单条字幕
    edit_subtitle_id = request.GET.get("edit_subtitle_id") or request.POST.get("edit_subtitle_id")
    edit_form = None
    if edit_subtitle_id and request.method=="POST" and request.POST.get("action")=="edit_one":
        body = {
            "start_time": request.POST["start_time"],
            "end_time": request.POST["end_time"],
            "translated_text": request.POST["translated_text"].strip(),
        }
        rr = requests.patch(f"{API}/api/subtitles/{task_id}/{edit_subtitle_id}", json=body, timeout=30)
        if rr.ok:
            messages.success(request, "字幕编辑成功！")
            return redirect("task_detail", task_id=task_id)
        messages.error(request, rr.text[:200])
    if edit_subtitle_id:
        edit_form = dto.get("subtitles", [])[int(request.POST["sequence"]) - 1]

    iso_str = dto["created_at"]  # '2025-10-20T14:55:09.788361'
    dt = parse_datetime(iso_str)
    if dt and timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    dto["created_at"] = dt
    # ====== 音色 VOICE_BANK：从数据库按语言聚合 ======
    voices_qs = VoiceProfile.objects.filter(enabled=True).order_by("language_code", "sort_order", "code")
    voice_bank = {}
    for v in voices_qs:
        voice_bank.setdefault(v.language_code, []).append({
            "code": v.code,
            "name": v.name,
            "gender": v.gender,
            "sample": v.sample_url or "",
        })
    voice_bank_json = json.dumps(voice_bank, ensure_ascii=False)
    return render(request, "subtitle_processor/task_detail.html", {
        "task": dto,
        "subtitles": dto.get("subtitles", []),
        "subtitle_count": len(dto.get("subtitles", [])),
        "edit_form": edit_form, "edit_subtitle_id": edit_subtitle_id,
        "voice_bank_json": voice_bank_json
    })

@require_POST
def confirm_translation(request, task_id):
    r = requests.post(f"{API}/api/tasks/{task_id}/confirm", timeout=15)
    messages.success(request, "已确认，进入合成队列。" if r.ok else f"失败：{r.text[:200]}")
    return redirect("task_list")

@require_POST
def refinalize_video(request, task_id):
    r = requests.post(f"{API}/api/tasks/{task_id}/confirm", timeout=15)
    messages.success(request, "已提交重新合成。" if r.ok else f"失败：{r.text[:200]}")
    return redirect("task_list")

@require_POST
def reburn_video(request, task_id):
    r = requests.post(f"{API}/api/tasks/{task_id}/reburn", timeout=15)
    messages.success(request, "已提交仅重新合成字幕。" if r.ok else f"失败：{r.text[:200]}")
    return redirect("task_list")

@require_POST
def restart_task(request, task_id):
    r = requests.post(f"{API}/api/tasks/{task_id}/restart", timeout=15)
    messages.success(request, "已重新开始阶段一。" if r.ok else f"失败：{r.text[:200]}")
    return redirect("task_list")

@require_POST
def stop_task(request, task_id):
    """
    调后端统一停止接口：
      POST /api/tasks/{task_id}/stop
    后端会：
      - 清理租约/心跳/占位，移出队列
      - 按 queued_for 回退到 REVIEW / SUCCESS / 或 FAILED
      - 同步更新 msg 与 error_msg
    """
    log.info(f"stop task {task_id}")
    try:
        resp = requests.post(f"{API}/api/tasks/{task_id}/stop", timeout=20)
        if resp.ok:
            dto = resp.json()
            st = dto.get("status")
            msg = dto.get("msg") or ""
            if st == "REVIEW":
                messages.info(request, f"已停止任务：回退到『待确认』。{msg}")
            elif st == "SUCCESS":
                messages.info(request, f"已停止任务：保留上次成功成片。{msg}")
            else:
                messages.info(request, f"已停止任务并标记为失败。{msg}")
        else:
            detail = ""
            try:
                detail = (resp.json().get("detail") or "").strip()
            except Exception:
                pass
            if not detail:
                detail = resp.text.strip()[:200]
            messages.error(request, f"停止失败：{detail or '未知错误'}")
    except Exception as e:
        messages.error(request, f"停止请求异常：{str(e)[:200]}")
    return redirect("task_list")

@require_POST
def delete_task(request, task_id):
    """
    改为直接调用后端：
    DELETE /api/tasks/{task_id}

    后端会做状态校验（PROCESSING/QUEUED 不允许删除）并清理后端媒体文件。
    前端仅根据返回结果提示用户，无需再本地删除文件或操作本地数据库。
    """
    try:
        resp = requests.delete(f"{API}/api/tasks/{task_id}", timeout=15)
        if resp.ok:
            messages.success(request, "任务已删除。")
        else:
            # 优先取后端的 detail 文本（FastAPI 常见返回）
            detail = ""
            try:
                detail = (resp.json().get("detail") or "").strip()
            except Exception:
                pass
            if not detail:
                detail = resp.text.strip()[:200]
            if resp.status_code == 400:
                # 后端对运行中/排队中任务会返回 400
                messages.error(request, detail or "任务正在运行或排队中，请先停止后再删除。")
            elif resp.status_code == 404:
                messages.error(request, "任务不存在或已被删除。")
            else:
                messages.error(request, f"删除失败：{detail or '未知错误'}")
    except Exception as e:
        messages.error(request, f"删除请求异常：{str(e)[:200]}")
    return redirect("task_list")
