import logging
from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from pathlib import Path
import os
from starlette.middleware.sessions import SessionMiddleware

from app.config import config
from app.api.routes import router as api_router, v1_router
from app.api.ota import router as ota_router
from app.api.ota_activate import router as ota_activate_router
from app.api.auth_google import router as auth_google_router
from app.api.auth import router as auth_local_router
from app.websocket.handler import handle_client
from app.mcp.alarm_scheduler import start_scheduler
from app.database.connection import init_database

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="Nexus ESP32 Server",
    version="1.0.0",
    description="Custom server cho nexus-esp32. WebSocket + REST API.",
    docs_url=None,         # Ẩn /docs
    redoc_url=None,        # Ẩn /redoc
    openapi_url=None,      # Ẩn /openapi.json
)

## Removed SessionMiddleware to prevent double Set-Cookie (session=null) and avoid overwriting custom session cookie

app.include_router(api_router)
app.include_router(v1_router)
app.include_router(ota_router)
app.include_router(ota_activate_router)
app.include_router(auth_google_router)
app.include_router(auth_local_router)

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def dashboard_panel():
    dashboard_html_path = Path("static/admin/index.html")
    if dashboard_html_path.exists():
        with open(dashboard_html_path, "r", encoding="utf-8") as f:
            content = f.read()
        return HTMLResponse(content=content)
    else:
        return HTMLResponse(content="<h1>Dashboard not found</h1>")


@app.websocket("/")
async def websocket_endpoint(ws: WebSocket):
    await handle_client(ws)


@app.on_event("startup")
async def on_startup():
    logger.info("=" * 60)
    init_database()
    try:
        await start_scheduler()
    except Exception:
        logger.exception("Failed to start alarm scheduler")
    logger.info("🚀 Nexus Server started")
    logger.info(f"   WebSocket : ws://0.0.0.0:{config.server.port}/")
    logger.info(f"   REST API  : http://0.0.0.0:{config.server.port}/api/")
    logger.info(f"   Dashboard: http://0.0.0.0:{config.server.port}/")
    logger.info(f"   Docs      : http://0.0.0.0:{config.server.port}/docs")
    providers = [f"{p.name}({p.model})" for p in config.llm.providers]
    intent_providers = [f"{p.name}({p.model})" for p in config.intent_llm.providers]
    logger.info(f"   LLM       : {' → '.join(providers)}")
    logger.info(f"   Intent LLM: {' → '.join(intent_providers)}")
    # Log TTS model info based on config
    if hasattr(config.tts, "google_tts_voice"):
        logger.info(f"   TTS model : Google {config.tts.google_tts_voice} ({config.tts.google_tts_language})")
    # If Piper TTS is used, uncomment below:
    # elif hasattr(config.tts, "model_path"):
    #     logger.info(f"   TTS model : {config.tts.model_path}")
    logger.info(f"   TTS style : {config.tts.voice_style}")
    logger.info(f"   Audio in  : {config.audio_input.sample_rate}Hz")
    logger.info(f"   Audio out : {config.audio_output.sample_rate}Hz")
    logger.info("=" * 60)
