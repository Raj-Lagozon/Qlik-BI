"""Public entry point for the Qlik-extraction domain.

Re-exports from `app.modules.extract` (Qlik Cloud REST + Engine API client,
extractor, section-access parser) so callers outside `modules/` import one
flat name instead of reaching into the package.
"""

from __future__ import annotations

from app.modules.extract import QlikCloudClient, extract_app

__all__ = ["QlikCloudClient", "extract_app"]
