"""Runtime configuration for the web API.

The conversion pipeline (services/external/qlik, services/external/llm,
services/internal/pbip_build) is part of this same backend app and is
called in-process by services/external/pipeline_runner.py; the pipeline's
own .env (Qlik Cloud / Azure OpenAI config) lives at the repo root and is
shared with the CLI (cli.py).
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

BACKEND_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_DIR.parent
FRONTEND_DIST_DIR = PROJECT_ROOT / "frontend" / "dist"


class Settings(BaseSettings):
    project_root: Path = PROJECT_ROOT
    frontend_dir: Path = FRONTEND_DIST_DIR
    uploads_dir: Path = BACKEND_DIR / "uploads"
    cli_path: Path = PROJECT_ROOT / "cli.py"
    python_executable: Path = (
        PROJECT_ROOT / "venv" / "Scripts" / "python.exe"
        if (PROJECT_ROOT / "venv" / "Scripts" / "python.exe").exists()
        else Path("python")
    )
    max_upload_bytes: int = 500 * 1024 * 1024  # 500 MB
    allowed_extensions: tuple[str, ...] = (".qvf",)
    cors_origins: tuple[str, ...] = ("*",)


settings = Settings()
settings.uploads_dir.mkdir(parents=True, exist_ok=True)
