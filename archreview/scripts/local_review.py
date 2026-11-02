"""Run one review locally: both MCP servers as local subprocesses + real Amazon Bedrock.

Usage: AWS_REGION=us-west-2 python scripts/local_review.py [example_name]
"""
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend" / "src"))

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

from archrev.config import DEFAULT_MODEL_PREFERENCE  # noqa: E402
from archrev.mcp_client import AgentCoreMcpClient  # noqa: E402
from archrev.models import ReviewRequest  # noqa: E402
from archrev.orchestrator import CATALOG, STANDARDS, ReviewAgent  # noqa: E402

EXAMPLES = json.loads((ROOT / "frontend" / "examples.json").read_text())


def start(dirname: str) -> tuple[subprocess.Popen, int]:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    code = f"import main; main.mcp.settings.port={port}; main.mcp.run(transport='streamable-http')"
    p = subprocess.Popen([sys.executable, "-c", code], cwd=ROOT / "mcp_servers" / dirname,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), 0.3).close(); break
        except OSError:
            time.sleep(0.2)
    return p, port


async def main(name: str) -> None:
    ex = next(e for e in EXAMPLES if e["name"] == name)
    req = ReviewRequest.model_validate(ex["request"])
    ps, pport = start("standards")
    pc, cport = start("catalog")
    br = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "us-west-2"),
                      config=Config(read_timeout=120))

    async def llm(**kw):
        return await asyncio.to_thread(br.converse, **kw)

    try:
        clients = {STANDARDS: AgentCoreMcpClient(STANDARDS, f"http://127.0.0.1:{pport}/mcp", "cr-local-0001"),
                   CATALOG: AgentCoreMcpClient(CATALOG, f"http://127.0.0.1:{cport}/mcp", "cr-local-0001")}
        res = await ReviewAgent(req, "cr-local-0001", clients, llm, DEFAULT_MODEL_PREFERENCE).run()
        out = ROOT / "build" / f"local_review_{name}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(res, indent=2))
        print(json.dumps({k: res[k] for k in ("status", "servers_used", "model_id", "llm_turns", "duration_ms",
                                              "warnings", "security_notices")}, indent=2))
        print("verdict:", res["recommendation"]["verdict"], "| components:",
              [c["service_id"] for c in res["architecture"]["components"]], "| standards:",
              [s["standard_id"] for s in res["standards"]], "| conflicts:", len(res["conflicts"]))
        print("wrote", out)
    finally:
        ps.terminate(); pc.terminate()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "genai-assistant"))
