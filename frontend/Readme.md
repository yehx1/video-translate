sudo apt update && sudo apt install ffmpeg redis-server

pip install django celery redis deep-translator -i https://pypi.tuna.tsinghua.edu.cn/simple

Django==4.2.8
celery==5.3.6
redis==5.0.1
faster-whisper==0.10.0
demucs==4.0.1
librosa==0.10.1
soundfile==0.12.1
ffmpeg-python==0.2.0
deep-translator==1.11.4
bootstrap==5.3.2  # 前端样式
python-dotenv==1.0.0

# 创建Django项目
django-admin startproject video_translate .

# 创建功能APP（处理字幕核心逻辑）
python manage.py startapp subtitle_processor

# 创建迁移文件
python manage.py makemigrations

# 执行迁移（创建数据库表）
python manage.py migrate

# 创建超级用户（用于管理后台）
python manage.py createsuperuser

# Ubuntu/Debian
sudo systemctl start redis-server

# 验证Redis是否运行
redis-cli ping  # 返回PONG表示正常

# 启动Worker，并发数由settings中的CELERY_WORKER_CONCURRENCY决定
celery -A video_translate worker --loglevel=info

CUDA_VISIBLE_DEVICES=7 python manage.py runserver 0.0.0.0:8800

# 重启Django服务器（按Ctrl+C停止后重新启动）
python manage.py runserver

# 重启Celery Worker（按Ctrl+C停止后重新启动）
CUDA_VISIBLE_DEVICES=7 celery -A video_subtitle worker --loglevel=info