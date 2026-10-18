"""Ensure operational voice summaries are visible under uvicorn's default config."""

import logging


def configure_diagnostics() -> None:
    for name in ("app.features.voice_followup.stream", "app.features.voice_followup.voice_health",
                 "app.features.voice_followup.tts", "app.features.order_intake.transcribe"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        if not logger.hasHandlers():
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
            logger.addHandler(handler)
