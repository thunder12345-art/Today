import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "auto")
sys.path.insert(0, BASE)

import common
import feed_sync
import server

ZIP = b"PK\x03\x04" + b"0" * 100000


class LocalJarUrlTests(unittest.TestCase):
    def test_rewrites_allowlisted(self):
        self.assertEqual(common.local_jar_url("https://x/y/spider.jar", "http://nas:8899"),
                         "http://nas:8899/jar/spider.jar")
        self.assertEqual(common.local_jar_url("https://x/pg_upgraded.jar;md5;abc", "http://nas:8899/"),
                         "http://nas:8899/jar/pg_upgraded.jar")

    def test_keeps_other_jars(self):
        url = "https://x/other.jar"
        self.assertEqual(common.local_jar_url(url, "http://nas:8899"), url)
        self.assertEqual(common.local_jar_url("https://x/spider.jar", ""), "https://x/spider.jar")


class JarCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_dir = server.JAR_CACHE_DIR
        server.JAR_CACHE_DIR = self.tmp

    def tearDown(self):
        server.JAR_CACHE_DIR = self.old_dir

    def test_downloads_once(self):
        with mock.patch.object(server, "http_fetch", return_value=(200, ZIP)) as fetch:
            path = server.ensure_local_jar("spider.jar")
            self.assertTrue(os.path.exists(path))
            again = server.ensure_local_jar("spider.jar")
        self.assertEqual(path, again)
        self.assertEqual(fetch.call_count, 1)

    def test_serves_stale_on_failure(self):
        path = os.path.join(self.tmp, "spider.jar")
        os.makedirs(self.tmp, exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(b"PK old")
        os.utime(path, (0, 0))
        with mock.patch.object(server, "http_fetch", side_effect=OSError("offline")):
            got = server.ensure_local_jar("spider.jar")
        self.assertEqual(got, path)

    def test_rejects_invalid_payload(self):
        with mock.patch.object(server, "http_fetch", return_value=(200, b"not a zip")):
            self.assertIsNone(server.ensure_local_jar("spider.jar"))

    def test_unknown_name(self):
        self.assertIsNone(server.ensure_local_jar("../secret"))


class JarRewriteTests(unittest.TestCase):
    def test_migrate_rewrites_allowlisted_jar(self):
        db = {"sites": [{"key": "花卷", "api": "csp_PanWebShareHJ",
                         "jar": "https://gh-proxy.com/https://raw.githubusercontent.com/qist/tvbox/master/jar/spider.jar",
                         "_meta": {"origin": "local"}}], "caiji": []}
        policy = {"allow_external_jar": True, "center_token_url": "",
                  "site_overrides": {}, "local_jar_base": "http://192.168.1.254:8899"}
        feed_sync.migrate_feed_entries(db, {"panwebsharehj": "Lx;"}, policy, {})
        self.assertEqual(db["sites"][0]["jar"], "http://192.168.1.254:8899/jar/spider.jar")


if __name__ == "__main__":
    unittest.main()
