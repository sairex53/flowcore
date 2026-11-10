# Runtime safety fault injection results

This report records tests on a Fedora 45 ARM64 virtual machine.
The live networking tests use disposable namespaces. Fault hooks are
part of the test code and unavailable through the deployed daemon.

## Reproduction

~~~sh
cmake -S . -B build -DFLOWCORE_BPF=ON -DCMAKE_BUILD_TYPE=Release
cmake --build build -j4
ctest --test-dir build -j4 --output-on-failure
python3 -B tests/install_test.py
sudo python3 -B tests/safety_integration.py
sudo python3 -B tests/helper_integration.py
sudo python3 -B tests/flow_engine_integration.py
sudo python3 -B tests/flow_v2_integration.py
sudo python3 -B tests/multipath_integration.py
sudo python3 -B tests/adaptive_integration.py
sudo python3 -B tests/optimizer_integration.py
sudo python3 -B tests/research_integration.py
sudo python3 -B tests/hybrid_lab.py
sudo unshare --net -- bash -c 'ip link set lo up && python3 -B tests/udp_intent_test.py --live'
~~~

## Confirmed outcomes

The test VM ran Linux 7.2.11-300.fc45.aarch64. Release builds were
tested with CO-RE enabled and disabled. The staged install exercised
read-only mode with zero effective capabilities and a relocated SDK.

| Injection or workload | Verified result |
| --- | --- |
| Invalid generation and overlapping proposal | No class write. A serial writer also wins the concurrent policy compare-and-swap test. |
| Failure before first class write or before journal replacement | No kernel change when the journal cannot stage. A simulated first kernel rejection leaves the baseline verified. |
| Partial class write, lost acknowledgement and readback failure | The simulated model exercised all three cases. The live test lost the reply after a real class update, verified compensation at generation 0, then verified the next successful update at generation 2. |
| Final journal write and directory sync failure | The simulated fault cases verified compensation. An unrelated or ambiguous state quarantined the hierarchy instead of attempting a speculative overwrite. |
| Foreign veth alias or flower classifier | The live daemon refused actuation. The injected filter remained present until explicit disposal of the lab. |
| Controller evaluation error or mixed workload | The fault fixture restored baseline reservations while retaining an authorized class ceiling. The live adaptive workload verified a 9 Mbit/s class ceiling under load. |
| Client disconnect, daemon SIGKILL and restart | Policy CAS reconciled after a disconnected reply. Live kills during initial setup and after a real class write released the holders. Startup reported the interrupted proposal as historical and kept active control disabled. |
| Corrupt actuation journal and shutdown error | The daemon started with unreadable historical evidence and no replay authority. An injected shutdown error removed the owned control socket and left the checkpoint unclean. |
| Helper SIGKILL, ACL and ownership drift | The isolated helper changed its epoch after restart. A read-only status call detected a foreign object, revoked verified status and left all links in the pinned test namespace untouched. |
| Lost BPF policy acknowledgement | The test consumed an actual policy acknowledgement and injected response loss. The wrapper stopped the owner and kernel inventory confirmed that all three map IDs disappeared. A subsequent attach and process kill exercised link lifetime and reload. |
| Socket reuse, expiry and concurrent pacing | Reused descriptors and changed socket cookies were rejected. Four threads raced 2,000 rate requests against expiry. Readback confirmed restored pacing rate, priority and low-water mark. The native test also passed with ASan, UBSan and leak detection enabled. |
| IPv4 and IPv6 UDP, MPTCP and Hybrid V3 | The isolated UDP SDK reported eight software TX timestamps per family. The existing three-path MPTCP failure scenario survived without fallback. Hybrid V3 verified its bounded shift and restoration before rebasing capacity. |
| Research chaos and replay | The live integration verified scheduled network faults, replay and cancellation, workload failure accounting, namespace cleanup and matching host network snapshots. |

The finite reference model explored 210 states and 2,762 transitions
across 28 abstract actions. The seeded differential HTB test ran 900
transaction cases against the real transaction implementation with a
simulated kernel. These checks do not prove that privileged actors
sharing a namespace cannot race the daemon.

The final live daemon stress test submitted 2,048 stale proposals
using six concurrent clients. All were rejected. The same daemon
process had nine file descriptors and one thread before and after.
VmRSS was 32,692 KiB before and 32,708 KiB after. This is a point-to-point
observation from one short stress test. It does not establish a
long-duration memory bound.

Both unprivileged CTest configurations completed 53 tests successfully
and skipped the privileged UDP test. The latter passed separately
inside a new network namespace. Local evidence lives under results/
and is excluded from Git. The primary logs are runtime-safety-final.log,
runtime-boundary-flow_engine_integration.log, runtime-final-research_integration.log,
runtime-hybrid-final.log and the two runtime-*-final-ctest.log files.

A real host reboot, physical power loss, filesystem corruption under
load, long-duration soak and recovery of an application-owned socket
kept alive by another process after SIGKILL remain unverified.
