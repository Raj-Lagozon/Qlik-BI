"""FastAPI entrypoint for the QVF -> PBIX web API.

Run from the backend/ directory:
    uvicorn app.main:app --reload --port 8000
"""

from __future__ import annotations

import logging
import sys

# Windows' console defaults stdout/stderr to the system codepage (cp1252),
# which can't encode characters Qlik data or the LLM's own output routinely
# contain (currency symbols like ₹, em dashes, accented names, emoji). Any
# print() anywhere in the pipeline touching such text then crashes the whole
# request with an unrelated-looking UnicodeEncodeError. Force UTF-8 here,
# before any other module has a chance to print, so this is fixed globally
# rather than needing every print() call site to be defensive.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.setting import settings

# The pipeline's own config (Qlik Cloud / Azure OpenAI creds) lives in
# backend/.env — see backend/.env.example.
load_dotenv(settings.backend_dir / ".env")

from app.routes import router as api_router  # noqa: E402
from app.exception import register_exception_handlers  # noqa: E402


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
app.include_router(api_router)

if settings.frontend_dir.exists():
    app.mount("/", StaticFiles(directory=str(settings.frontend_dir), html=True), name="frontend")
