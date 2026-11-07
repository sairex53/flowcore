# Fleet security

Fleet reuses Fabric mutual TLS and its separate agent and operator certificate roles. A Fabric admin needs an explicit Fleet operator mapping before reading or changing Fleet state. Each owner limits repository aliases, hosts, services and classes. A local cap actuator independently enforces interface identity, ownership and operator bounds. Repository content does not grant new privileges. Approval is explicit by an authorized operator, and no second approver is required by the current workflow.

## Git and signatures

The coordinator accepts full commit IDs only and loads bounded commit and blob objects from a configured local checkout using fixed Git commands. It disables replace objects, lazy fetching, external Git configuration and hooks for the object reads. Use an operator-controlled checkout and Git metadata. Git author metadata is recorded as a claim. The authenticated operator and optional bundle signer are separate identities.

For a signed bundle, commit a sibling `fleet.json.sig` containing `key_id` and `signature`. The signature is base64 Ed25519 over the ASCII bytes `FLOWCORE-FLEET-V1`, one zero byte, and the exact Git policy blob bytes. Newlines and formatting are covered. Verification uses Python's cryptography library with public keys provisioned in the coordinator configuration. A configured signature requirement fails closed when the signature, signer, or library is unavailable. An optional signature is still checked when present.

To sign with an existing locally protected Ed25519 PEM key, run the following from the policy checkout. Keep the private key outside Git.

```python
import base64
import json
from pathlib import Path
from cryptography.hazmat.primitives.serialization import load_pem_private_key

policy = Path("fleet.json")
key = load_pem_private_key(Path("/secure/owner-ed25519.pem").read_bytes(), password=None)
signature = key.sign(b"FLOWCORE-FLEET-V1\0" + policy.read_bytes())
Path("fleet.json.sig").write_text(json.dumps({
    "key_id": "release-2026",
    "signature": base64.b64encode(signature).decode()
}) + "\n")
print(base64.b64encode(key.public_key().public_bytes_raw()).decode())
```

Use the printed public key as the configured value for that signer ID. For rotation, add a new key ID and restart the coordinator with the same durable state. Approve new commits under the new key. Remove the old key after approved active stages complete or are rolled back. If a still-running forward stage no longer has its approved operator or signer authority after restart, Fleet enters rollback. An already completed or paused policy remains subject to operator review and explicit rollback. Keep the Fabric operator certificates and bundle signing keys separate.

## Audit and limits

The approval digest binds the actor, source, scope, generation and intended stages. It is a review handle rather than a secret. The coordinator checks the digest again under its single-writer lock. Rollback also requires the active approval and generation. A repo push, branch update, unsigned author field or copied review digest from a different TLS identity cannot apply a policy.

The coordinator keeps a bounded audit ring and emits structured logs. Treat hostnames, service names, author information and application metrics as operationally sensitive. Export logs to a restricted sink for long-term retention. The journal uses private atomic state and does not claim tamper-evidence against an administrator controlling that filesystem. Signed bundles authenticate policy bytes. They do not attest kernel state or protect against a compromised, enrolled agent falsifying its own telemetry. The deployment retains Fabric's single coordinator and known-resource trust model.
