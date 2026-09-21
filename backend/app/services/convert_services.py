"""Per-stage + full conversion, each runnable as its own job step. Thin
wrappers around `app.modules.convert_modules` so routes don't reach into
`modules/` directly.
"""

from __future__ import annotations

from app.modules.convert_modules import convert_all, convert_data_model, convert_script, convert_sheet
from app.services.step_runner import start_step_background


def start_convert_script(job_id: str, app_name: str) -> None:
    start_step_background(job_id, "convert/script", lambda: convert_script(app_name))


def start_convert_data_model(job_id: str, app_name: str) -> None:
    start_step_background(job_id, "convert/data-model", lambda: convert_data_model(app_name))


def start_convert_sheet(job_id: str, app_name: str) -> None:
    start_step_background(job_id, "convert/sheet", lambda: convert_sheet(app_name))


def start_convert_all(job_id: str, app_name: str) -> None:
    start_step_background(job_id, "convert", lambda: convert_all(app_name))


__all__ = [
    "start_convert_script",
    "start_convert_data_model",
    "start_convert_sheet",
    "start_convert_all",
]
