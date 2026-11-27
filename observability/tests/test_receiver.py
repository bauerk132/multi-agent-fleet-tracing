import contextlib
import io
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from unittest.mock import patch

from receiver import LocalReceiver


def _receive_response(sock):
    data = bytearray()
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    header, body = bytes(data).split(b"\r\n\r\n", 1)
    declared = 0
    for line in header.split(b"\r\n")[1:]:
        if line.lower().startswith(b"content-length:"):
            declared = int(line.split(b":", 1)[1].strip())
            break
    data = bytearray(body)
    while len(data) < declared:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return header + b"\r\n\r\n" + bytes(data)


class ReceiverBodyBoundsTests(unittest.TestCase):
    def setUp(self):
        self.receiver = LocalReceiver().start()

    def tearDown(self):
        self.receiver.stop()

    def _post_request(self, headers, body):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(2)
        try:
            lines = ["POST /v1/traces HTTP/1.1", "Host: 127.0.0.1"]
            lines.extend(headers)
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
            return _receive_response(sock).split(b"\r\n", 1)[0]
        finally:
            sock.close()

    def _post_headers(self, headers):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(1)
        try:
            lines = ["POST /v1/traces HTTP/1.1", "Host: 127.0.0.1"]
            lines.extend(headers)
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            return _receive_response(sock).split(b"\r\n", 1)[0]
        finally:
            sock.close()

    def test_missing_content_length_is_rejected_before_body_read(self):
        status = self._post_headers(["Content-Type: application/json"])
        self.assertIn(b" 411 ", status)

    def test_transfer_encoding_is_rejected_before_body_read(self):
        status = self._post_headers([
            "Content-Type: application/json", "Transfer-Encoding: chunked"
        ])
        self.assertIn(b" 411 ", status)

    def test_invalid_content_length_is_rejected_before_body_read(self):
        for value in ("", "+1", "-1", "1.0", "1, 1"):
            with self.subTest(value=value):
                status = self._post_headers([
                    "Content-Type: application/json", f"Content-Length: {value}"
                ])
                self.assertIn(b" 411 ", status)
                self.assertEqual(self.receiver.rejected[-1], {
                    "content_type": "application/json",
                    "length": None,
                    "encoding": None,
                    "reason": "invalid_length",
                })

    def test_declared_oversize_is_rejected_before_body_read(self):
        status = self._post_headers([
            "Content-Type: application/json", "Content-Length: 16777217"
        ])
        self.assertIn(b" 413 ", status)

    def test_small_patched_cap_accepts_exact_limit_and_rejects_plus_one(self):
        with patch("receiver.MAX_BODY_BYTES", 3):
            exact = self._post_request(["Content-Type: application/json", "Content-Length: 3"], b"{} ")
            leading_zero = self._post_request(["Content-Type: application/json", "Content-Length: 0003"], b"{} ")
            over = self._post_headers(["Content-Type: application/json", "Content-Length: 4"])
        self.assertIn(b" 400 ", exact)
        self.assertIn(b" 400 ", leading_zero)
        self.assertIn(b" 413 ", over)

    def test_premature_eof_is_rejected_before_json_parse(self):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(2)
        try:
            sock.sendall(("POST /v1/traces HTTP/1.1\r\nHost: localhost\r\n"
                          "Content-Type: application/json\r\nContent-Length: 5\r\n\r\n{}").encode("ascii"))
            sock.shutdown(socket.SHUT_WR)
            response = sock.recv(4096)
            self.assertIn(b" 400 ", response)
        finally:
            sock.close()

    def test_zero_length_reaches_json_validation(self):
        status = self._post_headers([
            "Content-Type: application/json", "Content-Length: 0"
        ])
        self.assertIn(b" 400 ", status)

    def test_expect_refusal_is_final_without_provisional_continue(self):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(1)
        try:
            sock.sendall((
                "POST /v1/traces HTTP/1.1\r\nHost: localhost\r\n"
                "Content-Length: 16777217\r\nExpect: 100-continue\r\n\r\n"
            ).encode("ascii"))
            response = _receive_response(sock)
            self.assertNotIn(b"100 Continue", response)
            self.assertIn(b" 413 ", response)
        finally:
            sock.close()


class ReceiverEncodingTests(unittest.TestCase):
    def setUp(self):
        self.receiver = LocalReceiver().start()

    def tearDown(self):
        self.receiver.stop()

    def _send(self, headers, body=b'{"resourceSpans": []}', declared_length=None,
              content_types=("application/json",)):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(2)
        try:
            lines = ["POST /v1/traces HTTP/1.1", "Host: localhost"]
            lines.extend(f"Content-Type: {value}" for value in content_types)
            lines.extend(headers)
            length = len(body) if declared_length is None else declared_length
            lines.append(f"Content-Length: {length}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + (body or b""))
            return _receive_response(sock).split(b"\r\n", 1)[0]
        finally:
            sock.close()

    def test_absent_encoding_accepts_valid_json(self):
        self.assertIn(b" 200 ", self._send([]))

    def test_identity_encoding_is_case_insensitive(self):
        self.assertIn(b" 200 ", self._send(["Content-Encoding: IdEnTiTy"]))

    def test_gzip_is_rejected_before_reading_body(self):
        self.assertIn(b" 415 ", self._send(["Content-Encoding: gzip"], body=None, declared_length=128))

    def test_unknown_encoding_is_rejected_before_reading_body(self):
        self.assertIn(b" 415 ", self._send(["Content-Encoding: compress"], body=None, declared_length=128))

    def test_repeated_encoding_is_rejected_before_reading_body(self):
        self.assertIn(b" 415 ", self._send([
            "Content-Encoding: identity", "Content-Encoding: gzip"
        ], body=None, declared_length=128))

    def test_blank_encoding_is_rejected_before_reading_body(self):
        self.assertIn(b" 415 ", self._send(["Content-Encoding:"], body=None, declared_length=128))

    def test_persistent_connection_revalidates_each_post(self):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(2)
        try:
            body = b'{"resourceSpans": []}'
            first = ("POST /v1/traces HTTP/1.1\r\nHost: localhost\r\n"
                     "Content-Type: application/json\r\n"
                     f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body
            sock.sendall(first)
            data = bytearray()
            while b"\r\n\r\n" not in data:
                data.extend(sock.recv(4096))
            header, rest = bytes(data).split(b"\r\n\r\n", 1)
            while len(rest) < 2:
                rest += sock.recv(4096)
            self.assertIn(b" 200 ", header.split(b"\r\n", 1)[0])
            sock.sendall(b"POST /v1/traces HTTP/1.1\r\nHost: localhost\r\n\r\n")
            response = sock.recv(4096)
            self.assertIn(b" 411 ", response)
        finally:
            sock.close()


class ReceiverMediaTypeTests(unittest.TestCase):
    def setUp(self):
        self.receiver = LocalReceiver().start()

    def tearDown(self):
        self.receiver.stop()

    def _send(self, content_types, body=None, declared_length=128):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(2)
        try:
            lines = ["POST /v1/traces HTTP/1.1", "Host: localhost"]
            lines.extend(f"Content-Type: {value}" for value in content_types)
            lines.append(f"Content-Length: {declared_length}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + (body or b""))
            return _receive_response(sock).split(b"\r\n", 1)[0]
        finally:
            sock.close()

    def test_missing_media_type_is_rejected_before_read(self):
        status = self._send([])
        self.assertIn(b" 415 ", status)

    def test_protobuf_is_rejected_before_read_and_preserved_as_metadata(self):
        status = self._send(["application/x-protobuf"])
        self.assertIn(b" 415 ", status)
        self.assertEqual(self.receiver.rejected[-1]["content_type"], "application/x-protobuf")

    def test_unsupported_media_type_is_rejected_before_read(self):
        status = self._send(["text/plain"])
        self.assertIn(b" 415 ", status)

    def test_repeated_media_type_is_rejected_before_read(self):
        status = self._send(["application/json", "application/json"])
        self.assertIn(b" 415 ", status)

    def test_transfer_encoding_precedes_other_unsupported_headers(self):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(2)
        try:
            sock.sendall(("POST /v1/traces HTTP/1.1\r\nHost: localhost\r\n"
                          "Transfer-Encoding: chunked\r\n"
                          "Content-Encoding: gzip\r\n"
                          "Content-Type: text/plain\r\n"
                          "Content-Length: 7\r\n\r\n").encode("ascii"))
            self.assertIn(b" 411 ", _receive_response(sock).split(b"\r\n", 1)[0])
            self.assertEqual(self.receiver.rejected[-1], {
                "content_type": "unsupported",
                "length": 7,
                "encoding": "gzip",
                "reason": "unsupported_transfer_encoding",
            })
        finally:
            sock.close()

    def test_json_and_json_with_charset_are_accepted(self):
        payload = b'{"resourceSpans": []}'
        for media in ("application/json", "application/json; charset=utf-8"):
            with self.subTest(media=media):
                status = self._send([media], body=payload, declared_length=len(payload))
                self.assertIn(b" 200 ", status)


class ReceiverBindTests(unittest.TestCase):
    def test_constructor_rejects_non_loopback_before_socket_creation(self):
        with patch("receiver.ThreadingHTTPServer") as server:
            for host in ("0.0.0.0", "192.0.2.1", "example.invalid"):
                with self.subTest(host=host):
                    with self.assertRaisesRegex(ValueError, "unsupported bind host"):
                        LocalReceiver(host=host)
            server.assert_not_called()

    def test_start_revalidates_a_mutated_host_before_server_creation(self):
        receiver = LocalReceiver()
        receiver.host = "0.0.0.0"
        with patch("receiver.ThreadingHTTPServer") as server:
            with self.assertRaisesRegex(ValueError, "unsupported bind host"):
                receiver.start()
            server.assert_not_called()

    def test_cli_rejects_non_loopback_without_echoing_host(self):
        marker = "198.51.100.77"
        result = subprocess.run(
            [sys.executable, "-B", "receiver.py", "--host", marker],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported bind host", result.stderr)
        self.assertNotIn(marker, result.stderr)

    def test_ipv4_loopback_hosts_have_healthy_endpoints(self):
        for host, endpoint_host in (("127.0.0.1", "127.0.0.1"), ("localhost", "127.0.0.1")):
            with self.subTest(host=host):
                receiver = LocalReceiver(host=host).start()
                try:
                    self.assertIn(f"http://{endpoint_host}:", receiver.endpoint)
                    with urllib.request.urlopen(receiver.endpoint.replace("/v1/traces", "/health"), timeout=2) as response:
                        self.assertEqual(response.status, 200)
                finally:
                    receiver.stop()

    def test_ipv6_loopback_uses_bracketed_endpoint_and_health_url(self):
        try:
            receiver = LocalReceiver(host="::1").start()
        except OSError as exc:
            self.skipTest(f"IPv6 loopback unavailable: {type(exc).__name__}")
        try:
            self.assertIn("http://[::1]:", receiver.endpoint)
            with urllib.request.urlopen(receiver.endpoint.replace("/v1/traces", "/health"), timeout=2) as response:
                self.assertEqual(response.status, 200)
        finally:
            receiver.stop()


class ReceiverPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sink = Path(self.tmp.name) / "received.jsonl"
        self.receiver = LocalReceiver(out_path=self.sink).start()

    def tearDown(self):
        self.receiver.stop()
        self.tmp.cleanup()

    def _post(self, headers, body):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(2)
        try:
            lines = ["POST /v1/traces HTTP/1.1", "Host: localhost"]
            lines.extend(headers)
            lines.append(f"Content-Length: {len(body)}")
            sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body)
            return _receive_response(sock)
        finally:
            sock.close()

    def test_refusal_records_only_normalized_metadata(self):
        marker = "SYNTHETIC-MARKER-493"
        body = ("{\"probe\":\"" + marker + "\"}").encode("utf-8")
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            response = self._post([
                "Content-Type: application/x-protobuf; note=" + marker,
                "X-Unrelated: " + marker,
            ], body)
        self.assertIn(b" 415 ", response.split(b"\r\n", 1)[0])
        self.assertEqual(self.receiver.rejected, [{
            "content_type": "application/x-protobuf",
            "length": len(body),
            "encoding": None,
            "reason": "unsupported_content_type",
        }])
        self.assertNotIn(marker.encode(), response)
        self.assertNotIn(marker, repr(self.receiver.rejected))
        self.assertEqual(stdout.getvalue(), "")
        self.assertNotIn(marker, stderr.getvalue())
        self.assertFalse(self.sink.exists())
        with urllib.request.urlopen(self.receiver.endpoint.replace("/v1/traces", "/health"), timeout=2) as health_response:
            health = health_response.read().decode("utf-8")
        self.assertNotIn(marker, health)

    def test_unsupported_encoding_records_only_fixed_metadata(self):
        marker = "SYNTHETIC-HEADER-209"
        body = marker.encode("ascii")
        response = self._post([
            "Content-Type: application/json; charset=" + marker,
            "Content-Encoding: gzip",
            "X-Unrelated: " + marker,
        ], body)
        self.assertIn(b" 415 ", response.split(b"\r\n", 1)[0])
        self.assertEqual(self.receiver.rejected[-1], {
            "content_type": "application/json",
            "length": len(body),
            "encoding": "gzip",
            "reason": "unsupported_encoding",
        })
        self.assertNotIn(marker.encode("ascii"), repr(self.receiver.rejected).encode())

    def test_extreme_length_uses_over_range_metadata_without_conversion(self):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(2)
        try:
            sock.sendall(("POST /v1/traces HTTP/1.1\r\nHost: localhost\r\n"
                          "Content-Type: application/json\r\n"
                          "Content-Length: 999999999999999999999999999999999999\r\n\r\n").encode("ascii"))
            response = _receive_response(sock)
            self.assertIn(b" 413 ", response.split(b"\r\n", 1)[0])
        finally:
            sock.close()
        self.assertEqual(self.receiver.rejected[-1], {
            "content_type": "application/json",
            "length": "over_range",
            "encoding": None,
            "reason": "body_too_large",
        })

    def test_json_recursion_error_returns_fixed_rejection(self):
        marker = "SYNTHETIC-JSON-811"
        body = ("{\"resourceSpans\":[],\"marker\":\"" + marker + "\"}").encode()
        with patch("receiver.json.loads", side_effect=RecursionError):
            response = self._post(["Content-Type: application/json"], body)
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
        self.assertNotIn(marker.encode(), response)
        self.assertEqual(self.receiver.rejected[-1], {
            "content_type": "application/json",
            "length": len(body),
            "encoding": None,
            "reason": "invalid_json",
        })

    def test_malformed_nested_payload_is_rejected_without_partial_state(self):
        body = b'{"resourceSpans":[{"scopeSpans":[{"spans":["nested-marker"]}]}]}'
        response = self._post(["Content-Type: application/json"], body)
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
        self.assertEqual(self.receiver.payloads, [])
        self.assertEqual(self.receiver.spans, [])
        self.assertFalse(self.sink.exists())
        self.assertEqual(self.receiver.rejected[0]["reason"], "invalid_payload")
        self.assertNotIn(b"nested-marker", response)

    def test_valid_synthetic_payload_is_written_and_health_is_count_only(self):
        marker = "SYNTHETIC-VALID-731"
        body = json.dumps({"resourceSpans": [{"scopeSpans": [{"spans": [{"name": marker}]}]}]}).encode()
        response = self._post(["Content-Type: application/json; charset=utf-8"], body)
        self.assertIn(b" 200 ", response.split(b"\r\n", 1)[0])
        self.assertNotIn(marker.encode(), response)
        self.assertEqual(self.sink.read_text(encoding="utf-8"), body.decode() + "\n")
        with urllib.request.urlopen(self.receiver.endpoint.replace("/v1/traces", "/health"), timeout=2) as health_response:
            health = json.loads(health_response.read().decode("utf-8"))
        self.assertEqual(health, {"status": "ok", "spans_received": 1, "payloads_received": 1})
        self.assertNotIn(marker, json.dumps(health))


if __name__ == "__main__":
    unittest.main()
