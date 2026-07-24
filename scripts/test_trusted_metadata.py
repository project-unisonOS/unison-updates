#!/usr/bin/env python3
"""Attack simulations for signed update metadata."""

import base64
import copy
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.trusted_metadata import ClientState, MetadataError, Verifier, canonical  # noqa: E402

NOW = datetime(2026, 7, 23, tzinfo=timezone.utc)
HARDWARE = {"os": "ubuntu-24.04", "architecture": "x86_64"}


def keys(count=3):
    private = {f"k{i}": Ed25519PrivateKey.generate() for i in range(count)}
    public = {
        keyid: {
            "keytype": "ed25519",
            "public": base64.b64encode(key.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )).decode(),
        }
        for keyid, key in private.items()
    }
    return private, public


def envelope(signed, private, signers):
    payload = canonical(signed)
    return {
        "signed": signed,
        "signatures": [
            {"keyid": keyid, "sig": base64.b64encode(private[keyid].sign(payload)).decode()}
            for keyid in signers
        ],
    }


def root(private, public, version=1, signers=("k0", "k1")):
    signed = {
        "_type": "root", "version": version, "expires": "2027-07-23T00:00:00Z",
        "keys": public,
        "roles": {
            "root": {"keyids": ["k0", "k1", "k2"], "threshold": 2},
            "targets": {"keyids": ["k0", "k1"], "threshold": 1},
        },
    }
    return envelope(signed, private, signers)


def metadata(private, artifact, version=1, target_version=1, channel="stable",
             expires="2026-08-23T00:00:00Z", hardware=HARDWARE):
    signed = {
        "_type": "targets", "version": version, "channel": channel, "expires": expires,
        "target": {
            "path": "unisonos-v1.bundle", "version": target_version,
            "length": len(artifact), "hashes": {"sha256": hashlib.sha256(artifact).hexdigest()},
            "custom": {"hardware": hardware, "restart": True, "backup_required": True},
        },
    }
    return envelope(signed, private, ("k0",))


def rejects(callable_, message):
    try:
        callable_()
        raise AssertionError(f"{message} was accepted")
    except MetadataError:
        pass


def main():
    private, public = keys()
    trusted = root(private, public)
    artifact = b"signed release candidate"

    verifier = Verifier(trusted, ClientState(1, {}, {}))
    target = verifier.verify_channel(metadata(private, artifact), artifact, "stable", HARDWARE, NOW)
    assert target["custom"]["backup_required"]

    rejects(lambda: verifier.verify_channel(
        metadata(private, artifact), artifact, "stable", HARDWARE, NOW
    ), "replay/freeze")

    for name, candidate, supplied, expected_channel, hardware in (
        ("expired", metadata(private, artifact, 2, 2, expires="2026-07-22T00:00:00Z"), artifact, "stable", HARDWARE),
        ("wrong channel", metadata(private, artifact, 2, 2, channel="preview"), artifact, "stable", HARDWARE),
        ("corrupt artifact", metadata(private, artifact, 2, 2), artifact + b"x", "stable", HARDWARE),
        ("wrong hardware", metadata(private, artifact, 2, 2), artifact, "stable", {"os": "ubuntu-24.04", "architecture": "arm64"}),
        ("target rollback", metadata(private, artifact, 2, 1), artifact, "stable", HARDWARE),
    ):
        rejects(lambda c=candidate, a=supplied, ch=expected_channel, hw=hardware:
                verifier.verify_channel(c, a, ch, hw, NOW), name)

    tampered = metadata(private, artifact, 2, 2)
    tampered["signed"]["target"]["length"] += 1
    rejects(lambda: verifier.verify_channel(tampered, artifact, "stable", HARDWARE, NOW), "tampering")

    next_private, next_public = keys()
    proposed = root(next_private, next_public, version=2, signers=("k0", "k1"))
    payload = canonical(proposed["signed"])
    proposed["signatures"] += [
        {"keyid": keyid, "sig": base64.b64encode(private[keyid].sign(payload)).decode()}
        for keyid in ("k0", "k1")
    ]
    verifier.rotate_root(proposed, NOW)
    verifier.verify_channel(metadata(next_private, artifact, 2, 2), artifact, "stable", HARDWARE, NOW)

    bad_rotation = copy.deepcopy(proposed)
    bad_rotation["signed"]["version"] = 4
    rejects(lambda: verifier.rotate_root(bad_rotation, NOW), "skipped root rotation")
    print("[PASS] Signed metadata rejects replay, freeze, expiry, channel, corruption, hardware, rollback, tamper, and bad rotation attacks.")


if __name__ == "__main__":
    main()
