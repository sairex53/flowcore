# Runtime recovery and reconciliation

Query the structured state using the local operator socket.

~~~sh
./build/flowcore ops status
~~~

For an HTB hierarchy, runtime_safety identifies the namespace, ifindex,
alias and root. Each generation belongs to that resource epoch. A newly
admitted lab starts at generation zero.

| Field | Meaning |
| --- | --- |
| desired_generation | Highest accepted transaction proposal in this resource epoch |
| proposed_generation | The staged transaction currently in progress |
| applied_generation | Allocation that the daemon currently attributes to its kernel writes. Unknown after ambiguous failure or release. |
| kernel_verified_generation | Generation whose configuration was read back. A monotonic timestamp records the last verification. |
| last_stable_generation | Last successfully committed generation, retained as evidence even if the current kernel state is unknown |
| last_stable_allocation | Rates and ceilings at the last stable generation, in kbit/s |

The journal at FLOWCORE_STATE/actuation.json is a bounded latest-state
checkpoint. The daemon persists PROPOSED before writing, then verifies
the complete target and persists VERIFIED. The synchronous API reports
success after verification and persistence. The journal is not a
durable event stream. The existing decision history remains separate.

1. Compare generation, capacity, ownership, topology and current rates.
2. Stage a validated target. Apply decreases before increases at a
   fixed parent capacity to avoid transiently overcommitting guarantees.
3. Read back the kernel target and commit the checkpoint.
4. If a class command fails, observe the hierarchy. Compensate only if
   the configuration exactly matches the acknowledged prefix or that
   prefix plus the last attempted write. Check the old configuration
   again after compensation.
5. If the observed state is ambiguous, record QUARANTINED with the
   cause and last stable allocation. Disable further actuation and
   require explicit recreation of the disposable lab. A failed
   checkpoint before mutation returns NO_MUTATION. A verified rollback
   returns VERIFIED_COMPENSATION.

Each class change and each socket option is a separate kernel operation.
A successful HTB readback says nothing about intermediate packet
scheduling. File replacement uses fsync, rename and directory fsync,
which are independent of kernel updates. If final persistence fails,
the actuator compensates when ownership and readback permit it. If the
recovery checkpoint also cannot be written, the live status records the
failure and the earlier on-disk record is only historical. The existing
BPF policy replaces one hash entry and checks readback. This cannot
atomically commit an HTB or socket change with it. [Linux traffic-control
Netlink](https://docs.kernel.org/netlink/specs/tc.html) · [Linux BPF
hash map semantics](https://docs.kernel.org/bpf/map_hash.html)

On abrupt exit, lab holder processes receive the parent-death signal,
and any open namespace or BPF references retain their ordinary kernel
lifetimes. A new daemon takes both locks and starts with the controller
disabled. It surfaces the prior checkpoint as HISTORICAL_NO_REPLAY or
UNREADABLE_NO_REPLAY. It never uses the historical ifindex, cookie,
endpoint ID or generation to authorize a new write. A controlled
capacity stimulus that fails partway disposes of the lab. Graceful
shutdown releases the lab and removes the owned control socket. A
cleanup failure leaves the daemon checkpoint unclean.

Pacing restoration verifies the cookie and each option before
modification. A failed restoration remains visible from the handle.
Applications call tick on a timer to enforce expiry, even when idle.
If an application closes its descriptor while another holder keeps the
same socket alive, the policy handle has no authority over that other
descriptor. Process death or SIGKILL cannot run application cleanup.
A live external holder can therefore retain the last option until it
closes or restores it. This design also does not provide a cross-process
lock over a passed socket.

See [operational recovery](RECOVERY.md) for persistent state procedures.
