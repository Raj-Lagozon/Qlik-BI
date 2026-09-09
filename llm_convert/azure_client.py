"""Thin wrapper around Azure OpenAI's OpenAI-compatible v1 Responses API."""

from __future__ import annotations

import json
import os

from openai import OpenAI


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
    system prompt and the extracted data as the user message."""
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
