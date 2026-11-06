"""MCP runpod_power 도구가 켜기 전 안전 조건을 지키고 공개 주소를 넘기는지 가짜 nightshift API로 검사한다."""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import mcp_server

OFF = {"id": "off1", "url": "", "runpod_tier": "image"}
ON = {"id": "on1", "url": "https://abc-8188.proxy.runpod.net", "runpod_tier": "image"}
LOCAL = {"id": "local1", "url": "", "runpod_tier": ""}


class RunpodPowerTest(unittest.TestCase):
    def call(self, pod_id, action, public="https://ns.example.com"):
        self.posts = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, json={"pods": [OFF, ON, LOCAL]})
            self.posts.append(request)
            return httpx.Response(200, json={"ok": True})

        client = lambda timeout: httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))
        tool = getattr(mcp_server.runpod_power, "fn", mcp_server.runpod_power)
        with patch.object(mcp_server, "_client", client), patch.dict(os.environ, {"NIGHTSHIFT_PUBLIC_URL": public}):
            return asyncio.run(tool(pod_id, action))

    def test_start_sends_public_address(self):
        self.assertEqual(self.call("off1", "start"), {"ok": True})
        self.assertEqual(str(self.posts[0].url), "http://test/api/pods/off1/runpod/start")
        self.assertEqual(self.posts[0].headers["x-forwarded-host"], "ns.example.com")
        self.assertEqual(self.posts[0].headers["x-forwarded-proto"], "https")

    def test_refused_starts_make_no_pod(self):
        cases = [("on1", "start", "https://ns.example.com"), ("local1", "start", "https://ns.example.com"),
                 ("missing", "start", "https://ns.example.com"), ("off1", "start", ""), ("off1", "reboot", "https://ns.example.com")]
        for pod_id, action, public in cases:
            with self.subTest(pod_id=pod_id, action=action, public=public):
                self.assertTrue(self.call(pod_id, action, public)["error"])
                self.assertEqual(self.posts, [])

    def test_stop_goes_to_server(self):
        self.assertEqual(self.call("on1", "stop", ""), {"ok": True})
        self.assertEqual(str(self.posts[0].url), "http://test/api/pods/on1/runpod/stop")


if __name__ == "__main__":
    unittest.main()
