import argparse
import uvicorn
from app.config import config
from app.server_logging import setup_logging


def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="Nexus ESP32 Server")
    parser.add_argument("--host", default=config.server.host, help="Bind host")
    parser.add_argument("--port", type=int, default=config.server.port, help="Bind port")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on code change")
    args = parser.parse_args()

    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        log_config=None,
    )


if __name__ == "__main__":
    main()
