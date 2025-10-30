import os, asyncio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from .db import Base, engine
from .routes import tasks, subtitles
from .queue import dispatcher
from fastapi.staticfiles import StaticFiles
from .processors.utils import MEDIA_ROOT
from app.logs import configure_logging, attach_request_logger, get_logger
configure_logging()
log = get_logger("main")
log.info("服务启动中...")

Base.metadata.create_all(bind=engine)
app = FastAPI(title="Video Translate Backend", version="1.0.0")
attach_request_logger(app)

# CORS（允许前端 Django 域名）
origins = os.getenv("CORS_ORIGINS","*").split(",")
app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

app.include_router(tasks.router)
app.include_router(subtitles.router)

app.mount("/media", StaticFiles(directory=MEDIA_ROOT), name="media")

@app.on_event("startup")
async def _startup():
    asyncio.create_task(dispatcher())
