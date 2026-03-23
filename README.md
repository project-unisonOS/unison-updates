# unison-updates

Update orchestration service for UnisonOS.

This service exposes the tool-style HTTP contract expected by `unison-orchestrator`:

- `POST /v1/tools/updates.check`
- `POST /v1/tools/updates.plan`
- `POST /v1/tools/updates.apply`
- `POST /v1/tools/updates.status`
- `POST /v1/tools/updates.pause`
- `POST /v1/tools/updates.resume`
- `POST /v1/tools/updates.cancel`
- `POST /v1/tools/updates.rollback`
- `POST /v1/tools/updates.whats_new`
- `POST /v1/tools/updates.get_policy`
- `POST /v1/tools/updates.set_policy`

## Run

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
uvicorn src.main:app --host 0.0.0.0 --port 8089
```

## Notes

- State is persisted to JSON under `UNISON_UPDATES_DATA_DIR` so policy, plans, and jobs survive container restarts.
- This is a Milestone 1 local-first implementation focused on explicit update planning, job tracking, and rollback posture.
- Actual package/image application is stubbed behind explicit plans until the platform release/install path is fully integrated.
