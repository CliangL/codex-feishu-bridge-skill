#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
import http.client
import urllib.parse


HOST = os.getenv("DEEPSEEK_PROXY_HOST", "127.0.0.1")
PORT = int(os.getenv("DEEPSEEK_PROXY_PORT", "48765"))
UPSTREAM = os.getenv("DEEPSEEK_UPSTREAM_BASE_URL", "https://api.deepseek.com").rstrip("/")
API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
DEFAULT_MODEL = os.getenv("DEEPSEEK_DEFAULT_MODEL", "deepseek-v4-flash")
MODEL_IDS = [
    item.strip()
    for item in os.getenv(
        "RESPONSES_PROXY_MODEL_IDS",
        "deepseek-v4-flash,deepseek-v4-pro",
    ).split(",")
    if item.strip()
]

UPSTREAM_PARSED = urllib.parse.urlparse(UPSTREAM)
UPSTREAM_SCHEME = (UPSTREAM_PARSED.scheme or "https").lower()
UPSTREAM_HOST = UPSTREAM_PARSED.hostname
UPSTREAM_PORT = UPSTREAM_PARSED.port or (443 if UPSTREAM_SCHEME == "https" else 80)
UPSTREAM_PATH_PREFIX = (UPSTREAM_PARSED.path or "").rstrip("/")


def _now() -> int:
    return int(time.time())


def _json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text is not None:
                    parts.append(str(text))
        return "\n".join(part for part in parts if part)
    if content is None:
        return ""
    return str(content)


def _responses_input_to_chat_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    instructions = _extract_text(payload.get("instructions"))
    if instructions:
        messages.append({"role": "system", "content": instructions})

    raw_input = payload.get("input", [])
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
        return messages
    if isinstance(raw_input, dict):
        raw_input = [raw_input]

    for item in raw_input if isinstance(raw_input, list) else []:
        if not isinstance(item, dict):
            messages.append({"role": "user", "content": str(item)})
            continue
        item_type = str(item.get("type") or "")
        role = str(item.get("role") or "").strip() or "user"
        if role == "developer":
            role = "system"
        if item_type == "function_call":
            call_id = str(item.get("call_id") or item.get("id") or f"call_{uuid.uuid4().hex[:12]}")
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or "{}"),
                            },
                        }
                    ],
                }
            )
            continue
        if item_type == "function_call_output":
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or item.get("id") or ""),
                    "content": _extract_text(item.get("output") or item.get("content")),
                }
            )
            continue
        if item_type == "reasoning":
            continue
        content = _extract_text(item.get("content") if "content" in item else item.get("text"))
        if content:
            messages.append({"role": role, "content": content})
    return messages or [{"role": "user", "content": ""}]


def _responses_tools_to_chat_tools(payload: dict[str, Any]) -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            tools.append(tool)
            continue
        name = str(tool.get("name") or "").strip()
        if not name:
            continue
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(tool.get("description") or ""),
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                },
            }
        )
    return tools


def _chat_payload_from_responses(payload: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    chat_payload: dict[str, Any] = {
        "model": payload.get("model") or DEFAULT_MODEL,
        "messages": _responses_input_to_chat_messages(payload),
        "stream": stream,
    }
    tools = _responses_tools_to_chat_tools(payload)
    if tools:
        chat_payload["tools"] = tools
        chat_payload["tool_choice"] = payload.get("tool_choice", "auto")
    if payload.get("temperature") is not None:
        chat_payload["temperature"] = payload["temperature"]
    if payload.get("max_output_tokens") is not None:
        chat_payload["max_tokens"] = payload["max_output_tokens"]
    return chat_payload


def _upstream_path(path: str) -> str:
    clean = "/" + path.lstrip("/")
    if UPSTREAM_PATH_PREFIX:
        return f"{UPSTREAM_PATH_PREFIX}{clean}"
    return clean


def _request_upstream(path: str, body: dict[str, Any], *, stream: bool):
    if not API_KEY:
        raise RuntimeError("DEEPSEEK_API_KEY is not set")
    if not UPSTREAM_HOST:
        raise RuntimeError("DEEPSEEK_UPSTREAM_BASE_URL is invalid")
    conn_cls = http.client.HTTPSConnection if UPSTREAM_SCHEME == "https" else http.client.HTTPConnection
    conn = conn_cls(UPSTREAM_HOST, UPSTREAM_PORT, timeout=600)
    encoded = _json_dumps(body).encode("utf-8")
    conn.request(
        "POST",
        _upstream_path(path),
        body=encoded,
        headers={
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if stream else "application/json",
        },
    )
    resp = conn.getresponse()
    if resp.status >= 400:
        detail = resp.read().decode("utf-8", errors="replace")
        raise ConnectionError(f"Upstream HTTP {resp.status}: {detail}")
    return _StreamableResponse(conn, resp)


class _StreamableResponse:
    """Wraps http.client.HTTPResponse to support context manager, .read(), and iteration,
    matching the interface of urllib.request.urlopen return values."""

    def __init__(self, conn: http.client.HTTPSConnection, resp: http.client.HTTPResponse) -> None:
        self._conn = conn
        self._resp = resp

    def __enter__(self) -> _StreamableResponse:
        return self

    def __exit__(self, *args: Any) -> None:
        self._resp.close()
        self._conn.close()

    def read(self) -> bytes:
        return self._resp.read()

    def __iter__(self) -> Any:
        return iter(self._resp)

    def __next__(self) -> bytes:
        return next(self._resp)

    @property
    def status(self) -> int:
        return self._resp.status


class Handler(BaseHTTPRequestHandler):
    server_version = "HermesResponsesProxy/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_json(self, code: int, obj: Any) -> None:
        data = _json_dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_sse(self, event: str, data: dict[str, Any]) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(f"data: {_json_dumps(data)}\n\n".encode("utf-8"))
        self.wfile.flush()

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        parsed = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send_json(200, {"ok": True})
            return
        if path in {"/models", "/v1/models"}:
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [{"id": model_id, "object": "model"} for model_id in MODEL_IDS],
                },
            )
            return
        self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path not in {"/responses", "/v1/responses"}:
            self._send_json(404, {"error": {"message": "not found"}})
            return
        try:
            payload = self._read_json()
            # Match the Responses API default: only stream when the client asks.
            stream = bool(payload.get("stream", False))
            chat_payload = _chat_payload_from_responses(payload, stream=stream)
            if stream:
                self._handle_stream(payload, chat_payload)
            else:
                self._handle_non_stream(payload, chat_payload)
        except ConnectionError as exc:
            self._send_json(502, {"error": {"message": str(exc)}})
        except (RuntimeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json(502, {"error": {"message": str(exc)}})

    def _handle_non_stream(self, payload: dict[str, Any], chat_payload: dict[str, Any]) -> None:
        with _request_upstream("/chat/completions", chat_payload, stream=False) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        output_text = message.get("content") or ""
        response_id = f"resp_{uuid.uuid4().hex}"
        self._send_json(
            200,
            {
                "id": response_id,
                "object": "response",
                "created_at": _now(),
                "model": payload.get("model") or DEFAULT_MODEL,
                "status": "completed",
                "output_text": output_text,
                "output": [
                    {
                        "id": f"msg_{uuid.uuid4().hex[:16]}",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": output_text}],
                    }
                ],
            },
        )

    def _handle_stream(self, payload: dict[str, Any], chat_payload: dict[str, Any]) -> None:
        response_id = f"resp_{uuid.uuid4().hex}"
        model = str(payload.get("model") or DEFAULT_MODEL)
        created = _now()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self._send_sse(
            "response.created",
            {
                "type": "response.created",
                "response": {"id": response_id, "object": "response", "created_at": created, "model": model, "status": "in_progress"},
            },
        )
        text_parts: list[str] = []
        item_open = False
        reasoning_seen = False
        item_id = f"msg_{uuid.uuid4().hex[:16]}"
        tool_items: dict[int, dict[str, Any]] = {}

        def ensure_message_item() -> None:
            nonlocal item_open
            if item_open:
                return
            item_open = True
            self._send_sse(
                "response.output_item.added",
                {"type": "response.output_item.added", "output_index": 0, "item": {"id": item_id, "type": "message", "role": "assistant", "content": []}},
            )
            self._send_sse(
                "response.content_part.added",
                {"type": "response.content_part.added", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": ""}},
            )

        try:
            with _request_upstream("/chat/completions", chat_payload, stream=True) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    choice = (chunk.get("choices") or [{}])[0]
                    finish_reason = choice.get("finish_reason")
                    delta = choice.get("delta") or {}
                    reasoning_content = delta.get("reasoning_content")
                    content = delta.get("content")
                    # Send reasoning as a separate content part so the UI shows "thinking..."
                    if reasoning_content and not reasoning_seen:
                        reasoning_seen = True
                        ensure_message_item()
                        self._send_sse(
                            "response.output_text.delta",
                            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "..."},
                        )
                    if content:
                        ensure_message_item()
                        text_parts.append(str(content))
                        self._send_sse(
                            "response.output_text.delta",
                            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": str(content)},
                        )
                    for tool_delta in delta.get("tool_calls") or []:
                        index = int(tool_delta.get("index", 0) or 0)
                        state = tool_items.setdefault(
                            index,
                            {
                                "id": f"fc_{uuid.uuid4().hex[:16]}",
                                "call_id": tool_delta.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                                "name": "",
                                "arguments": "",
                                "announced": False,
                            },
                        )
                        func = tool_delta.get("function") or {}
                        if tool_delta.get("id"):
                            state["call_id"] = tool_delta["id"]
                        if func.get("name"):
                            state["name"] = func["name"]
                        if not state["announced"] and state["name"]:
                            state["announced"] = True
                            self._send_sse(
                                "response.output_item.added",
                                {
                                    "type": "response.output_item.added",
                                    "output_index": index + 1,
                                    "item": {
                                        "id": state["id"],
                                        "type": "function_call",
                                        "call_id": state["call_id"],
                                        "name": state["name"],
                                        "arguments": "",
                                    },
                                },
                            )
                        if func.get("arguments"):
                            state["arguments"] += str(func["arguments"])
                            self._send_sse(
                                "response.function_call_arguments.delta",
                                {
                                    "type": "response.function_call_arguments.delta",
                                    "output_index": index + 1,
                                    "delta": str(func["arguments"]),
                                },
                            )

        except Exception as exc:
            # If upstream fails mid-stream, close gracefully instead of crashing
            try:
                self._send_sse(
                    "error",
                    {"type": "error", "error": {"message": f"Upstream error: {exc}", "type": "upstream_error"}},
                )
            except Exception:
                pass
            self._send_sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "object": "response",
                        "created_at": created,
                        "model": model,
                        "status": "failed",
                        "output": [{"id": item_id, "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "".join(text_parts)}]}],
                        "output_text": "".join(text_parts),
                    },
                },
            )
            self.close_connection = True
            return

        output: list[dict[str, Any]] = []
        full_text = "".join(text_parts)
        if item_open:
            msg_item = {
                "id": item_id,
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": full_text}],
            }
            self._send_sse(
                "response.output_text.done",
                {"type": "response.output_text.done", "output_index": 0, "content_index": 0, "text": full_text},
            )
            self._send_sse(
                "response.content_part.done",
                {"type": "response.content_part.done", "output_index": 0, "content_index": 0, "part": {"type": "output_text", "text": full_text}},
            )
            self._send_sse(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": 0, "item": msg_item},
            )
            output.append(msg_item)

        for index, state in sorted(tool_items.items()):
            item = {
                "id": state["id"],
                "type": "function_call",
                "call_id": state["call_id"],
                "name": state["name"],
                "arguments": state["arguments"],
            }
            self._send_sse(
                "response.function_call_arguments.done",
                {"type": "response.function_call_arguments.done", "output_index": index + 1, "arguments": state["arguments"]},
            )
            self._send_sse(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": index + 1, "item": item},
            )
            output.append(item)

        self._send_sse(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": response_id,
                    "object": "response",
                    "created_at": created,
                    "model": model,
                    "status": "completed",
                    "output": output,
                    "output_text": full_text,
                },
            },
        )
        self.close_connection = True


if __name__ == "__main__":
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"deepseek responses proxy listening on http://{HOST}:{PORT}", flush=True)
    httpd.serve_forever()
