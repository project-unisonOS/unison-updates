"""Small TUF-inspired verifier for UnisonOS update-channel metadata."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


class MetadataError(ValueError):
    pass


def canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _verify_threshold(envelope: dict, role: dict, keys: dict) -> None:
    signatures = envelope.get("signatures", [])
    signed = canonical(envelope.get("signed", {}))
    accepted = set()
    for signature in signatures:
        keyid = signature.get("keyid")
        if keyid in accepted or keyid not in role["keyids"] or keyid not in keys:
            continue
        try:
            public = Ed25519PublicKey.from_public_bytes(base64.b64decode(keys[keyid]["public"]))
            public.verify(base64.b64decode(signature["sig"]), signed)
            accepted.add(keyid)
        except Exception:
            continue
    if len(accepted) < int(role["threshold"]):
        raise MetadataError("signature threshold not met")


@dataclass
class ClientState:
    root_version: int
    channel_versions: dict[str, int]
    target_versions: dict[str, int]


class Verifier:
    def __init__(self, trusted_root: dict, state: ClientState):
        self.root = trusted_root
        self.state = state

    def rotate_root(self, candidate: dict, now: datetime) -> None:
        current = self.root["signed"]
        proposed = candidate.get("signed", {})
        if proposed.get("_type") != "root":
            raise MetadataError("candidate is not root metadata")
        if int(proposed.get("version", 0)) != self.state.root_version + 1:
            raise MetadataError("root version must advance exactly once")
        if _time(proposed["expires"]) <= now:
            raise MetadataError("new root metadata is expired")
        _verify_threshold(candidate, current["roles"]["root"], current["keys"])
        _verify_threshold(candidate, proposed["roles"]["root"], proposed["keys"])
        self.root = candidate
        self.state.root_version = proposed["version"]

    def verify_channel(
        self,
        envelope: dict,
        artifact: bytes,
        expected_channel: str,
        hardware: dict[str, str],
        now: datetime,
    ) -> dict:
        signed = envelope.get("signed", {})
        if signed.get("_type") != "targets":
            raise MetadataError("channel metadata must be targets")
        channel = signed.get("channel")
        if channel != expected_channel:
            raise MetadataError("wrong update channel")
        if _time(signed["expires"]) <= now:
            raise MetadataError("channel metadata is expired")
        root = self.root["signed"]
        _verify_threshold(envelope, root["roles"]["targets"], root["keys"])
        version = int(signed.get("version", 0))
        if version <= self.state.channel_versions.get(channel, 0):
            raise MetadataError("replayed or frozen channel metadata")
        target = signed.get("target", {})
        target_version = int(target.get("version", 0))
        if target_version <= self.state.target_versions.get(channel, 0):
            raise MetadataError("target rollback detected")
        compatibility = target.get("custom", {}).get("hardware", {})
        for key in ("os", "architecture"):
            if compatibility.get(key) != hardware.get(key):
                raise MetadataError(f"incompatible target {key}")
        if int(target.get("length", -1)) != len(artifact):
            raise MetadataError("artifact length mismatch")
        if target.get("hashes", {}).get("sha256") != hashlib.sha256(artifact).hexdigest():
            raise MetadataError("artifact digest mismatch")
        self.state.channel_versions[channel] = version
        self.state.target_versions[channel] = target_version
        return target

    def verify_target_receipt(
        self,
        envelope: dict,
        artifact: bytes,
        expected_channel: str,
        hardware: dict[str, str],
        now: datetime,
    ) -> dict:
        """Verify signed metadata and emit the exact staging authorization."""
        target = self.verify_channel(envelope, artifact, expected_channel, hardware, now)
        signed = envelope["signed"]
        return {
            "schema_version": "unison.updates.verified-target.v1",
            "verified_at": now.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "trusted_root_version": self.state.root_version,
            "channel": expected_channel,
            "channel_metadata_version": int(signed["version"]),
            "target": {
                "path": target["path"],
                "version": int(target["version"]),
                "release_version": str(target["custom"]["release_version"]),
                "length": int(target["length"]),
                "sha256": target["hashes"]["sha256"],
                "hardware": dict(target["custom"]["hardware"]),
                "restart": bool(target["custom"].get("restart")),
                "backup_required": bool(target["custom"].get("backup_required")),
            },
            "evidence": {"channel_metadata": envelope},
        }
