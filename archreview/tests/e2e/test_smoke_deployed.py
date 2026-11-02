"""End-to-end smoke test against the DEPLOYED site.

Run: SITE_URL=https://archreview.awsteach.com pytest tests/e2e -q
Uses build/outputs.json for the API URL and user-pool id (to clean up the throwaway user).
"""
import json
import os
import socket
import ssl
import time
import uuid
from pathlib import Path

import boto3
import pytest
import requests

ROOT = Path(__file__).resolve().parents[2]
SITE = os.environ.get("SITE_URL", "https://archreview.awsteach.com").rstrip("/")
pytestmark = pytest.mark.skipif(not (ROOT / "build" / "outputs.json").exists(), reason="not deployed")


@pytest.fixture(scope="module")
def outputs():
    return json.loads((ROOT / "build" / "outputs.json").read_text())["ArchRevApp"]


@pytest.fixture(scope="module")
def user(outputs):
    name = f"smoke{uuid.uuid4().hex[:8]}"
    yield name, "abc123"
    boto3.client("cognito-idp", region_name=outputs["UserPoolId"].split("_")[0]).admin_delete_user(
        UserPoolId=outputs["UserPoolId"], Username=name)


def test_dns_and_tls():
    host = SITE.split("//")[1]
    assert socket.getaddrinfo(host, 443)
    ctx = ssl.create_default_context()
    with socket.create_connection((host, 443), timeout=10) as s, ctx.wrap_socket(s, server_hostname=host) as tls:
        cert = tls.getpeercert()
        assert tls.version() in ("TLSv1.2", "TLSv1.3")
        assert any(v == host for k, v in cert["subjectAltName"])


def test_site_banner_headers_and_knowledge_file():
    r = requests.get(SITE + "/", timeout=15)
    assert r.status_code == 200 and "Synthetic demonstration data" in r.text
    assert "default-src 'self'" in r.headers.get("content-security-policy", "")
    assert "max-age" in r.headers.get("strict-transport-security", "")
    k = requests.get(SITE + "/projectknowledge.md", timeout=15)
    assert k.status_code == 200 and "Enterprise Solution Review Assistant" in k.text and len(k.text) > 5000
    assert requests.get(SITE.replace("https://", "http://") + "/", timeout=15, allow_redirects=False).status_code in (301, 302)


def test_cors_restricted(outputs):
    api = outputs["ApiUrl"].rstrip("/")
    ok = requests.options(api + "/reviews", headers={"Origin": SITE, "Access-Control-Request-Method": "POST"}, timeout=15)
    assert ok.headers.get("access-control-allow-origin") == SITE
    bad = requests.get(api + "/health", headers={"Origin": "https://evil.example"}, timeout=15)
    assert bad.headers.get("access-control-allow-origin") == SITE  # never reflects other origins


def test_full_review_flow(outputs, user):
    api = outputs["ApiUrl"].rstrip("/")
    name, pw = user
    r = requests.post(api + "/auth/register", json={"username": name, "password": pw, "role": "architect"}, timeout=15)
    assert r.status_code == 201, r.text
    tok = requests.post(api + "/auth/login", json={"username": name, "password": pw}, timeout=15).json()["id_token"]
    h = {"Authorization": tok, "X-Correlation-Id": f"smoke-{uuid.uuid4()}"}
    assert requests.post(api + "/reviews", json={"title": "x"}, headers=h, timeout=15).status_code == 400
    assert requests.get(api + "/reviews", params={"scope": "all"}, headers=h, timeout=15).status_code == 403
    example = json.loads((ROOT / "frontend" / "examples.json").read_text())[0]["request"]
    sub = requests.post(api + "/reviews", json=example, headers=h, timeout=15)
    assert sub.status_code == 202, sub.text
    rid = sub.json()["review_id"]
    deadline = time.time() + 330
    item = {}
    while time.time() < deadline:
        item = requests.get(f"{api}/reviews/{rid}", headers=h, timeout=15).json()
        if item["status"] not in ("queued", "running"):
            break
        time.sleep(5)
    assert item["status"] == "complete", item.get("error")
    res = item["result"]
    assert set(res["servers_used"]) == {"enterprise-standards", "approved-service-catalog"}
    assert res["architecture"]["components"] and res["standards"] and res["controls"]
    assert all(c["source"]["server"] == "approved-service-catalog" for c in res["architecture"]["components"])
    assert "ADVISORY" in res["disclaimer"]
    assert res["correlation_id"] == h["X-Correlation-Id"]
    (ROOT / "build" / "smoke_review.json").write_text(json.dumps(item, indent=2))
