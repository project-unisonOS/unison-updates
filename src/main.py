from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from fastapi import Body, FastAPI, HTTPException
from pydantic import BaseModel, Field


def _now() -> float:
    return time.time()


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ToolRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)


@dataclass
class UpdateStore:
    data_dir: Path
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifacts_dir = self.data_dir / "artifacts"
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.policy_path = self.data_dir / "policy.json"
        self.plans_path = self.data_dir / "plans.json"
        self.jobs_path = self.data_dir / "jobs.json"
        self.history_path = self.data_dir / "history.json"
        self.rollback_path = self.data_dir / "rollback.json"
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        if not self.policy_path.exists():
            self._write_json(
                self.policy_path,
                {
                    "auto_apply": "manual",
                    "allow_major": False,
                    "allow_model_updates": False,
                    "quiet_hours": {"enabled": True, "start": "22:00", "end": "07:00"},
                    "updated_at": _iso_now(),
                },
            )
        for path in (self.plans_path, self.jobs_path):
            if not path.exists():
                self._write_json(path, {})
        if not self.history_path.exists():
            self._write_json(self.history_path, [])
        if not self.rollback_path.exists():
            self._write_json(
                self.rollback_path,
                {
                    "last_known_good": {
                        "platform_version": os.getenv("UNISON_IMAGE_TAG", "latest"),
                        "captured_at": _iso_now(),
                        "images_pinned": {},
                        "images_resolved": {},
                    }
                },
            )

    def _read_json(self, path: Path) -> Any:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    def _write_json(self, path: Path, payload: Any) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        tmp.replace(path)

    def write_artifact(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.artifacts_dir / name
        self._write_json(path, payload)
        return path

    def read_policy(self) -> dict[str, Any]:
        with self._lock:
            return self._read_json(self.policy_path)

    def patch_policy(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            policy = self._read_json(self.policy_path)
            policy.update(patch)
            policy["updated_at"] = _iso_now()
            self._write_json(self.policy_path, policy)
            return policy

    def list_plans(self) -> dict[str, Any]:
        with self._lock:
            return self._read_json(self.plans_path)

    def save_plan(self, plan: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            plans = self._read_json(self.plans_path)
            plans[plan["plan_id"]] = plan
            self._write_json(self.plans_path, plans)
            return plan

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._lock:
            plans = self._read_json(self.plans_path)
            return plans.get(plan_id)

    def save_job(self, job: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            jobs = self._read_json(self.jobs_path)
            jobs[job["job_id"]] = job
            self._write_json(self.jobs_path, jobs)
            return job

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            jobs = self._read_json(self.jobs_path)
            return jobs.get(job_id)

    def update_job(self, job_id: str, **patch: Any) -> dict[str, Any] | None:
        with self._lock:
            jobs = self._read_json(self.jobs_path)
            job = jobs.get(job_id)
            if not job:
                return None
            job.update(patch)
            job["updated_at"] = _iso_now()
            jobs[job_id] = job
            self._write_json(self.jobs_path, jobs)
            return job

    def rollback_target(self) -> dict[str, Any]:
        with self._lock:
            return self._read_json(self.rollback_path)

    def list_history(self) -> list[dict[str, Any]]:
        with self._lock:
            history = self._read_json(self.history_path)
            return history if isinstance(history, list) else []

    def record_update_attempt(self, attempt: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            history = self._read_json(self.history_path)
            if not isinstance(history, list):
                history = []
            history.append(attempt)
            self._write_json(self.history_path, history)

            rollback = self._read_json(self.rollback_path)
            rollback["last_attempted_target"] = attempt
            rollback["updated_at"] = _iso_now()
            self._write_json(self.rollback_path, rollback)
            return rollback

    def update_history_entry(self, job_id: str, **patch: Any) -> dict[str, Any] | None:
        with self._lock:
            history = self._read_json(self.history_path)
            if not isinstance(history, list):
                return None
            for idx, entry in enumerate(history):
                if isinstance(entry, dict) and entry.get("job_id") == job_id:
                    entry.update(patch)
                    history[idx] = entry
                    self._write_json(self.history_path, history)
                    return entry
            return None

    def set_last_known_good(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rollback = self._read_json(self.rollback_path)
            rollback["last_known_good"] = payload
            rollback["updated_at"] = _iso_now()
            self._write_json(self.rollback_path, rollback)
            return rollback


SERVICE_PORT = int(os.getenv("UNISON_UPDATES_PORT", os.getenv("SERVICE_PORT", "8089")))
DATA_DIR = Path(os.getenv("UNISON_UPDATES_DATA_DIR", "/var/lib/unison/updates"))
CURRENT_VERSION = os.getenv("UNISON_IMAGE_TAG", "latest")
PLATFORM_CHANNEL = os.getenv("UNISON_UPDATES_CHANNEL", "alpha")
REQUIRE_SIGNATURES = os.getenv("UNISON_UPDATES_REQUIRE_SIGNATURES", "false").lower() in {"1", "true", "yes", "on"}
MANIFEST_PATH = os.getenv("UNISON_UPDATES_MANIFEST_PATH", "").strip()
MANIFEST_URL = os.getenv("UNISON_UPDATES_MANIFEST_URL", "").strip()

store = UpdateStore(DATA_DIR)
app = FastAPI(title="unison-updates", version="0.1.0")


def _default_catalog() -> dict[str, Any]:
    return {
        "channel": PLATFORM_CHANNEL,
        "available": {
            "platform": {"current": CURRENT_VERSION, "target": CURRENT_VERSION, "available": False},
            "models": {"current_pack": os.getenv("UNISON_MODEL_PACK_PROFILE", "alpha/default"), "available": False},
            "os": {"track": "ubuntu-24.04", "available": False},
        },
        "signature_policy": {"required": REQUIRE_SIGNATURES},
        "checked_at": _iso_now(),
    }


def _load_release_manifest() -> dict[str, Any] | None:
    if MANIFEST_PATH:
        path = Path(MANIFEST_PATH)
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
    if MANIFEST_URL:
        try:
            with httpx.Client(timeout=3.0) as client:
                resp = client.get(MANIFEST_URL)
                resp.raise_for_status()
                body = resp.json()
            return body if isinstance(body, dict) else None
        except Exception:
            return None
    return None


def _catalog_from_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    release = manifest.get("release") if isinstance(manifest.get("release"), dict) else {}
    model_packs = manifest.get("model_packs") if isinstance(manifest.get("model_packs"), dict) else {}
    compose = manifest.get("compose") if isinstance(manifest.get("compose"), dict) else {}
    assets = manifest.get("assets") if isinstance(manifest.get("assets"), dict) else {}
    target_version = release.get("version") or CURRENT_VERSION
    return {
        "channel": release.get("channel") or PLATFORM_CHANNEL,
        "available": {
            "platform": {
                "current": CURRENT_VERSION,
                "target": target_version,
                "available": str(target_version) != str(CURRENT_VERSION),
            },
            "models": {
                "current_pack": os.getenv("UNISON_MODEL_PACK_PROFILE", "alpha/default"),
                "target_pack": model_packs.get("default_profile"),
                "available": bool(model_packs.get("default_profile")),
            },
            "os": {"track": "ubuntu-24.04", "available": False},
        },
        "signature_policy": {"required": REQUIRE_SIGNATURES},
        "manifest": {
            "schema_version": manifest.get("schema_version"),
            "release_version": target_version,
            "images_pinned": compose.get("images_pinned") or {},
            "images_resolved": compose.get("images_resolved") or {},
            "asset_count": len(assets),
        },
        "checked_at": _iso_now(),
    }


def _manifest_target(manifest: dict[str, Any] | None, fallback_version: str) -> dict[str, Any]:
    release = manifest.get("release") if isinstance(manifest, dict) and isinstance(manifest.get("release"), dict) else {}
    compose = manifest.get("compose") if isinstance(manifest, dict) and isinstance(manifest.get("compose"), dict) else {}
    return {
        "platform_version": release.get("version") or fallback_version,
        "channel": release.get("channel") or PLATFORM_CHANNEL,
        "images_pinned": compose.get("images_pinned") or {},
        "images_resolved": compose.get("images_resolved") or {},
    }


def _image_plan_steps(current_images: dict[str, Any], target_images: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    service_names = sorted(set(current_images) | set(target_images))
    for service in service_names:
        current_ref = str(current_images.get(service) or "")
        target_ref = str(target_images.get(service) or "")
        action = "hold" if current_ref == target_ref else "switch-image"
        if not current_ref and target_ref:
            action = "adopt-image"
        elif current_ref and not target_ref:
            action = "remove-image"
        steps.append(
            {
                "service": service,
                "action": action,
                "current": current_ref,
                "target": target_ref,
            }
        )
    return steps


def _build_execution_plan(plan: dict[str, Any]) -> dict[str, Any]:
    target_release = plan.get("target_release") if isinstance(plan.get("target_release"), dict) else {}
    target_images = target_release.get("images_pinned") if isinstance(target_release.get("images_pinned"), dict) else {}
    rollback = store.rollback_target().get("last_known_good") or {}
    current_images = rollback.get("images_pinned") if isinstance(rollback.get("images_pinned"), dict) else {}
    return {
        "executor": "compose-dry-run",
        "mode": "no-op",
        "current_version": rollback.get("platform_version") or CURRENT_VERSION,
        "target_version": target_release.get("platform_version") or CURRENT_VERSION,
        "steps": _image_plan_steps(current_images, target_images),
    }


def _artifact_payload(kind: str, execution_plan: dict[str, Any], job_id: str, plan_id: str) -> dict[str, Any]:
    return {
        "schema_version": "unison.updates.compose.override.v1",
        "artifact_kind": kind,
        "job_id": job_id,
        "plan_id": plan_id,
        "generated_at": _iso_now(),
        "execution_plan": execution_plan,
        "services": {
            step["service"]: {"image": step["target"], "action": step["action"]}
            for step in execution_plan.get("steps") or []
            if isinstance(step, dict) and str(step.get("service") or "").strip()
        },
    }


def _artifact_ref(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "basename": path.name,
        "format": "json",
    }


def _make_plan(arguments: dict[str, Any]) -> dict[str, Any]:
    manifest = _load_release_manifest()
    manifest_release = manifest.get("release") if isinstance(manifest, dict) and isinstance(manifest.get("release"), dict) else {}
    selection = arguments.get("selection") if isinstance(arguments.get("selection"), dict) else {}
    constraints = arguments.get("constraints") if isinstance(arguments.get("constraints"), dict) else {}
    person_id = arguments.get("person_id")
    target_version = (
        selection.get("platform_version")
        or selection.get("target_version")
        or manifest_release.get("version")
        or CURRENT_VERSION
    )
    pinned_images = {}
    resolved_images = {}
    if isinstance(manifest, dict):
        compose = manifest.get("compose") if isinstance(manifest.get("compose"), dict) else {}
        pinned_images = compose.get("images_pinned") or {}
        resolved_images = compose.get("images_resolved") or {}
    plan_id = f"plan-{uuid.uuid4().hex[:12]}"
    summary = [
        {
            "plane": "platform",
            "current": CURRENT_VERSION,
            "target": target_version,
            "action": "hold" if target_version == CURRENT_VERSION else "update",
        }
    ]
    return {
        "ok": True,
        "plan_id": plan_id,
        "person_id": person_id,
        "selection": selection,
        "constraints": constraints,
        "requires_confirmation": True,
        "summary": summary,
        "source_manifest_version": manifest_release.get("version"),
        "images_pinned": pinned_images,
        "images_resolved": resolved_images,
        "target_release": _manifest_target(manifest, target_version),
        "created_at": _iso_now(),
        "status": "planned",
    }


def _make_job(plan: dict[str, Any], person_id: str | None) -> dict[str, Any]:
    job_id = f"job-{uuid.uuid4().hex[:12]}"
    rollback = store.rollback_target().get("last_known_good") or {
        "platform_version": CURRENT_VERSION,
        "captured_at": _iso_now(),
        "images_pinned": {},
        "images_resolved": {},
    }
    target_release = plan.get("target_release") if isinstance(plan.get("target_release"), dict) else {}
    execution_plan = _build_execution_plan(plan)
    apply_artifact_name = f"{job_id}-apply-override.json"
    rollback_artifact_name = f"{job_id}-rollback-override.json"
    apply_artifact = store.write_artifact(
        apply_artifact_name,
        _artifact_payload("apply", execution_plan, job_id, plan["plan_id"]),
    )
    rollback_execution_plan = {
        "executor": execution_plan.get("executor"),
        "mode": execution_plan.get("mode"),
        "current_version": execution_plan.get("target_version"),
        "target_version": rollback.get("platform_version") or CURRENT_VERSION,
        "steps": _image_plan_steps(
            target_release.get("images_pinned") if isinstance(target_release.get("images_pinned"), dict) else {},
            rollback.get("images_pinned") if isinstance(rollback.get("images_pinned"), dict) else {},
        ),
    }
    rollback_artifact = store.write_artifact(
        rollback_artifact_name,
        _artifact_payload("rollback", rollback_execution_plan, job_id, plan["plan_id"]),
    )
    return {
        "ok": True,
        "job_id": job_id,
        "plan_id": plan["plan_id"],
        "person_id": person_id,
        "status": "completed",
        "result": {
            "applied": False,
            "mode": "dry-run",
            "reason": "Milestone 1 update service is wired for explicit planning and tracking; package promotion is not yet enabled.",
            "target_release": target_release,
            "rollback_target": rollback,
            "execution_plan": execution_plan,
            "artifacts": {
                "apply_override": _artifact_ref(apply_artifact),
                "rollback_override": _artifact_ref(rollback_artifact),
            },
        },
        "created_at": _iso_now(),
        "updated_at": _iso_now(),
    }


def _make_history_entry(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    return {
        "job_id": job["job_id"],
        "plan_id": job["plan_id"],
        "requested_at": job["created_at"],
        "status": job["status"],
        "target_release": result.get("target_release") or {},
        "rollback_target": result.get("rollback_target") or {},
        "mode": result.get("mode"),
        "artifacts": result.get("artifacts") or {},
        "applied": False,
    }


def _applied_payload(job: dict[str, Any]) -> dict[str, Any]:
    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    target_release = result.get("target_release") if isinstance(result.get("target_release"), dict) else {}
    return {
        "platform_version": target_release.get("platform_version") or CURRENT_VERSION,
        "captured_at": _iso_now(),
        "images_pinned": target_release.get("images_pinned") or {},
        "images_resolved": target_release.get("images_resolved") or {},
        "source_job_id": job.get("job_id"),
        "source_plan_id": job.get("plan_id"),
    }


@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, Any]:
    return {"status": "ok", "service": "unison-updates", "port": SERVICE_PORT}


@app.get("/ready")
@app.get("/readyz")
def ready() -> dict[str, Any]:
    return {"ready": True, "service": "unison-updates", "data_dir": str(DATA_DIR)}


@app.post("/v1/tools/updates.check")
def updates_check(_: ToolRequest = Body(default_factory=ToolRequest)) -> dict[str, Any]:
    manifest = _load_release_manifest()
    catalog = _catalog_from_manifest(manifest) if isinstance(manifest, dict) else _default_catalog()
    return {"ok": True, "catalog": catalog}


@app.post("/v1/tools/updates.plan")
def updates_plan(request: ToolRequest) -> dict[str, Any]:
    plan = _make_plan(request.arguments or {})
    store.save_plan(plan)
    return plan


@app.post("/v1/tools/updates.apply")
def updates_apply(request: ToolRequest) -> dict[str, Any]:
    arguments = request.arguments or {}
    plan_id = arguments.get("plan_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise HTTPException(status_code=400, detail="plan_id_required")
    plan = store.get_plan(plan_id)
    if not plan:
        raise HTTPException(status_code=404, detail="plan_not_found")
    job = _make_job(plan, arguments.get("person_id"))
    store.save_job(job)
    store.record_update_attempt(_make_history_entry(job))
    return job


@app.post("/v1/tools/updates.status")
def updates_status(request: ToolRequest) -> dict[str, Any]:
    job_id = request.arguments.get("job_id") if isinstance(request.arguments, dict) else None
    if not isinstance(job_id, str) or not job_id:
        raise HTTPException(status_code=400, detail="job_id_required")
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")
    return {"ok": True, **job}


def _set_job_state(request: ToolRequest, status: str) -> dict[str, Any]:
    job_id = request.arguments.get("job_id") if isinstance(request.arguments, dict) else None
    if not isinstance(job_id, str) or not job_id:
        raise HTTPException(status_code=400, detail="job_id_required")
    job = store.update_job(job_id, status=status)
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")
    return {"ok": True, **job}


@app.post("/v1/tools/updates.pause")
def updates_pause(request: ToolRequest) -> dict[str, Any]:
    return _set_job_state(request, "paused")


@app.post("/v1/tools/updates.resume")
def updates_resume(request: ToolRequest) -> dict[str, Any]:
    return _set_job_state(request, "completed")


@app.post("/v1/tools/updates.cancel")
def updates_cancel(request: ToolRequest) -> dict[str, Any]:
    return _set_job_state(request, "cancelled")


@app.post("/v1/tools/updates.rollback")
def updates_rollback(_: ToolRequest = Body(default_factory=ToolRequest)) -> dict[str, Any]:
    rollback = store.rollback_target()
    history = store.list_history()
    last_attempt = history[-1] if history else rollback.get("last_attempted_target")
    prior_candidate = (last_attempt or {}).get("rollback_target") if isinstance(last_attempt, dict) else rollback.get("last_known_good")
    return {
        "ok": True,
        "status": "ready",
        "target": prior_candidate,
        "last_attempted_target": rollback.get("last_attempted_target"),
        "artifacts": (last_attempt or {}).get("artifacts") if isinstance(last_attempt, dict) else {},
        "history_count": len(history),
        "history_tail": history[-5:],
        "note": "Rollback target is recorded, but automatic platform rollback remains pending release integration.",
    }


@app.post("/v1/tools/updates.record_applied")
def updates_record_applied(request: ToolRequest) -> dict[str, Any]:
    arguments = request.arguments or {}
    job_id = arguments.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise HTTPException(status_code=400, detail="job_id_required")
    job = store.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job_not_found")

    result = job.get("result") if isinstance(job.get("result"), dict) else {}
    applied_at = _iso_now()
    result["applied"] = True
    result["applied_at"] = applied_at
    updated_job = store.update_job(job_id, status="applied", result=result)
    if not updated_job:
        raise HTTPException(status_code=404, detail="job_not_found")

    store.update_history_entry(job_id, applied=True, applied_at=applied_at, status="applied")
    rollback = store.set_last_known_good(_applied_payload(updated_job))
    return {"ok": True, "job": updated_job, "last_known_good": rollback.get("last_known_good")}


@app.post("/v1/tools/updates.whats_new")
def updates_whats_new(request: ToolRequest) -> dict[str, Any]:
    arguments = request.arguments or {}
    from_version = arguments.get("from_version")
    to_version = arguments.get("to_version")
    if not isinstance(from_version, str) or not isinstance(to_version, str):
        raise HTTPException(status_code=400, detail="from_version_and_to_version_required")
    return {
        "ok": True,
        "from_version": from_version,
        "to_version": to_version,
        "highlights": [
            "Milestone 1 local-source install path is documented and validated.",
            "Golden-path validation covers onboarding, briefing, VDI, and recovery checks.",
            "Update application remains explicit and policy-governed.",
        ],
    }


@app.post("/v1/tools/updates.get_policy")
def updates_get_policy(_: ToolRequest = Body(default_factory=ToolRequest)) -> dict[str, Any]:
    return {"ok": True, "policy": store.read_policy()}


@app.post("/v1/tools/updates.set_policy")
def updates_set_policy(request: ToolRequest) -> dict[str, Any]:
    policy_patch = request.arguments.get("policy_patch") if isinstance(request.arguments, dict) else None
    if not isinstance(policy_patch, dict):
        raise HTTPException(status_code=400, detail="policy_patch_required")
    policy = store.patch_policy(policy_patch)
    return {"ok": True, "policy": policy}
