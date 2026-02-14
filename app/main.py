"""
FastAPI application — ghép WebSocket + REST API.
"""

import logging
from fastapi import FastAPI, WebSocket

from app.config import config
from app.api.routes import router as api_router
from app.websocket.handler import handle_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="XiaoZhi ESP32 Server",
    version="1.0.0",
    description="Custom server cho xiaozhi-esp32. WebSocket + REST API.",
)

app.include_router(api_router)


@app.websocket("/")
async def websocket_endpoint(ws: WebSocket):
    """ESP32 kết nối vào đây."""
    await handle_client(ws)


@app.on_event("startup")
async def on_startup():
    logger.info("=" * 60)
    logger.info("🚀 XiaoZhi Server started")
    logger.info(f"   WebSocket : ws://0.0.0.0:{config.server.port}/")
    logger.info(f"   REST API  : http://0.0.0.0:{config.server.port}/api/")
    logger.info(f"   Docs      : http://0.0.0.0:{config.server.port}/docs")
    providers = [f"{p.name}({p.model})" for p in config.llm.providers]
    intent_providers = [f"{p.name}({p.model})" for p in config.intent_llm.providers]
    logger.info(f"   LLM       : {' → '.join(providers)}")
    logger.info(f"   Intent LLM: {' → '.join(intent_providers)}")
    logger.info(f"   TTS model : {config.tts.model_path}")
    logger.info(f"   TTS style : {config.tts.voice_style}")
    logger.info(f"   Audio in  : {config.audio_input.sample_rate}Hz")
    logger.info(f"   Audio out : {config.audio_output.sample_rate}Hz")
    logger.info("=" * 60)
