"""Shared LLM plumbing used by every conversion module (script, data_model,
sheet): the Azure OpenAI call itself, plus loading a skill.md / extracted
JSON and writing converted JSON — kept in one place so every module talks to
the LLM and the filesystem the exact same way.
"""

from __future__ import annotations

import json
import os
import re

from openai import OpenAI

from app.setting import settings

SKILLS_DIR = str(settings.backend_dir / "app" / "prompts")
EXTRACTED_ROOT = str(settings.project_root / "extracted")
CONVERTED_ROOT = str(settings.project_root / "converted")


# ---------------------------------------------------------------------------
# Azure OpenAI call
# ---------------------------------------------------------------------------

def get_client() -> OpenAI:
    endpoint = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    # Accept the endpoint with or without the "/openai/v1" suffix already
    # included, so a value copied straight from the Azure portal (which
    # usually already has it) doesn't end up doubled into ".../openai/v1/openai/v1".
    if not endpoint.endswith("/openai/v1"):
        endpoint = f"{endpoint}/openai/v1"
    api_key = os.environ["AZURE_OPENAI_API_KEY"]
    return OpenAI(base_url=f"{endpoint}/", api_key=api_key)


def run_skill(skill_prompt: str, user_payload: dict | str, *, json_output: bool = True) -> dict | str:
    """Call the configured Azure OpenAI deployment with a skill.md file as the
    system prompt and the extracted data (plus a "task" field selecting
    which section of that skill.md applies) as the user message."""
    client = get_client()
    deployment = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4")

    user_content = user_payload if isinstance(user_payload, str) else json.dumps(user_payload, indent=2)

    kwargs = {}
    if json_output:
        kwargs["text"] = {"format": {"type": "json_object"}}

    response = client.responses.create(
        model=deployment,
        input=[
            {"type": "message", "role": "system", "content": skill_prompt},
            {"type": "message", "role": "user", "content": user_content},
        ],
        **kwargs,
    )
    content = response.output_text
    return json.loads(content) if json_output else content


# ---------------------------------------------------------------------------
# skill.md / extracted / converted I/O
# ---------------------------------------------------------------------------

def load_skill(filename: str) -> str:
    with open(os.path.join(SKILLS_DIR, filename), encoding="utf-8") as f:
        return f.read()


_EXIT_SCRIPT_RE = re.compile(r"\bexit\s+script\b", re.IGNORECASE)


def load_extracted(app_name: str, filename: str):
    path = os.path.join(EXTRACTED_ROOT, app_name, filename)
    if filename.endswith(".json"):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if filename == "script.qvs":
        # Qlik stops executing at its first `Exit Script` statement —
        # anything after it (draft/disabled LOAD blocks, notes, an old
        # version kept "just in case") never actually ran in the real app
        # and has no bearing on its real data model. Feeding that dead tail
        # to the LLM risks it converting a table/column that looks real but
        # was never live — same reasoning as project.py's own
        # _truncate_at_exit_script, applied here for the LLM-driven M-query
        # conversion path (see modules/script/converter.py), which reads
        # script.qvs through this function rather than project.py's regex
        # detectors.
        m = _EXIT_SCRIPT_RE.search(text)
        if m:
            text = text[:m.start()]
    return text


def write_converted(app_name: str, filename: str, data) -> str:
    out_dir = os.path.join(CONVERTED_ROOT, app_name)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    with open(path, "w", encoding="utf-8") as f:
        if filename.endswith(".json"):
            json.dump(data, f, indent=2, ensure_ascii=False)
        else:
            f.write(data)
    print(f"[convert]   -> wrote converted/{app_name}/{filename}")
    return path


# ---------------------------------------------------------------------------
# Cross-module confidence-flag log
# ---------------------------------------------------------------------------

# Every skill's output items carry a "confidence": "high"|"medium"|"low"
# field (see each skill.md's Output section) — the LLM's own judgment of how
# certain it is about that one translation, separate from and in addition to
# the code-level checks in modules/build (hallucinated entity names,
# unresolved measures, etc). Collected here across every conversion call so
# the convert endpoint can print one batch summary — a low-confidence item
# might still build and render fine, so this is a review flag, not a build
# blocker.
confidence_log: list[dict] = []


def collect_confidence(source: str, items: list[dict]) -> None:
    for item in items:
        if not isinstance(item, dict):
            # Defensive: an LLM response can occasionally include a
            # malformed (non-object) entry in an items list — every caller
            # of this function is expected to have already filtered these
            # out, but this is shared plumbing several conversion modules
            # call, so it shouldn't crash the whole conversion over one
            # caller that didn't.
            continue
        confidence = item.get("confidence")
        if confidence in ("medium", "low"):
            confidence_log.append({
                "source": source,
                "name": item.get("name") or item.get("title") or "<unnamed>",
                "confidence": confidence,
                "notes": item.get("notes") or item.get("description") or "",
            })


def print_confidence_summary() -> None:
    if not confidence_log:
        print("[convert] all converted items reported high confidence")
        return
    low = [x for x in confidence_log if x["confidence"] == "low"]
    medium = [x for x in confidence_log if x["confidence"] == "medium"]
    print(f"[convert] confidence flags: {len(medium)} medium, {len(low)} low — review before trusting the build")
    for item in low + medium:
        note = f" — {item['notes']}" if item["notes"] else ""
        print(f"[convert]   [{item['confidence'].upper()}] {item['source']}: '{item['name']}'{note}")


# ---------------------------------------------------------------------------
# Misc helpers shared by conversion modules
# ---------------------------------------------------------------------------

def safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


def strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()
