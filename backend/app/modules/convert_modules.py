"""Public entry point for the conversion side of the pipeline: one function
per domain module (script, data_model, sheet) plus `convert_all`, which runs
every domain in the same order the full pipeline always has. Each domain can
also be run standalone (its own API endpoint) for incremental/debug use.
"""

from __future__ import annotations

from app.modules import data_model as data_model_module
from app.modules import script as script_module
from app.modules import sheet as sheet_module
from app.utilities.llm import confidence_log, print_confidence_summary


def convert_script(app_name: str) -> dict:
    written = {}
    print(f"[convert] === {app_name}: m-queries (per table, from script.qvs) ===")
    written["m_queries"] = script_module.convert_m_queries(app_name)
    print(f"[convert] === {app_name}: variables / parameters (from script.qvs) ===")
    written["parameters"] = script_module.convert_parameters(app_name)
    return written


def convert_data_model(app_name: str) -> dict:
    written = {}
    print(f"[convert] === {app_name}: data model / relationships ===")
    written["data_model"] = data_model_module.convert_data_model(app_name)
    print(f"[convert] === {app_name}: section access / RLS ===")
    written["rls"] = data_model_module.convert_rls(app_name)
    return written


def convert_sheet(app_name: str) -> dict:
    written = {}
    print(f"[convert] === {app_name}: master measures ===")
    written["measures"] = sheet_module.convert_measures(app_name)
    print(f"[convert] === {app_name}: master dimensions ===")
    written["dimensions"] = sheet_module.convert_dimensions(app_name)
    print(f"[convert] === {app_name}: report sheets/visuals/charts ===")
    written["report"] = sheet_module.convert_report(app_name)
    print(f"[convert] === {app_name}: KPI containers ===")
    written["kpi_containers"] = sheet_module.convert_kpi_containers(app_name)
    print(f"[convert] === {app_name}: ad-hoc chart expressions ===")
    written["adhoc_expressions"] = sheet_module.convert_adhoc_expressions(app_name)
    return written


def convert_all(app_name: str) -> dict:
    """Run every domain converter for one extracted app, in the order the
    build step needs (script/data-model results feed the sheet conversion's
    variable/relationship lookups). Returns the paths of everything written
    under converted/<app_name>/."""
    confidence_log.clear()
    written = {}
    written.update(convert_script(app_name))
    written.update(convert_data_model(app_name))
    written.update(convert_sheet(app_name))
    print_confidence_summary()
    return written


__all__ = ["convert_script", "convert_data_model", "convert_sheet", "convert_all"]
