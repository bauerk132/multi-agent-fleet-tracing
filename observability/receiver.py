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
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MAX_BODY_BYTES = 16 * 1024 * 1024


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
                        "links": [
                            {
                                "traceId": link.get("traceId"),
                                "spanId": link.get("spanId"),
                                "attributes": _attrs_to_dict(link.get("attributes")),
                            }
                            for link in (span.get("links") or [])
                        ],
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
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("unsupported bind host")
        self.host = "127.0.0.1" if host == "localhost" else host
        self.port = port
        self.out_path = Path(out_path) if out_path else None
        self._lock = threading.Lock()
        self.payloads = []  # decoded JSON payloads, in arrival order
        self.spans = []  # flattened span dicts, in arrival order
        # Rejected POSTs to /v1/traces (metadata only: content-type +
        # length, never raw bytes). A real agent exporting protobuf hits
        # this; the harness reports it as a transport mismatch instead
        # of silently losing the spans.
        self.rejected = []
        self._server = None
        self._thread = None

    @property
    def endpoint(self):
        display_host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{display_host}:{self.port}/v1/traces"

    def _handle_payload(self, payload):
        with self._lock:
            flattened = flatten_payload(payload)
            self.payloads.append(payload)
            self.spans.extend(flattened)
        if self.out_path is not None:
            self.out_path.parent.mkdir(parents=True, exist_ok=True)
            with self.out_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")

    def start(self):
        bind_host = self.host
        if bind_host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError("unsupported bind host")
        if bind_host == "localhost":
            bind_host = "127.0.0.1"
        self.host = bind_host
        receiver = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output clean
                pass

            def handle_one_request(self):
                self._preflight_done = False
                self._preflight_accepted = False
                self._validated_length = None
                self._normalized_length = None
                self._declared_body_too_large = False
                self._encoding_metadata = None
                self._media_type_metadata = None
                return super().handle_one_request()

            def _send_json(self, code, obj):
                body = json.dumps(obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record_rejection(self, length, reason, encoding=None):
                with receiver._lock:
                    receiver.rejected.append({
                        "content_type": self._media_type_metadata,
                        "length": length,
                        "encoding": encoding if encoding is not None else self._encoding_metadata,
                        "reason": reason,
                    })

            def _refuse(self, code, reason, length=None):
                self.close_connection = True
                if length is None:
                    length = self._normalized_length
                self._record_rejection(length, reason)
                self._send_json(code, {"error": reason})
                return False

            def _preflight(self):
                """Validate HTTP framing and headers before reading the body."""
                values = self.headers.get_all("Content-Length", [])
                if len(values) == 1:
                    raw_length = values[0].strip(" \t")
                    if raw_length and re.fullmatch(r"[0-9]+", raw_length, flags=re.ASCII) is not None:
                        significant = raw_length.lstrip("0") or "0"
                        cap_digits = str(MAX_BODY_BYTES)
                        if len(significant) > 18:
                            self._normalized_length = "over_range"
                            self._declared_body_too_large = True
                        else:
                            self._declared_body_too_large = (
                                len(significant) > len(cap_digits)
                                or (
                                    len(significant) == len(cap_digits)
                                    and significant > cap_digits
                                )
                            )
                            self._normalized_length = int(significant)

                encodings = self.headers.get_all("Content-Encoding", [])
                if not encodings:
                    self._encoding_metadata = None
                elif len(encodings) != 1:
                    self._encoding_metadata = "unsupported"
                else:
                    encoding = encodings[0].strip(" \t").lower()
                    self._encoding_metadata = (
                        encoding if encoding in ("identity", "gzip") else "unsupported"
                    )

                types = self.headers.get_all("Content-Type", [])
                if not types:
                    self._media_type_metadata = None
                elif len(types) != 1:
                    self._media_type_metadata = "unsupported"
                else:
                    media_type = types[0].split(";", 1)[0].strip(" \t").lower()
                    self._media_type_metadata = (
                        media_type
                        if media_type in ("application/json", "application/x-protobuf")
                        else "unsupported"
                    )

                transfer_encoding = self.headers.get_all("Transfer-Encoding", [])
                if transfer_encoding:
                    return self._refuse(411, "unsupported_transfer_encoding")

                if not values:
                    return self._refuse(411, "length_required")
                if len(values) != 1:
                    return self._refuse(411, "invalid_length")

                raw_length = values[0].strip(" \t")
                if not raw_length or re.fullmatch(r"[0-9]+", raw_length, flags=re.ASCII) is None:
                    return self._refuse(411, "invalid_length")
                significant = raw_length.lstrip("0") or "0"
                if self._declared_body_too_large:
                    return self._refuse(413, "body_too_large")
                length = int(significant)
                self._validated_length = length
                self._normalized_length = length

                if encodings and self._encoding_metadata != "identity":
                    return self._refuse(415, "unsupported_encoding")

                if not types or len(types) != 1 or self._media_type_metadata != "application/json":
                    return self._refuse(415, "unsupported_content_type")
                return True

            def handle_expect_100(self):
                if self.command != "POST":
                    self.send_response_only(100)
                    self.end_headers()
                    return True
                if self.path != "/v1/traces":
                    self.close_connection = True
                    self._send_json(404, {"error": "not found"})
                    return False
                self._preflight_done = True
                self._preflight_accepted = self._preflight()
                if not self._preflight_accepted:
                    return False
                self.send_response_only(100)
                self.end_headers()
                return True

            def do_POST(self):
                if self.path != "/v1/traces":
                    self.close_connection = True
                    self._send_json(404, {"error": "not found"})
                    return
                if not getattr(self, "_preflight_done", False):
                    self._preflight_done = True
                    self._preflight_accepted = self._preflight()
                if not self._preflight_accepted:
                    return
                length = self._validated_length
                raw = self.rfile.read(length) if length else b""
                if len(raw) != length:
                    self._refuse(400, "incomplete_body", length)
                    return
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError, RecursionError):
                    self._record_rejection(length, "invalid_json")
                    self.close_connection = True
                    self._send_json(400, {"error": "invalid JSON"})
                    return
                if not isinstance(payload, dict) or "resourceSpans" not in payload:
                    self._record_rejection(length, "invalid_payload")
                    self.close_connection = True
                    self._send_json(400, {"error": "not an OTLP traces payload"})
                    return
                try:
                    receiver._handle_payload(payload)
                except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                    self._record_rejection(length, "invalid_payload")
                    self.close_connection = True
                    self._send_json(400, {"error": "not an OTLP traces payload"})
                    return
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

        if bind_host == "::1":
            class IPv6ThreadingHTTPServer(ThreadingHTTPServer):
                address_family = socket.AF_INET6

            server_type = IPv6ThreadingHTTPServer
        else:
            server_type = ThreadingHTTPServer
        self._server = server_type((bind_host, self.port), Handler)
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

    try:
        recv = LocalReceiver(host=args.host, port=args.port, out_path=args.out).start()
    except ValueError:
        parser.error("unsupported bind host")
    display_host = f"[{recv.host}]" if ":" in recv.host else recv.host
    print(f"listening on {recv.endpoint} (health: http://{display_host}:{recv.port}/health)")
    print(f"appending payloads to {args.out}")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        recv.stop()
        print(f"stopped; received {len(recv.spans)} spans in {len(recv.payloads)} payloads")
