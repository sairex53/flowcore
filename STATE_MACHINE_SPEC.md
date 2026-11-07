# Executable safety specification

The finite model in `tests/safety_model.py` defines a reference state with no imports from the implementation. Its successor function enumerates enabled actions. A breadth-first traversal checks state and transition invariants to a fixed point, including self-loops for rejected authority and stale IDs. The domains represent one lab, one controller, one representative lease, one representative path and one in-flight transaction. The detailed test adapters exercise real numeric and identity fields outside those finite domains.

| Machine | Allowed transitions and recovery |
| --- | --- |
| Daemon | Offline → ready under exclusive locks. Shutdown or crash → offline. Restart loads the committed policy revision and starts with no lab or active controller. |
| Controller | Off → active on authorized request and verified lab. The runtime also has read-only shadow and observe modes. Stop revokes write authority. Ownership loss, capacity drift and a transaction fault remove confirmation and lead to off or safe fallback. |
| Policy transaction | Idle → planned → partial → verified commit. Rejection before mutation leaves kernel state alone. Rejection after mutation enters compensation, which writes only after matching the known intermediate state. Rejection or drift during compensation leaves the result unconfirmed. |
| Resource ownership | Absent → owned after admission. Interface removal and detach release the lease. A new object requires new admission. |
| Helper | Ready authenticates reader/operator roles and checks epoch/revision. A verified mutation advances revision. Failure revokes ownership and transitions to failed. |
| Network recovery | Normal → capacity drift disables new writes. Link deletion detaches the lab. New lab admission and an explicit activation are required to resume after teardown. |
| Service registration | Absent → admitted under registry ownership and quota. Expiry removes the lease and invalidates probe evidence. The finite model uses an off state at expiry. The runtime can remain active after restoring the authorized baseline and revalidating current intents. |
| MPTCP path manager | Absent → owned only after readback. An owned path can change backup preference or be removed. ID reuse introduces a foreign object. Stale update and delete requests have no write authority. |

Planning can retain the previous verified allocation as a historical fact. A partial mutation cannot label its target verified. A clean shutdown checkpoint is published only after lab cleanup and state-file replacement. A file failure before rename preserves the previous revision. A directory sync failure after rename leaves the new visible revision with uncertain durability.

Run the finite checker directly.

```sh
python3 -B tests/safety_model.py
```

The finite traversal checks this abstraction. Differential tests in `tests/safety_model_test.py` and the live Linux harness compare selected implementation observations with independent expectations. The bounded state exploration is not a refinement proof for the daemon, kernel or privileged helpers. The scheduler, file system crash consistency and arbitrary concurrent privileged writers are outside the finite model. [Validation scope](MODEL_TEST_RESULTS.md)
