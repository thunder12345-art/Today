import json
import os
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "auto")
sys.path.insert(0, BASE)

import build
import common
import feed_sync


class MaskTests(unittest.TestCase):
    def test_mask_text_cookie_patterns(self):
        text = ("BDUSS=abc123;STOKEN=def456; SESSDATA=ghi,1,x; "
                "__pus=p1; __puus=p2; bili_jct=j1")
        masked = common.mask_text(text)
        for secret in ("abc123", "def456", "ghi,1", "p1", "p2", "j1"):
            self.assertNotIn(secret, masked)

    def test_mask_text_json_fields(self):
        masked = common.mask_text('{"quark_cookie": "secretvalue", "token": "tok123"}')
        self.assertNotIn("secretvalue", masked)
        self.assertNotIn("tok123", masked)

    def test_mask_secrets_recursive(self):
        data = {"a": {"quark_cookie": "x", "name": "keep"}, "list": [{"token": "t"}], "s": "BDUSS=v;"}
        masked = common.mask_secrets(data)
        self.assertEqual(masked["a"]["quark_cookie"], "***")
        self.assertEqual(masked["a"]["name"], "keep")
        self.assertEqual(masked["list"][0]["token"], "***")
        self.assertNotIn("BDUSS=v", masked["s"])


class PublishGuardTests(unittest.TestCase):
    def test_newer_epoch_wins(self):
        self.assertTrue(common.publish_allowed(200, {"epoch": 100}))
        self.assertTrue(common.publish_allowed(100, {"epoch": 100}))
        self.assertFalse(common.publish_allowed(99, {"epoch": 100}))

    def test_missing_epoch_allows(self):
        self.assertTrue(common.publish_allowed(None, {"epoch": 100}))
        self.assertTrue(common.publish_allowed(5, {}))


class BuildMetaTests(unittest.TestCase):
    def test_run_meta_injected(self):
        config = build.apply_run_meta({"sites": []}, "run-1", 123)
        self.assertEqual(config["_meta"]["run_id"], "run-1")
        self.assertEqual(config["_meta"]["epoch"], 123)
        self.assertIn("generated_at", config["_meta"])
        untouched = build.apply_run_meta({"sites": []}, "", 1)
        self.assertNotIn("_meta", untouched)


class FeedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old_dir = feed_sync.FEED_SNAPSHOT_DIR
        feed_sync.FEED_SNAPSHOT_DIR = self.tmp
        self.item = {"name": "demo", "url": "https://example.com/feed.json", "ghproxy": False}

    def tearDown(self):
        feed_sync.FEED_SNAPSHOT_DIR = self.old_dir

    def test_success_writes_snapshot(self):
        payload = {"sites": [{"key": "a", "api": "csp_A"}]}
        with mock.patch.object(feed_sync, "fetch_feed_json", return_value=payload):
            got, info = feed_sync.fetch_feed_with_fallback(self.item)
        self.assertEqual(got, payload)
        self.assertFalse(info["restored"])
        self.assertEqual(info["sites"], 1)
        self.assertTrue(os.path.exists(feed_sync.feed_snapshot_path(self.item)))

    def test_failure_uses_snapshot(self):
        payload = {"sites": [{"key": "a", "api": "csp_A"}]}
        feed_sync.save_feed_snapshot(self.item, payload)
        with mock.patch.object(feed_sync, "fetch_feed_json", side_effect=RuntimeError("boom")):
            got, info = feed_sync.fetch_feed_with_fallback(self.item)
        self.assertEqual(got, payload)
        self.assertTrue(info["restored"])
        self.assertIn("boom", info["error"])

    def test_failure_without_snapshot_raises(self):
        with mock.patch.object(feed_sync, "fetch_feed_json", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                feed_sync.fetch_feed_with_fallback(self.item)


class SiteOverrideTests(unittest.TestCase):
    def test_override_applies_to_feed_entry(self):
        db = {"sites": [{"key": "Wogg", "api": "csp_Wogg", "jar": "https://x/pg.jar",
                         "ext": "http://old/token.json$$$https://wogg.net/",
                         "_meta": {"origin": "feed"}}], "caiji": []}
        policy = {"allow_external_jar": True,
                  "center_token_url": "http://192.168.1.254:8899/token.json",
                  "site_overrides": {"Wogg": {"jar": None, "ext": {"site": ["https://wogg.net"]}}}}
        feed_sync.migrate_feed_entries(db, {"wogg": "Lcom/github/catvod/spider/Wogg;"}, policy, {})
        entry = db["sites"][0]
        self.assertNotIn("jar", entry)
        self.assertEqual(entry["ext"], {"site": ["https://wogg.net"]})


if __name__ == "__main__":
    unittest.main()
