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


def test_manifest_drives_catalog_and_plan(tmp_path, monkeypatch):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        """
{
  "schema_version": "unison.platform.release.manifest.v1",
  "release": {"version": "v0.6.0-alpha.1", "channel": "alpha"},
  "compose": {"images_pinned": {"orchestrator": "ghcr.io/project-unisonos/unison-orchestrator@sha256:test"}},
  "model_packs": {"default_profile": "alpha/default"}
}
""".strip()
    )

    import src.main as main

    monkeypatch.setattr(main, "MANIFEST_PATH", str(manifest_path))
    main.store = main.UpdateStore(tmp_path / "data")

    with TestClient(main.app) as client:
        check_resp = client.post("/v1/tools/updates.check", json={"arguments": {}})
        assert check_resp.status_code == 200
        catalog = check_resp.json()["catalog"]
        assert catalog["manifest"]["release_version"] == "v0.6.0-alpha.1"

        plan_resp = client.post(
            "/v1/tools/updates.plan",
            json={"arguments": {"selection": {}, "constraints": {"approved": True}}},
        )
        assert plan_resp.status_code == 200
        body = plan_resp.json()
        assert body["source_manifest_version"] == "v0.6.0-alpha.1"
        assert "orchestrator" in body["images_pinned"]
