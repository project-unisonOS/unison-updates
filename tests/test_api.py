from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient


def _client():
    with tempfile.TemporaryDirectory() as tmpdir:
        import src.main as main

        main.store = main.UpdateStore(Path(tmpdir))
        with TestClient(main.app) as client:
            yield client


def test_health():
    for client in _client():
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["service"] == "unison-updates"


def test_policy_round_trip():
    for client in _client():
        get_resp = client.post("/v1/tools/updates.get_policy", json={"arguments": {}})
        assert get_resp.status_code == 200
        assert get_resp.json()["policy"]["auto_apply"] == "manual"

        set_resp = client.post(
            "/v1/tools/updates.set_policy",
            json={"arguments": {"policy_patch": {"auto_apply": "security_only"}}},
        )
        assert set_resp.status_code == 200
        assert set_resp.json()["policy"]["auto_apply"] == "security_only"


def test_plan_apply_status_flow():
    for client in _client():
        plan_resp = client.post(
            "/v1/tools/updates.plan",
            json={"arguments": {"selection": {"platform_version": "next"}, "constraints": {"approve": True}}},
        )
        assert plan_resp.status_code == 200
        plan_id = plan_resp.json()["plan_id"]

        apply_resp = client.post("/v1/tools/updates.apply", json={"arguments": {"plan_id": plan_id}})
        assert apply_resp.status_code == 200
        job_id = apply_resp.json()["job_id"]

        status_resp = client.post("/v1/tools/updates.status", json={"arguments": {"job_id": job_id}})
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["job_id"] == job_id
        assert body["status"] == "completed"
