"""Local OTLP receiver (stdlib only).

Accepts OTLP/HTTP JSON POSTs to /v1/traces on localhost, stores decoded
payloads in memory and appends them to a JSONL file for assertions.

This is Milestone 1's test receiver only. It is NOT a production collector:
no auth, no TLS, localhost only, unbounded in-memory storage.

Usage as a script:
    python receiver.py [--port 0] [--out out/received.jsonl]
    # prints the listening URL, serves until Ctrl-C

Usage in-process (tests):
    from receiver import LocalReceiver
    recv = LocalReceiver().start()
    endpoint = recv.endpoint  # http://127.0.0.1:<port>/v1/traces
    ... run the coordinator / fixture against endpoint ...
    spans = recv.spans  # flattened list of span dicts
    recv.stop()
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _attrs_to_dict(attrs):
    """Convert an OTLP attribute list to a plain {key: value} dict."""
    out = {}
    for attr in attrs or []:
        key = attr.get("key")
        value = attr.get("value") or {}
        if "stringValue" in value:
            out[key] = value["stringValue"]
        elif "intValue" in value:
            try:
                out[key] = int(value["intValue"])
            except (TypeError, ValueError):
                out[key] = value["intValue"]
        elif "boolValue" in value:
            out[key] = bool(value["boolValue"])
        elif "doubleValue" in value:
            out[key] = float(value["doubleValue"])
        elif "arrayValue" in value:
            out[key] = str(value["arrayValue"])
        else:
            out[key] = None
    return out


def flatten_payload(payload):
    """Flatten an OTLP/HTTP JSON payload into a list of span dicts."""
    spans = []
    for resource_span in payload.get("resourceSpans") or []:
        resource_attrs = _attrs_to_dict(
            (resource_span.get("resource") or {}).get("attributes")
        )
        for scope_span in resource_span.get("scopeSpans") or []:
            scope = scope_span.get("scope") or {}
            for span in scope_span.get("spans") or []:
                status = span.get("status") or {}
                spans.append(
                    {
                        "traceId": span.get("traceId"),
                        "spanId": span.get("spanId"),
                        "parentSpanId": span.get("parentSpanId"),
                        "name": span.get("name"),
                        "kind": span.get("kind"),
                        "startTimeUnixNano": span.get("startTimeUnixNano"),
                        "endTimeUnixNano": span.get("endTimeUnixNano"),
                        "attributes": _attrs_to_dict(span.get("attributes")),
                        "status": {
                            "code": status.get("code", 0),
                            "message": status.get("message"),
                        },
                        "resource": resource_attrs,
                        "scope": {
                            "name": scope.get("name"),
                            "version": scope.get("version"),
                        },
                    }
                )
    return spans


class LocalReceiver:
    """A localhost OTLP/HTTP JSON receiver for tests and demos."""

    def __init__(self, host="127.0.0.1", port=0, out_path=None):
        self.host = host
        self.port = port
        self.out_path = Path(out_path) if out_path else None
        self._lock = threading.Lock()
        self.payloads = []  # decoded JSON payloads, in arrival order
        self.spans = []  # flattened span dicts, in arrival order
        self._server = None
        self._thread = None

    @property
    def endpoint(self):
        return f"http://{self.host}:{self.port}/v1/traces"

    def _handle_payload(self, payload):
        with self._lock:
            self.payloads.append(payload)
            self.spans.extend(flatten_payload(payload))
        if self.out_path is not None:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            with self.out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")

    def start(self):
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output clean
                pass

            def _send_json(self, code, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                if self.path != "/v1/traces":
                    self._send_json(404, {"error": "not found"})
                    return
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {"error": "invalid JSON"})
                    return
                if not isinstance(payload, dict) or "resourceSpans" not in payload:
                    self._send_json(400, {"error": "not an OTLP traces payload"})
                    return
                receiver._handle_payload(payload)
                self._send_json(200, {})

            def do_GET(self):
                if self.path in ("/", "/health"):
                    with receiver._lock:
                        n_spans = len(receiver.spans)
                        n_payloads = len(receiver.payloads)
                    self._send_json(
                        200,
                        {
                            "status": "ok",
                            "spans_received": n_spans,
                            "payloads_received": n_payloads,
                        },
                    )
                    return
                self._send_json(404, {"error": "not found"})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        return self

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Local OTLP/HTTP JSON receiver")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--out", default="out/received.jsonl")
    args = parser.parse_args()

    recv = LocalReceiver(host=args.host, port=args.port, out_path=args.out).start()
    print(f"listening on {recv.endpoint} (health: http://{args.host}:{recv.port}/health)")
    print(f"appending payloads to {args.out}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        recv.stop()
        print(f"stopped; received {len(recv.spans)} spans in {len(recv.payloads)} payloads")
