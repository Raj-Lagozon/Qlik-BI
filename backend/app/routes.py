"""Aggregates all domain routers for `main.py` to include as one import."""

from __future__ import annotations

from fastapi import APIRouter

from app.route_handlers.build_routes import router as build_router
from app.route_handlers.convert_routes import router as convert_router
from app.route_handlers.extract_routes import router as extract_router
from app.route_handlers.pipeline_routes import router as pipeline_router

router = APIRouter()
router.include_router(extract_router)
router.include_router(convert_router)
router.include_router(build_router)
router.include_router(pipeline_router)
