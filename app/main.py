"""Vorflux Gateway — OpenAI-compatible multi-account gateway."""
from __future__ import annotations

import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
import db
from admin_api import router as admin_router, _admin_token
from openai_api import router as openai_router
from pool import pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    _admin_token()          # ensure admin token exists (prints if generated)
    await pool.reload()
    yield


app = FastAPI(title="Vorflux Gateway", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(openai_router)
app.include_router(admin_router)


@app.get("/healthz")
async def healthz():
    ov = pool.overview()
    return {"ok": True, "accounts_healthy": ov["accounts_healthy"],
            "accounts_total": ov["accounts_total"]}


# ------------------------------------------------------------------- webui
@app.get("/")
async def index():
    return FileResponse(config.WEBUI_DIR / "index.html")


if config.WEBUI_DIR.exists():
    app.mount("/static", StaticFiles(directory=config.WEBUI_DIR), name="static")


@app.exception_handler(Exception)
async def unhandled(request, exc):
    return JSONResponse({"error": {"message": str(exc),
                                   "type": "server_error"}}, status_code=500)
