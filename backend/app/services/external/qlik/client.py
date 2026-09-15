"""Qlik Cloud access: REST API for app import, Engine API (WebSocket JSON-RPC)
for reading a session app's script, data model, master items, sheets."""

from __future__ import annotations

import itertools
import json
import os
from typing import Any, Optional
from urllib.parse import urlparse

import requests
import websocket  # websocket-client


class EngineApiError(RuntimeError):
    def __init__(self, req_id: int, error: dict):
        self.code = error.get("code")
        self.parameter = error.get("parameter")
        super().__init__(f"Engine API error for id={req_id}: {error}")


class QlikCloudClient:
    def __init__(
        self,
        tenant_url: Optional[str] = None,
        api_key: Optional[str] = None,
        space_id: Optional[str] = None,
    ):
        self.tenant_url = (tenant_url or os.environ["QLIK_TENANT_URL"]).rstrip("/")
        self.api_key = api_key or os.environ["QLIK_API_KEY"]
        # Personal/root space usually rejects app import via API key (403);
        # importing into a shared/managed space id works. Optional because a
        # tenant with API-key import enabled on the personal space needs none.
        self.space_id = space_id or os.environ.get("QLIK_SPACE_ID")
        self._headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ── REST: import a local .qvf into the tenant ──────────────────────────

    def import_app(self, qvf_path: str, name: Optional[str] = None) -> str:
        """Upload a local .qvf file to Qlik Cloud and return the new app id.

        The apps/import endpoint takes the raw .qvf bytes as the request body
        (Content-Type: application/octet-stream) — NOT a multipart/form-data
        upload, which the API rejects with 415 Unsupported Media Type.
        """
        name = name or os.path.splitext(os.path.basename(qvf_path))[0]
        url = f"{self.tenant_url}/api/v1/apps/import"
        params = {"name": name}
        if self.space_id:
            params["spaceId"] = self.space_id
        with open(qvf_path, "rb") as fh:
            resp = requests.post(
                url,
                headers={
                    "Authorization": self._headers["Authorization"],
                    "Content-Type": "application/octet-stream",
                },
                params=params,
                data=fh,
                timeout=300,
            )
        if resp.status_code == 403:
            raise PermissionError(
                "403 Forbidden importing the app. Qlik Cloud API keys usually "
                "can't import into the personal space — set QLIK_SPACE_ID in "
                ".env (or pass --space-id) to a space id your API key's owner "
                "has 'can create apps' access to. Find space ids in the "
                "Qlik Cloud hub URL when a space is open (…/spaces/<id>) or "
                "via GET /api/v1/spaces.\n"
                f"Raw response: {resp.text}"
            )
        resp.raise_for_status()
        data = resp.json()
        return data["attributes"]["id"] if "attributes" in data else data["id"]

    def delete_app(self, app_id: str) -> None:
        """Remove an imported app from the tenant. extract_app calls this
        once extraction is done — the .qvf is only imported to read it back
        through the Engine API, it isn't meant to accumulate in Qlik Cloud."""
        url = f"{self.tenant_url}/api/v1/apps/{app_id}"
        resp = requests.delete(url, headers={"Authorization": self._headers["Authorization"]}, timeout=60)
        if resp.status_code not in (200, 204, 404):
            resp.raise_for_status()

    # ── Engine API session ──────────────────────────────────────────────────

    def open_engine_session(self, app_id: str) -> "EngineSession":
        host = urlparse(self.tenant_url).netloc
        ws_url = f"wss://{host}/app/{app_id}"
        return EngineSession(ws_url, self.api_key)


class EngineSession:
    """Minimal synchronous JSON-RPC client for the Qlik Associative Engine API."""

    def __init__(self, ws_url: str, api_key: str):
        self._ws = websocket.create_connection(
            ws_url,
            header=[f"Authorization: Bearer {api_key}"],
            timeout=60,
        )
        self._id_counter = itertools.count(1)
        # Engine sends an OnConnected notification first; drain it.
        self._recv_until_response(None)
        self.doc_handle: Optional[int] = None

    def close(self) -> None:
        try:
            self._ws.close()
        except Exception:
            pass

    def __enter__(self) -> "EngineSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _send(self, method: str, handle: int, params: Any) -> dict:
        req_id = next(self._id_counter)
        payload = {"jsonrpc": "2.0", "id": req_id, "handle": handle, "method": method, "params": params}
        self._ws.send(json.dumps(payload))
        return self._recv_until_response(req_id)

    def _recv_until_response(self, req_id: Optional[int]) -> dict:
        while True:
            raw = self._ws.recv()
            msg = json.loads(raw)
            if req_id is None:
                return msg
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise EngineApiError(req_id, msg["error"])
                return msg.get("result", {})
            # notification (e.g. OnConnected/OnMaxParallelSessionsExceeded) — ignore and keep waiting

    def global_call(self, method: str, params: Any = None) -> dict:
        return self._send(method, -1, params or [])

    def open_doc(self, app_id: str) -> int:
        result = self.global_call("OpenDoc", [app_id])
        self.doc_handle = result["qReturn"]["qHandle"]
        return self.doc_handle

    def doc_call(self, method: str, params: Any = None) -> dict:
        if self.doc_handle is None:
            raise RuntimeError("Call open_doc() before doc_call().")
        return self._send(method, self.doc_handle, params or [])

    def create_session_object(self, obj_def: dict) -> tuple[int, dict]:
        """Create a session object, return (handle, layout)."""
        result = self.doc_call("CreateSessionObject", [obj_def])
        handle = result["qReturn"]["qHandle"]
        layout = self.object_call(handle, "GetLayout", [])
        return handle, layout["qLayout"]

    def object_call(self, handle: int, method: str, params: Any = None) -> dict:
        return self._send(method, handle, params or [])

    def get_object(self, obj_id: str) -> Optional[int]:
        result = self.doc_call("GetObject", [obj_id])
        handle = result.get("qReturn", {}).get("qHandle")
        return handle if result.get("qReturn", {}).get("qType") else handle
