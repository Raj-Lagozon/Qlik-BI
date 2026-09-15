"""Parse a Qlik load script's SECTION ACCESS block(s) so the RLS converter has
something structured to work from (raw script text is a bad LLM input for this
specific 2.8 item since exact field/user semantics matter)."""

from __future__ import annotations

import re

_SECTION_RE = re.compile(
    r"SECTION\s+ACCESS\s*;(.*?)(?=SECTION\s+APPLICATION\s*;|$)",
    re.IGNORECASE | re.DOTALL,
)
_LOAD_RE = re.compile(r"LOAD\b(.*?);", re.IGNORECASE | re.DOTALL)
_FIELD_RE = re.compile(r"^\s*([A-Za-z_][\w]*)\s*(?:,|$)", re.MULTILINE)


def parse_section_access(script: str) -> dict:
    match = _SECTION_RE.search(script)
    if not match:
        return {"present": False}

    block = match.group(1)
    load_match = _LOAD_RE.search(block)
    fields: list[str] = []
    if load_match:
        field_list = load_match.group(1)
        # Strip inline resident/from clauses if present on the same LOAD line.
        field_list = re.split(r"\bFROM\b|\bRESIDENT\b", field_list, flags=re.IGNORECASE)[0]
        fields = [f.strip().strip("[]") for f in field_list.split(",") if f.strip()]

    return {
        "present": True,
        "raw_block": block.strip(),
        "fields": fields,
        "has_userid": any(f.upper() == "USERID" for f in fields),
        "has_omit": "OMIT" in block.upper(),
        "has_reduction": "REDUCTION" in block.upper(),
    }
