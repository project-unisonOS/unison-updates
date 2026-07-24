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

## Signed channel metadata

`src/trusted_metadata.py` is the Phase 9 client-side trust boundary for
development, preview, and stable metadata. It uses canonical JSON and Ed25519
role thresholds, rejects expired or non-monotonic channel metadata and target
versions, binds a target to its channel and hardware profile, and verifies
artifact length and SHA-256 before the target can be staged.

Successful verification can emit
`unison.updates.verified-target.v1`. This receipt binds the locally verified
root and channel metadata versions to the exact artifact length, SHA-256,
target and release versions, channel, hardware profile, restart requirement,
and checkpoint requirement. The privileged platform lifecycle still verifies the signed
release bundle and the receipt's original signed channel-metadata evidence
against its separately pinned update root before it accepts the target for
staging.

Root rotation must advance exactly one version and meet both the currently
trusted root threshold and the proposed root threshold. This permits controlled
key replacement without allowing a single new online key to replace trust.

The verifier deliberately does not activate an update. Platform staging,
checkpointing, health validation, promotion, and rollback remain separate
privileged operations.
