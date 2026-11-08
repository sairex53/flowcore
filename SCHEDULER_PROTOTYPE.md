# Stock-kernel application-intent prototype

The runnable prototype is in [scheduler_experiment.py](scripts/scheduler_experiment.py). It separates the producer's explicit deadline decision from the kernel's scheduling and constrains both in disposable namespaces.

1. Validate a supplied deadline against the current monotonic time. Reject expired metadata and a horizon beyond 100 ms. In this experiment, positive slack of at most 12 ms selects urgent service. Longer admitted slack would use the ordinary class.
2. Put a privileged mark on the trusted producer's connected UDP socket. The producer sends urgent and routine datagrams on that same socket. For an urgent datagram, sendmsg supplies a priority cmsg and an EF DSCP cmsg. Classification uses kernel metadata, with no packet-content parsing.
3. The prototype's TC filter matches both the mark and priority 6. All other traffic uses the default class. Static HTB instead admits all datagrams from the marked socket. The stock CAKE DiffServ mode consumes the same EF marking. Unclassified baselines ignore the intent.
4. Verify the root, class rates, qdisc leaves and filter readback. Then test an actual capability-free priority-6 sender and compare the observed leaf packet deltas. The class counters must also cover the observed urgent echo traffic. CAKE DiffServ independently checks the voice tin counter.
5. During the capacity-collapse stress trial, lower the child guarantees and ceilings before lowering the 12 Mbit/s parent to 6 Mbit/s. Use three class changes against the same hierarchy and verify the resulting tree. Failed setup or readback invalidates the experiment and triggers namespace cleanup. These staged changes are separate kernel operations.

At the controlled interface, the prototype changes class selection per datagram without a syscall to reconfigure TC for each request. The datagram send already supplies the hint. Dynamic rate changes stay in the operator's control path. The code is a research fixture. It deliberately does not expose a production admission broker, application API for untrusted priorities, or a daemon activation mode.

## Run

Use a disposable Linux VM with a built FLOWCORE binary, working iproute2 and the required scheduler modules. Run from a regular user account with sudo. The experiment takes the existing exclusive research lock. It fails if another lab or daemon owns it.

```sh
sudo python3 -B scripts/scheduler_experiment.py
sudo python3 -B scripts/scheduler_experiment.py --capabilities
sudo ./build/flowcore bench replay scenarios/scheduler-tcp-comparison.json
```

The default UDP run has 10 seed-paired blocks. Use --repetitions 1 and a separate --seed for a smoke test. The runner saves the randomized order, configuration and qdisc readbacks, raw request timestamps, test counters and paired summaries under results/. Namespace teardown is the rollback boundary for this fixture. It never replaces a host qdisc or pins a BPF link. See [benchmark results](SCHEDULER_BENCHMARKS.md).

## Reproduce the upstream priority selftest

Use Linux revision d48723711bae9b8cdc56719c448ba42fef28cbee. From tools/testing/selftests/net, copy cmsg_sender.c, cmsg_so_priority.sh, lib.sh and lib/sh/defer.sh into a scratch directory under results/ while preserving their relative paths. Copy tools/testing/selftests/kselftest.h alongside cmsg_sender.c. Compile with gcc -O2 -Wall cmsg_sender.c -o cmsg_sender. The script expects bash, ip, tc, jq and an existing /run/netns mount point. In a disposable VM, run it with sudo in a private mount namespace and bind an empty scratch directory over /run/netns before invoking bash ./cmsg_so_priority.sh. Put the distro tc binary on PATH. This keeps temporary netns handles out of the host mount namespace. [Pinned test sources](https://github.com/torvalds/linux/tree/d48723711bae9b8cdc56719c448ba42fef28cbee/tools/testing/selftests/net)
