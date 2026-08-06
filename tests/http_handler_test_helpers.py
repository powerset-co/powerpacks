"""Exercise BaseHTTPRequestHandler classes without binding a TCP port."""

from __future__ import annotations

import http.client
import socket
import threading
import urllib.parse


class _Server:
    server_name = "127.0.0.1"
    server_port = 0


class InProcessHttpClient:
    def __init__(self, handler: type):
        self.handler = handler

    def _exchange(
        self,
        method: str,
        path: str,
        body: bytes,
        headers: dict[str, str],
    ) -> tuple[socket.socket, threading.Thread]:
        client, server = socket.socketpair()

        def serve() -> None:
            try:
                self.handler(server, ("127.0.0.1", 1), _Server())
            finally:
                server.close()

        request_headers = {
            "Host": "127.0.0.1",
            "Connection": "close",
            **headers,
        }
        if body:
            request_headers["Content-Length"] = str(len(body))
        raw = (
            f"{method} {path} HTTP/1.1\r\n"
            + "".join(f"{key}: {value}\r\n" for key, value in request_headers.items())
            + "\r\n"
        ).encode() + body
        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        client.sendall(raw)
        client.shutdown(socket.SHUT_WR)
        return client, thread

    def request(
        self,
        method: str,
        path: str,
        fields: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, str, bytes, dict[str, str]]:
        body = (
            urllib.parse.urlencode(fields or {}).encode()
            if fields is not None
            else b""
        )
        request_headers = dict(headers or {})
        if fields is not None:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        client, thread = self._exchange(method, path, body, request_headers)
        try:
            response = http.client.HTTPResponse(client)
            response.begin()
            response_body = response.read()
            return (
                response.status,
                response.getheader("Content-Type") or "",
                response_body,
                {key.lower(): value for key, value in response.getheaders()},
            )
        finally:
            client.close()
            thread.join(timeout=5)

    def read_until(self, path: str, marker: bytes) -> bytes:
        client, thread = self._exchange("GET", path, b"", {})
        client.settimeout(5)
        chunks: list[bytes] = []
        try:
            while marker not in b"".join(chunks):
                chunks.append(client.recv(4096))
            return b"".join(chunks)
        finally:
            client.close()
            thread.join(timeout=5)
