import logging

from fastapi import FastAPI

from app.config import get_settings
from app.webhook.router import router

settings = get_settings()
logging.basicConfig(level=settings.log_level.upper())

app = FastAPI(title="PR Analyzer")
app.include_router(router)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}
