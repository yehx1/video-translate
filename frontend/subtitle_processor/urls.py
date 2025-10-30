from django.urls import path
from . import views

urlpatterns = [
    path("", views.task_list, name="task_list"),  # 任务列表页
    path("upload/", views.video_upload, name="video_upload"),  # 视频上传页
    path(
        "task/<int:task_id>/", views.task_detail, name="task_detail"
    ),  # 任务详情（字幕编辑）
    path(
        "api/task/<str:celery_task_id>/progress/",
        views.task_progress_api,
        name="task_progress_api",
    ),  # 进度查询API
    path(
        "task/<int:task_id>/confirm/",
        views.confirm_translation,
        name="confirm_translation",
    ),
    path(
        "task/<int:task_id>/refinalize/",
        views.refinalize_video,
        name="refinalize_video",
    ),
    path("task/<int:task_id>/reburn/", views.reburn_video, name="reburn_video"),
    path("task/<int:task_id>/stop/", views.stop_task, name="stop_task"),
    path("task/<int:task_id>/restart/", views.restart_task, name="restart_task"),
    path("task/<int:task_id>/delete/", views.delete_task, name="delete_task"),
    path("help/", views.help_center, name="help_center"),
]
