<p align="left">
    English</a>&nbsp ｜ &nbsp<a href="README_CN.md">中文</a>&nbsp ｜ &nbsp
</p>


# 🌏 VT Video Translation Assistant

Break the language barrier for your videos — one click to multilingual translation.  
From video upload to multilingual dubbing, from subtitle generation to final rendering — an all-in-one solution to make your content understood worldwide.

> 🌐 Try Online: [https://vt.fa-tools.com](https://vt.fa-tools.com)


---

## 🚀 Overview

**VT Video Translation Assistant** is a full-process multilingual video localization and distribution tool designed for creators, educators, brands, and teams.  
It integrates **speech separation, recognition, LLM translation, voice cloning, subtitle editing, and video synthesis** into one seamless workflow.

Supports nearly 100 languages — including Chinese, English, Japanese, Korean, French, and German.

---

## 🧩 Key Features

| Feature | Description |
|----------|-------------|
| 🎧 Voice Separation | Automatically separates vocals and background music |
| 🗣️ Speech Recognition | Accurate ASR using Whisper model |
| 🌍 AI Translation | Context-aware translation using Qwen or GPT |
| 🧬 Speech Synthesis | Supports XTTS, Edge-TTS, Azure-TTS engines |
| 🎛️ Voice Cloning | Preserves original tone and emotion |
| 📝 Subtitle Editing | Real-time in-page editing without reloading |
| 🎬 Video Rendering | Automatically generates final multilingual videos |

---

## ⚙️ Environment Setup

### System Dependencies
```bash
apt-get update
apt-get install -y vim git wget build-essential ffmpeg portaudio19-dev     libasound2-dev fonts-noto-cjk fonts-noto-cjk-extra     fonts-wqy-zenhei fonts-wqy-microhei
```

### Python Environment
```bash
conda create -n trvideo python=3.11 -y
conda activate trvideo

git clone https://gitee.com/fgai/video-translate.git
cd video-translate

pip install torch==2.5.0 torchvision==0.20.0 torchaudio==2.5.0     --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
```

---

## 📦 Model Downloads

```bash
mkdir -p models

# Whisper ASR model
modelscope download --model Systran/faster-whisper-large-v2 --local_dir ./Systran/faster-whisper-large-v2

# XTTS speech synthesis model
export HF_ENDPOINT=https://hf-mirror.com
huggingface-cli download --resume-download coqui/XTTS-v2 --local-dir coqui/XTTS-v2

# Link to TTS model path
ln -s /root/project/release/models/coqui/XTTS-v2       /root/.local/share/tts/tts_models--multilingual--multi-dataset--xtts_v2
```

---

## 🧭 Run Services

### 💡 Method 1: Manual Start

#### Frontend (Django)

```bash
cd frontend
mkdir -p media/final_videos
python manage.py makemigrations
python manage.py migrate
python manage.py runserver 0.0.0.0:31005
```

#### Backend (FastAPI)

```bash
cd backend
mkdir -p media/videos media/vocals media/bgm media/videos_novocals \
         media/srts media/tts media/final_videos

uvicorn app.main:app --host 0.0.0.0 --port 31006 --workers 1 --reload
```

---
### 💡 Method 2: One-Click Startup (Recommended)

A startup script is included in the project root:

```bash
cd video-translate
chmod +x start.sh
./start.sh
```

This will automatically start both the **frontend (Django)** and **backend (FastAPI)** servers.
Once started, open:

* Frontend: [http://127.0.0.1:31005/](http://127.0.0.1:31005/)
* Backend: [http://127.0.0.1:31006/](http://127.0.0.1:31006/)

---

## 🔍 API Test

```bash
curl "http://localhost:31006/api/tasks"
```

---

## 🧠 Architecture Overview

```
├── backend/     # FastAPI backend service
├── frontend/    # Django frontend
└── models/      # Model storage
```

---

## 💡 Use Cases

- Multilingual short video republishing  
- Course, lecture, and training localization  
- Product demos and brand internationalization  

---

## 🧑‍💻 Tech Stack

| Component | Technology |
|------------|-------------|
| Backend | FastAPI, SQLite, Uvicorn |
| Frontend | Django, Bootstrap, jQuery |
| Models | Whisper, XTTS, Edge-TTS, GPT/Qwen |
| System | Ubuntu 22.04+, Conda, Docker |

---
## 💖 Support the Project

If you like this project and wish to support its continued development, you can sponsor via QR code:

<p align="center">
  <img src="./frontend/static/assets/wechat_qr.jpg" alt="AliPay Sponsor" width="220" /> AliPay &nbsp Sponsor
  <br />
  <img src="./frontend/static/assets/alipay_qr.jpg" alt="WeChat Sponsor" width="220" /> WeChat Sponsor
</p>

> Your support helps us maintain and improve VT Video Translation Assistant continuously.

---

## 📜 License

Apache License 2.0  
© 2025 VT Video Translation Assistant Team
