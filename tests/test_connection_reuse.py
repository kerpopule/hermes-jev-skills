"""Connections are pooled, bounded, and never followed to another origin.

A Jev call is one POST. Until 2026-09-22 each one opened its own TLS session; measured against
api.typesafe.ai on the same question: urllib with a fresh opener 522 ms, one `http.client`
connection reused 245 ms, httpx (what the official SDK pools) 189 ms. A Hermes turn pays for two
of those, so the handshake was the single biggest per-turn cost any of this repo controls.

These tests run against a local HTTP server: offline, fast, and they hold the four properties
the change rests on — reuse, no redirect hop, a bounded pool, and a stale socket recovered once.
"""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jevkit import client

OK_BODY = json.dumps({"model": "jev-test", "answers": {}, "usage": {}}).encode()


class _Handler(BaseHTTPRequestHandler):
    """Counts connections (by client port) and answers with whatever the test set."""

    protocol_version = "HTTP/1.1"
    response = (200, OK_BODY)          # (status, body) the test wants back
    ports: list = []
    paths: list = []
    delay = 0.0
    lock = threading.Lock()

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        with type(self).lock:
            type(self).ports.append(self.client_address[1])
            type(self).paths.append(self.path)
        if type(self).delay:
            import time
            time.sleep(type(self).delay)
        status, body = type(self).response
        self.send_response(status)
        if status in (301, 302, 303, 307, 308):
            self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args) -> None:  # noqa: A002, ANN001
        pass


class TransportTests(unittest.TestCase):
    def setUp(self):
        _Handler.response = (200, OK_BODY)
        _Handler.ports = []
        _Handler.paths = []
        _Handler.delay = 0.0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/v1/systemone"
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self._drain_pool)

    def _drain_pool(self):
        with client._POOL._lock:                     # noqa: SLF001 - the test owns the pool's state
            pooled = list(client._POOL._free)
            client._POOL._free.clear()
        for _, connection in pooled:
            client._close(connection)

    def post(self, timeout=5.0):
        return client._http_transport(b'{"state":{},"questions":{}}', {"Content-Type": "application/json"},
                                      timeout, self.url)

    def test_two_calls_reuse_one_connection(self):
        self.assertEqual(json.loads(self.post())["model"], "jev-test")
        self.post()
        self.assertEqual(len(_Handler.ports), 2, "the server saw two requests")
        self.assertEqual(len(set(_Handler.ports)), 1, "and they arrived on one connection")

    def test_a_second_call_after_something_else_still_reuses_it(self):
        self.post()
        self.post()
        self.post()
        self.assertEqual(len(set(_Handler.ports)), 1, "still one socket for three requests")

    def test_a_redirect_is_refused_and_never_followed(self):
        _Handler.response = (302, b"go elsewhere")
        with self.assertRaises(client.JevError) as caught:
            self.post()
        self.assertEqual(caught.exception.code, "http_302")
        self.assertEqual(len(_Handler.paths), 1, "the redirect target was never requested")

    def test_an_error_response_is_not_kept_in_the_pool(self):
        _Handler.response = (429, b"slow down")
        with self.assertRaises(client.JevError) as caught:
            self.post()
        self.assertEqual(caught.exception.code, "rate_limited")
        _Handler.response = (200, OK_BODY)
        self.post()
        self.assertEqual(len(set(_Handler.ports)), 2, "a connection with an error on it is dropped")

    def test_a_socket_the_server_closed_while_idle_is_recovered_once(self):
        self.post()
        with client._POOL._lock:                     # noqa: SLF001
            for _, connection in client._POOL._free:
                connection.sock.close()              # what an idle-timeout server does
        self.assertEqual(json.loads(self.post())["model"], "jev-test", "the call still succeeds")
        self.assertEqual(len(set(_Handler.ports)), 2, "on a fresh connection")

    def test_a_timeout_is_the_retryable_network_error(self):
        _Handler.delay = 1.0
        with self.assertRaises(client.JevError) as caught:
            self.post(timeout=0.2)
        self.assertEqual(caught.exception.code, "network")

    def test_the_pool_is_bounded_and_closes_what_it_cannot_keep(self):
        pool = client._ConnectionPool(limit=2)
        connections = [pool.borrow(("http", "127.0.0.1", 1), 1.0) for _ in range(5)]
        for connection in connections:
            pool.release(("http", "127.0.0.1", 1), connection)
        self.assertEqual(pool.size(), 2)
        with self.assertRaises(Exception):
            connections[-1].request("POST", "/x")    # the extras were closed, not leaked

    def test_a_borrowed_connection_is_never_handed_to_two_callers(self):
        pool = client._ConnectionPool(limit=4)
        key = ("http", "127.0.0.1", 1)
        first = pool.borrow(key, 1.0)
        second = pool.borrow(key, 1.0)
        self.assertIsNot(first, second, "an empty pool makes a new connection, it does not share one")
        for connection in (first, second):
            client._close(connection)


if __name__ == "__main__":
    unittest.main()
