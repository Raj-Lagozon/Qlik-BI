"""Orchestration for the Qlik-extraction stage only. Thin wrapper around
`app.modules.extract_modules.extract_app` so callers don't reach into
`modules/` directly.
"""

from __future__ import annotations

from app.modules.extract_modules import extract_app


def extract(app_name: str, qvf_path: str) -> None:
    extract_app(qvf_path, app_name)
