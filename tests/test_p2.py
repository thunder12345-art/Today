import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "auto")
sys.path.insert(0, BASE)

import common
import build
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
        self.old_root = server.ROOT
        server.JAR_CACHE_DIR = self.tmp
        server.ROOT = self.tmp

    def tearDown(self):
        server.JAR_CACHE_DIR = self.old_dir
        server.ROOT = self.old_root

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


class LegacyModeTests(unittest.TestCase):
    def test_legacy_keeps_old_root_but_uses_local_site_jars(self):
        config = {"spider": "./tvbox.jar", "sites": [
            {"key": "花卷", "jar": "http://192.168.1.254:8899/jar/spider.jar"},
        ]}
        hashes = {"spider.jar": "16e7a6d79af111b1a67a04769e877fb7",
                  "tvbox.jar": "05dbd787ba7fccde38ff8e0d169d8009"}
        out = build.apply_legacy_mode(config, hashes)
        self.assertEqual(out["spider"],
                         "./tvbox.jar;md5;05dbd787ba7fccde38ff8e0d169d8009")
        self.assertTrue(out["sites"][0]["jar"].startswith("./jar/spider.jar"))


class CompatTests(unittest.TestCase):
    def test_compat_lists_all_profiles(self):
        configs = {
            "lan": {"spider": "./jar/spider.jar"},
            "phone": {"spider": "./jar/spider.jar"},
            "github": {"spider": "https://example.com/jar/spider.jar"},
            "legacy": {"spider": "./tvbox.jar"},
        }
        compat = build.build_compat(configs, {"spider.jar": "abc"})
        self.assertEqual(compat["product"], "Tomorrow")
        self.assertEqual(set(compat["profiles"]),
                         {"lan", "phone", "github", "legacy"})
        self.assertIn("pg_upgraded.jar", compat["jars"])


class RemoteBaseTests(unittest.TestCase):
    def test_rewrites_token_and_jar(self):
        config = {"spider": "./tvbox.jar", "sites": [
            {"key": "Wogg", "ext": "http://192.168.1.254:8899/token.json$$$https://www.wogg.net/"},
            {"key": "花卷", "jar": "http://192.168.1.254:8899/jar/spider.jar",
             "ext": {"site": ["https://x"]}},
            {"key": "keep", "jar": "https://example.com/other.jar", "ext": "plain"},
        ]}
        out = build.apply_remote_base(config, "http://100.64.0.5:8899")
        by_key = {site["key"]: site for site in out["sites"]}
        self.assertTrue(by_key["Wogg"]["ext"].startswith("http://100.64.0.5:8899/token.json$$$"))
        self.assertTrue(by_key["花卷"]["jar"].startswith(common.PUBLIC_JAR_URLS["spider.jar"]))
        self.assertEqual(by_key["keep"], config["sites"][2])
        self.assertTrue(out["spider"].startswith(common.PUBLIC_SPIDER_URL))

    def test_empty_base_is_noop(self):
        config = {"sites": [{"key": "a", "jar": "http://192.168.1.254:8899/jar/spider.jar"}]}
        self.assertIs(build.apply_remote_base(config, ""), config)

    def test_token_parts_helper(self):
        ext = "https://a/token.json$$$https://site/$$$null"
        self.assertEqual(common.rewrite_token_parts(ext, "http://100.64.0.5:8899/token.json"),
                         "http://100.64.0.5:8899/token.json$$$https://site/$$$null")


class PhoneModeTests(unittest.TestCase):
    def test_phone_uses_local_jars_and_fast_fail_token(self):
        config = {"spider": "./tvbox.jar", "sites": [
            {"key": "Wogg", "ext": "http://192.168.1.254:8899/token.json$$$https://www.wogg.net/"},
            {"key": "花卷", "jar": "http://192.168.1.254:8899/jar/spider.jar"},
        ]}
        out = build.apply_phone_mode(config)
        by_key = {site["key"]: site for site in out["sites"]}
        self.assertIn(common.PHONE_TOKEN_URL, by_key["Wogg"]["ext"])
        self.assertNotIn("192.168.1.254", by_key["Wogg"]["ext"])
        self.assertTrue(by_key["花卷"]["jar"].startswith("./jar/spider.jar"))
        self.assertTrue(out["spider"].startswith("./jar/spider.jar"))

    def test_github_uses_absolute_proxy_and_fast_fail_token(self):
        config = {"spider": "./tvbox.jar", "sites": [
            {"key": "Wogg", "ext": "http://192.168.1.254:8896/token.json$$$https://www.wogg.net/"},
            {"key": "花卷", "jar": "./jar/spider.jar"},
        ]}
        out = build.apply_github_mode(config)
        by_key = {site["key"]: site for site in out["sites"]}
        self.assertIn(common.PHONE_TOKEN_URL, by_key["Wogg"]["ext"])
        self.assertNotIn("192.168.1.254", by_key["Wogg"]["ext"])
        self.assertTrue(by_key["花卷"]["jar"].startswith(common.PUBLIC_JAR_URLS["spider.jar"]))
        self.assertTrue(out["spider"].startswith(common.PUBLIC_SPIDER_URL))

    def test_jar_md5_added_once(self):
        hashes = {"spider.jar": "abc123"}
        config = {"sites": [{"key": "a", "jar": "http://nas:8899/jar/spider.jar"}]}
        out = build.apply_jar_md5(config, hashes)
        self.assertEqual(out["sites"][0]["jar"], "http://nas:8899/jar/spider.jar;md5;abc123")
        again = build.apply_jar_md5(out, hashes)
        self.assertEqual(again["sites"][0]["jar"], "http://nas:8899/jar/spider.jar;md5;abc123")


if __name__ == "__main__":
    unittest.main()
