"""FastAPI entrypoint for the QVF -> PBIX web API.

Run from the backend/ directory:
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config.settings import settings

# The pipeline's own config (Qlik Cloud / Azure OpenAI creds) lives in a
# single .env at the repo root, shared by the CLI and this API.
load_dotenv(settings.project_root / ".env")

from app.api.routes.conversion import router as conversion_router  # noqa: E402
from app.exceptions import register_exception_handlers  # noqa: E402


class _MuteLogPolling(logging.Filter):
    """The frontend polls GET .../logs every ~1.5s while a job runs, which
    would otherwise print one uvicorn access-log line per poll for the
    entire duration of every conversion. Drop just those lines; every other
    request (upload/run/download, and any non-2xx response) still logs
    normally.

    uvicorn's access logger logs with args=(client_addr, method, full_path,
    http_version, status_code) and a "%d" placeholder for the status — the
    "200 OK" phrase text only appears later, added by the Formatter, which
    runs after filters. So the status must be read from record.args here,
    not string-matched against the not-yet-formatted message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not isinstance(record.args, tuple) or len(record.args) != 5:
            return True
        _client_addr, _method, full_path, _http_version, status_code = record.args
        return not (isinstance(full_path, str) and "/logs" in full_path and status_code == 200)


logging.getLogger("uvicorn.access").addFilter(_MuteLogPolling())

app = FastAPI(title="Qlik to Power BI Conversion API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)
app.include_router(conversion_router)

if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend")
