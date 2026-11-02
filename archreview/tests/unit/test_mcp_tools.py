import json

import pytest

from conftest import ROOT, load_module

std = load_module("mcp_servers/standards/tools.py", "std_tools")
cat = load_module("mcp_servers/catalog/tools.py", "cat_tools")


# ---------- Enterprise Standards ----------

def test_seed_data_is_labeled_synthetic():
    for f in ("mcp_servers/standards/data/standards.json", "mcp_servers/catalog/data/catalog.json"):
        assert "SYNTHETIC" in json.loads((ROOT / f).read_text())["dataset"]


def test_standards_cover_required_categories():
    assert set(std.CATEGORIES) >= {"architecture", "iam", "encryption", "logging", "privacy", "ai_governance",
                                   "disaster_recovery", "third_party"}


def test_every_standard_control_reference_exists():
    ctl_ids = {c["control_id"] for c in std._DATA["controls"]}
    for s in std._DATA["standards"]:
        assert set(s["control_ids"]) <= ctl_ids, s["standard_id"]


def test_search_standards_ranks_relevant_first():
    r = std.search_standards("generative ai chatbot with prompt injection defense")
    assert r["server"] == "enterprise-standards"
    ids = [x["standard_id"] for x in r["results"]]
    assert ids and ids[0].startswith("STD-AI-")


def test_search_standards_category_filter():
    r = std.search_standards("anything", category="encryption")
    assert r["results"] and all(x["category"] == "encryption" for x in r["results"])


@pytest.mark.parametrize("q,cat_", [("", None), ("x" * 501, None), ("ok", "bogus")])
def test_search_standards_rejects_bad_input(q, cat_):
    with pytest.raises(std.ToolInputError):
        std.search_standards(q, cat_)


def test_get_standard_found_and_not_found():
    r = std.get_standard("std-enc-002")
    assert r["found"] and r["record_id"] == "STD-ENC-002" and r["standard"]["controls"]
    assert std.get_standard("STD-XXX-999")["found"] is False
    with pytest.raises(std.ToolInputError):
        std.get_standard("DROP TABLE")


def test_list_required_controls_monotonic_by_risk():
    low = std.list_required_controls("low", "confidential")["controls"]
    crit = std.list_required_controls("critical", "confidential")["controls"]
    assert {c["control_id"] for c in low} < {c["control_id"] for c in crit}
    assert all("confidential" in c["classifications"] for c in crit)


def test_list_required_controls_validation():
    with pytest.raises(std.ToolInputError):
        std.list_required_controls("extreme", "public")
    with pytest.raises(std.ToolInputError):
        std.list_required_controls("low", "top-secret")


# ---------- Approved Service Catalog ----------

def test_search_services():
    r = cat.search_services("serverless REST API with a NoSQL database")
    ids = [x["service_id"] for x in r["results"]]
    assert "SVC-LAMBDA" in ids and "SVC-APIGW" in ids


def test_get_service_profile_and_status():
    r = cat.get_service_profile("svc-legacy-ftp")
    assert r["found"] and r["profile"]["status"] == "retired"
    assert cat.get_service_profile("SVC-NOPE")["found"] is False
    with pytest.raises(cat.ToolInputError):
        cat.get_service_profile("lambda")


def test_compare_services():
    r = cat.compare_services(["SVC-AURORA", "SVC-DYNAMODB", "SVC-NOPE"])
    assert r["compared"] == ["SVC-AURORA", "SVC-DYNAMODB"]
    assert r["not_found"] == ["SVC-NOPE"]
    assert r["lowest_cost"] == "SVC-DYNAMODB"
    with pytest.raises(cat.ToolInputError):
        cat.compare_services(["SVC-S3"])


def test_find_reference_pattern():
    r = cat.find_reference_pattern("customer-facing generative AI assistant")
    assert r["results"][0]["pattern_id"] == "PAT-AI-001"


def test_pattern_services_exist_in_catalog():
    svc = {s["service_id"] for s in cat._DATA["services"]}
    for p in cat._DATA["patterns"]:
        assert set(p["services"]) <= svc, p["pattern_id"]


def test_catalog_contains_injection_test_record():
    rec = cat.get_service_profile("SVC-LEGACY-FTP")["profile"]
    assert "ignore all previous instructions" in rec["notes"].lower()
