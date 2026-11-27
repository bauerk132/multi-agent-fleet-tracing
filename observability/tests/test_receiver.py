import socket
import unittest
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
        for value in ("+1", "-1", "1.0"):
            with self.subTest(value=value):
                status = self._post_headers([
                    "Content-Type: application/json", f"Content-Length: {value}"
                ])
                self.assertIn(b" 411 ", status)

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

    def _send(self, headers, body=b'{"resourceSpans": []}', declared_length=None):
        sock = socket.create_connection(("127.0.0.1", self.receiver.port), timeout=2)
        sock.settimeout(2)
        try:
            lines = ["POST /v1/traces HTTP/1.1", "Host: localhost", "Content-Type: application/json"]
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


if __name__ == "__main__":
    unittest.main()
