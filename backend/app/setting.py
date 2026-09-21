"""Runtime configuration for the web API.

The conversion pipeline (modules/extract, modules/script, modules/data_model,
modules/sheet, modules/build) is part of this same backend app and is
called in-process by services/pipeline_services.py (full run) or the
per-stage services (extract/convert/build); the pipeline's own .env (Qlik
Cloud / Azure OpenAI config) lives at backend/.env — see backend/.env.example.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

BACKEND_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"


class Settings(BaseSettings):
    project_root: Path = PROJECT_ROOT
    backend_dir: Path = BACKEND_DIR
    frontend_dir: Path = FRONTEND_DIST_DIR
    uploads_dir: Path = BACKEND_DIR / "uploads"
    max_upload_bytes: int = 500 * 1024 * 1024  # 500 MB
    allowed_extensions: tuple[str, ...] = (".qvf",)
    cors_origins: tuple[str, ...] = ("*",)


settings = Settings()
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
