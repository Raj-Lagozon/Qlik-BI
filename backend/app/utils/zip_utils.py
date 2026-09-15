"""Zips a *.pbip project (the .pbip file + its .Report/ and .SemanticModel/
folders) into a single downloadable archive.

The .pbip file alone is just a pointer — Power BI Desktop needs the sibling
.Report/ and .SemanticModel/ folders next to it to open the project, and
neither carries the actual row data the way the compiled .pbix does (that
still needs a Refresh after opening, same as the .pbix). Bundling all three
into one zip is what makes ".pbip" downloadable as a single file at all.
"""

from __future__ import annotations

import os
import tempfile
import zipfile
from pathlib import Path


def zip_pbip_project(project_dir: Path, app_name: str) -> Path:
    pbip_file = project_dir / f"{app_name}.pbip"
    report_dir = project_dir / f"{app_name}.Report"
    semantic_dir = project_dir / f"{app_name}.SemanticModel"

    fd, zip_path_str = tempfile.mkstemp(suffix=".zip", prefix=f"{app_name}_pbip_")
    zip_path = Path(zip_path_str)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        if pbip_file.exists():
            zf.write(pbip_file, arcname=pbip_file.name)
        for folder in (report_dir, semantic_dir):
            if not folder.exists():
                continue
            for path in folder.rglob("*"):
                if path.is_file():
                    zf.write(path, arcname=str(path.relative_to(project_dir)))

    os.close(fd)
    return zip_path
