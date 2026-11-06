"""MCP build_workflow의 확대 설정이 빌더에 전달되어 hires 노드가 만들어지는지 가짜 API로 검사한다."""
import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
import mcp_server
import workflow_builder


class BuildWorkflowHiresTest(unittest.TestCase):
    def build(self, **kwargs):
        def handler(request):
            spec = json.loads(request.content)
            return httpx.Response(200, json={"workflow": workflow_builder.build_workflow(spec)})

        client = lambda timeout: httpx.AsyncClient(base_url="http://test", transport=httpx.MockTransport(handler))
        tool = getattr(mcp_server.build_workflow, "fn", mcp_server.build_workflow)
        with patch.object(mcp_server, "_client", client):
            return asyncio.run(tool(checkpoint="m.safetensors", positive="p", **kwargs))["workflow"]

    def titles(self, workflow):
        return [node["_meta"]["title"] for node in workflow.values()]

    def test_hires_adds_second_sampler(self):
        workflow = self.build(hires={"enabled": True, "scale_by": 1.5, "denoise": 0.5, "steps": 15})
        self.assertIn("KSampler (hires-fix)", self.titles(workflow))

    def test_without_hires_stays_single_pass(self):
        self.assertNotIn("KSampler (hires-fix)", self.titles(self.build()))


if __name__ == "__main__":
    unittest.main()
