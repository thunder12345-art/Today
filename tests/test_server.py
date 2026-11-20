import http.client
import os
import sys
import threading
import unittest
from functools import partial
from http.server import ThreadingHTTPServer

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "auto")
sys.path.insert(0, BASE)

import server


class ServerHeadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        partial(server.Handler, directory=server.ROOT))
        cls.httpd.config_only = True
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        conn.close()
        return response.status, headers, body

    def test_head_root_has_json_headers_without_body(self):
        status, headers, body = self.request("HEAD", "/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("application/json", headers.get("content-type", ""))
        self.assertGreater(int(headers.get("content-length", "0")), 0)

    def test_head_phone_with_trailing_slash(self):
        status, headers, body = self.request("HEAD", "/tvbox.phone/")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertIn("application/json", headers.get("content-type", ""))

    def test_head_token_is_disabled_on_tvbox_port(self):
        status, _, _ = self.request("HEAD", "/token.json")
        self.assertEqual(status, 404)

    def test_get_config_has_single_cache_control(self):
        status, headers, body = self.request("GET", "/tvbox")
        self.assertEqual(status, 200)
        self.assertIn(b'"sites"', body)
        self.assertEqual(headers.get("cache-control"), "no-cache")


class PanelOpenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                        partial(server.Handler, directory=server.ROOT))
        cls.httpd.config_only = False
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)

    def request(self, method, path):
        conn = http.client.HTTPConnection("127.0.0.1", self.httpd.server_port, timeout=10)
        conn.request(method, path)
        response = conn.getresponse()
        body = response.read()
        headers = {key.lower(): value for key, value in response.getheaders()}
        conn.close()
        return response.status, headers, body

    def test_panel_opens_without_credentials(self):
        status, headers, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("content-type", ""))
        self.assertIn(b"<title>", body)

    def test_api_opens_without_credentials(self):
        status, headers, body = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertIn("application/json", headers.get("content-type", ""))
        self.assertIn(b'"state"', body)

    def test_login_page_is_not_exposed(self):
        status, _, body = self.request("GET", "/login")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
