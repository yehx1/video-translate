#!/bin/bash
SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
echo "Starting services in background with no logs"
echo "SCRIPT_DIR: $SCRIPT_DIR"

FRONTEND_PATH="$SCRIPT_DIR/frontend"
BACKEND_PATH="$SCRIPT_DIR/backend"

mkdir -p $FRONTEND_PATH/media/final_videos
mkdir -p $BACKEND_PATH/media/videos $BACKEND_PATH/media/vocals $BACKEND_PATH/media/bgm $BACKEND_PATH/media/videos_novocals $BACKEND_PATH/media/srts $BACKEND_PATH/media/tts $BACKEND_PATH/media/final_videos

# 启动前端服务（不产生日志）
cd $FRONTEND_PATH || exit
python manage.py makemigrations
python manage.py migrate
python manage.py runserver 0.0.0.0:31005 > /dev/null 2>&1 &

# 等待前端启动
sleep 2

# 启动后端服务（不产生日志）
cd $BACKEND_PATH || exit
uvicorn app.main:app --host 0.0.0.0 --port 31006 --workers 1 --reload > /dev/null 2>&1 &

echo "Services started in background with no logs"