"""Offline cloud media cache tests: resume, integrity and cross-session reuse."""
import hashlib
import tempfile
import unittest
from pathlib import Path

import httpx

from sarae_agent.media.cloud_cache import CloudMediaCache

MID = "a" * 32
DATA = b"\x00\x00\x00\x18ftypisom" + b"a" * 20000


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.requests = []
        self.item = {"media_id": MID, "filename": "sample.mp4", "size_bytes": len(DATA),
                     "sha256": hashlib.sha256(DATA).hexdigest(), "md5": hashlib.md5(DATA).hexdigest()}
        self.corrupt = False

        def handler(request):
            self.requests.append(request)
            if request.url.path.endswith("/manifest"):
                return httpx.Response(200, json={"session_id": 7, "agent_id": "agt_1",
                                                 "items": [self.item], "products_without_video": []})
            if request.url.path.endswith("/content"):
                start = int(request.headers.get("range", "bytes=0-").split("=")[1].split("-")[0])
                data = DATA[start:]
                if self.corrupt:
                    data = data[:-1] + b"X"
                headers = {"Content-Range": f"bytes {start}-{len(DATA)-1}/{len(DATA)}"} if start else {}
                return httpx.Response(206 if start else 200, content=data, headers=headers)
            return httpx.Response(404)
        self.client = httpx.Client(base_url="https://example.test/api", transport=httpx.MockTransport(handler))
        self.addCleanup(self.client.close)
        self.cache = CloudMediaCache(self.client, "agt_1", "example-secret", Path(self.tmp.name))

    def test_first_download_and_cached_reuse(self):
        result = self.cache.prepare(7)
        self.assertEqual(result["state"], "READY")
        self.assertEqual(Path(result["ready"][0]["path"]).read_bytes(), DATA)
        before = len([r for r in self.requests if r.url.path.endswith("/content")])
        self.assertEqual(self.cache.prepare(7)["state"], "READY")
        after = len([r for r in self.requests if r.url.path.endswith("/content")])
        self.assertEqual(before, after)

    def test_resume(self):
        dest = Path(self.tmp.name) / (MID + ".mp4.part")
        dest.write_bytes(DATA[:1000])
        self.assertEqual(self.cache.prepare(7)["state"], "READY")
        matches = [r for r in self.requests if r.url.path.endswith("/content")]
        self.assertEqual(matches[0].headers["range"], "bytes=1000-")

    def test_recover_complete_part_without_redownload(self):
        dest = Path(self.tmp.name) / (MID + ".mp4.part")
        dest.write_bytes(DATA)
        self.assertEqual(self.cache.prepare(7)["state"], "READY")
        self.assertFalse(dest.exists())
        self.assertFalse(any(r.url.path.endswith("/content") for r in self.requests))

    def test_bad_checksum_not_published(self):
        self.corrupt = True
        result = self.cache.prepare(7)
        self.assertEqual(result["state"], "FAILED")
        self.assertFalse((Path(self.tmp.name) / (MID + ".mp4")).exists())

    def test_empty_or_missing_checksums_not_ready(self):
        self.item["sha256"] = None
        self.item["md5"] = None
        self.assertEqual(self.cache.prepare(7)["state"], "FAILED")

    def test_wrong_agent_manifest_rejected(self):
        self.cache.agent_id = "agt_other"
        with self.assertRaises(Exception):
            self.cache.prepare(7)


if __name__ == "__main__":
    unittest.main()


class PrepareProgressTests(CacheTests):
    def test_progress_reports_bytes_and_the_revision_of_ready_videos(self):
        from unittest.mock import patch
        self.item["revision"] = "rev-1"
        seen = []
        with patch("sarae_agent.media.cloud_cache.PROGRESS_SECONDS", 0):
            result = self.cache.prepare(7, on_progress=seen.append)
        self.assertEqual((seen[0]["state"], seen[0]["bytes_done"], seen[0]["bytes_total"]), ("PREPARING", 0, len(DATA)))
        self.assertEqual(seen[-1]["bytes_done"], len(DATA))
        self.assertTrue(all(a["bytes_done"] <= b["bytes_done"] for a, b in zip(seen, seen[1:])), "progress never goes back")
        self.assertEqual(result["ready"][0]["revision"], "rev-1")
        self.assertEqual((result["bytes_done"], result["bytes_total"]), (len(DATA), len(DATA)))


class MediaPrepareCommandTests(unittest.TestCase):
    def router(self, media_prepare=None):
        from unittest.mock import MagicMock
        from sarae_agent.cloud.commands import CommandRouter
        return CommandRouter(playback=MagicMock(), monitor=MagicMock(), voice=MagicMock(), media_prepare=media_prepare)

    def test_command_starts_the_download_and_runs_in_the_background_lane(self):
        from sarae_agent.cloud.commands import lane
        calls = []
        router = self.router(lambda sid: calls.append(sid) or {"session_id": sid, "state": "PREPARING", "total": 3, "ready": [{"path": "C:/x"}]})
        self.assertEqual(router.execute("media_prepare", {"session_id": 7}), {"session_id": 7, "state": "PREPARING", "total": 3})
        self.assertEqual(calls, [7])
        self.assertEqual(lane("media_prepare"), "background", "downloads never block LIVE controls")

    def test_bad_payload_or_missing_support_is_an_explicit_error(self):
        from sarae_agent.errors import AgentError
        with self.assertRaises(AgentError) as bad:
            self.router(lambda sid: {}).execute("media_prepare", {"session_id": 0})
        self.assertEqual(bad.exception.code, "INVALID_COMMAND_PAYLOAD")
        with self.assertRaises(AgentError) as missing:
            self.router().execute("media_prepare", {"session_id": 7})
        self.assertEqual(missing.exception.code, "MEDIA_PREPARE_UNAVAILABLE")

    def test_heartbeat_report_carries_no_local_path(self):
        from types import SimpleNamespace
        from sarae_agent.runtime import AgentRuntime
        job = {"session_id": 7, "state": "READY", "total": 1, "bytes_total": 10, "bytes_done": 10,
               "ready": [{"media_id": MID, "revision": "r", "path": r"C:\Users\shop\cache\a.mp4", "size_bytes": 10}],
               "failed": [], "products_without_video": []}
        report = AgentRuntime.media_cache_report(SimpleNamespace(_cloud_cache_job=job))
        self.assertEqual(report["ready"], [{"media_id": MID, "revision": "r"}])
        self.assertNotIn("Users", str(report))
