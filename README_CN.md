<p align="left">
    <a href="README.md">English</a>&nbsp ｜ &nbsp中文&nbsp ｜ &nbsp
</p>

# 🌏 VT 视频翻译助手

让视频跨越语言障碍，一键实现多语言翻译  
从视频上传到多语言配音，从字幕生成到最终成片，一站式解决方案让全球观众都能理解你的内容。

> 🌐 在线体验地址：[https://vt.fa-tools.com](https://vt.fa-tools.com)


---

## 🚀 项目简介

**VT 视频翻译助手**是一款面向创作者、教育机构、企业品牌与服务团队的全流程多语言视频本地化与分发工具。  
集成语音识别、大模型翻译、语音合成、声音克隆、字幕编辑与视频合成等技术。  

支持近百种语言，包括中文、英语、日语、韩语、法语、德语等。

---

## 🧩 功能亮点

| 功能 | 说明 |
|------|------|
| 🎧 人声分离 | 自动分离人声与背景音乐 |
| 🗣️ 语音识别 | Whisper 模型高精度识别语音 |
| 🌍 大模型翻译 | 支持 Qwen、GPT 等上下文翻译 |
| 🧬 语音合成 | 支持 XTTS、Edge-TTS、Azure-TTS 多引擎 |
| 🎛️ 声音克隆 | 保留原音色与情感 |
| 📝 字幕编辑 | 可视化无刷新编辑界面 |
| 🎬 视频合成 | 自动合成多语成片，直接发布 |

---

## ⚙️ 环境安装

### 系统依赖
```bash
apt-get update
apt-get install -y vim git wget build-essential ffmpeg portaudio19-dev     libasound2-dev fonts-noto-cjk fonts-noto-cjk-extra     fonts-wqy-zenhei fonts-wqy-microhei
```

### Python 环境
```bash
conda create -n trvideo python=3.11 -y
conda activate trvideo

git clone https://gitee.com/fgai/video-translate.git
cd video-translate

pip install torch==2.5.0 torchvision==0.20.0 torchaudio==2.5.0     --index-url https://download.pytorch.org/whl/cu124
pip install -r requirements.txt
```

---

## 📦 模型下载

```bash
mkdir -p models

# Whisper 语音识别模型
modelscope download --model Systran/faster-whisper-large-v2 --local_dir ./Systran/faster-whisper-large-v2

# XTTS 语音合成模型
export HF_ENDPOINT=https://hf-mirror.com
huggingface-cli download --resume-download coqui/XTTS-v2 --local-dir coqui/XTTS-v2

# 软链接到 TTS 目录
ln -s /root/project/release/models/coqui/XTTS-v2       /root/.local/share/tts/tts_models--multilingual--multi-dataset--xtts_v2
```

---

## 🧭 启动服务

### 💡 方法一：手动启动

#### 前端 (Django)

```bash
cd frontend
mkdir -p media/final_videos
python manage.py makemigrations
python manage.py migrate
python manage.py runserver 0.0.0.0:31005
```

#### 后端 (FastAPI)

```bash
cd backend
mkdir -p media/videos media/vocals media/bgm media/videos_novocals \
         media/srts media/tts media/final_videos

uvicorn app.main:app --host 0.0.0.0 --port 31006 --workers 1 --reload
```

---
### 💡 方法二：一键启动（推荐）

项目根目录下已提供一键启动脚本：

```bash
cd video-translate
chmod +x start.sh
./start.sh
```

脚本会自动启动前端 (Django) 与后端 (FastAPI) 服务。
启动完成后访问：

* 前端地址：[http://127.0.0.1:31005/](http://127.0.0.1:31005/)
* 后端接口：[http://127.0.0.1:31006/](http://127.0.0.1:31006/)

---

## 🔍 测试接口

```bash
curl "http://localhost:31006/api/tasks"
```

---

## 🧠 架构概览

```
├── backend/     # FastAPI 后端
├── frontend/    # Django 前端
└── models/      # 模型文件目录
```

---

## 💡 适用场景

- 短视频多语改编与出海分发  
- 课程、讲座、培训视频本地化  
- 产品演示与品牌内容国际化  

---

## 🧑‍💻 技术栈

| 模块 | 技术 |
|------|------|
| 后端 | FastAPI, SQLite, Uvicorn |
| 前端 | Django, Bootstrap, jQuery |
| 模型 | Whisper, XTTS, Edge-TTS, GPT/Qwen |
| 系统 | Ubuntu 22.04+, Conda, Docker |

---
## 💖 支持项目 / 赞助我们

如果这个项目对你有帮助，欢迎扫码赞助支持开发：

<p align="center">
  <img src="./frontend/static/assets/wechat_qr.jpg" alt="微信赞助" width="220" /> 微&nbsp&nbsp&nbsp信赞助
  <br />
  <img src="./frontend/static/assets/alipay_qr.jpg" alt="支付宝赞助" width="220" /> 支付宝赞助
</p>

> 你的支持将帮助我们持续改进、优化与维护 VT 视频翻译助手。

---

## 📜 License

Apache License 2.0  
© 2025 VT Video Translation Assistant Team
