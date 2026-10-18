"""Pipecat voice agent entry point, hosted by FastAPI (no separate RTC worker)."""

import uvicorn

from app.config import settings


def main() -> None:
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.api_port, workers=1)


if __name__ == "__main__":
    main()
