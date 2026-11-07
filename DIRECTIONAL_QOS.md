# Directional QoS

FLOWCORE can install an optional ingress shaper on the receiving side of the owned sender veth. Each direction has a separate bandwidth and policy model. The default lab continues to use its configured sender egress scheduler. [Attribution](BOTTLENECK_ENGINE.md) · [Measured results](BOTTLENECK_RESULTS.md)

| Direction | Data path | Policy |
|---|---|---|
| Egress | Local stack → `fc0` root qdisc → peer `fc1` | Existing configured HTB and fq_codel classes with destination-port matches, or the existing CAKE policy. |
| Ingress | Peer `fc1` → `fc0` ingress hook → `fcifb0` egress HTB → local receive stack | Independent cap. TCP replies with source port 9000 select the critical class for IPv4 and IPv6. Other traffic uses the background class. The two guarantees are 30% and 70% and both ceilings equal the ingress cap. |

The receive-side cap acts after traffic reaches the interface. It can regulate the local delivery queue and affect responsive senders through feedback. Packets have already crossed the upstream link. Observe that link or coordinate with the sender if that earlier bottleneck matters. [IFB implementation](https://github.com/torvalds/linux/blob/d48723711bae9b8cdc56719c448ba42fef28cbee/drivers/net/ifb.c) · [mirred interface](https://github.com/iproute2/iproute2/blob/main/man/man8/tc-mirred.8)

## Start and inspect a private lab

Start an authorized lab daemon and opt in when creating a fresh lab. The ingress limit is in decimal kbit/s, from 100 to 1,000,000. The egress limit comes from the lab profile.

```sh
sudo ./build/flowcore lab up --ingress-kbit 2000
sudo ./build/flowcore qos direction --json
sudo ./build/flowcore capacity estimate --json
sudo ./build/flowcore lab inspect --json
sudo ./build/flowcore lab down
```

The IFB driver must already be available. Upstream IFB module initialization can create devices in the initial namespace, so FLOWCORE checks for the driver and does not request implicit IFB module loading. A disposable VM can provision the module with `numifbs=0`. The Fedora fixture already had that setting. The host's boot and link/qdisc fingerprints were checked around the lab experiments. [IFB module initialization](https://github.com/torvalds/linux/blob/d48723711bae9b8cdc56719c448ba42fef28cbee/drivers/net/ifb.c)

## Admission and rollback

Ingress admission requires the daemon's fresh private veth pair and its pinned namespace descriptors. The adapter rejects the host namespace, extra interfaces, foreign ingress hooks and topologies beyond the two endpoints. The IFB tree and its classifiers are installed and verified before the redirect is added. One matchall rule redirects `fc0` ingress to `fcifb0` egress. The IFB has no redirect hook, and the paired topology has no forwarding role. The readback validates the target, action, class rates, default class and classifier shape. Failure destroys the candidate namespaces. An already admitted lab stays active if a replacement fails.

Monitor mode exposes diagnostics and denies lab writes. An ingress selection on an existing lab is rejected. Stop and start that lab to change ingress options. Existing explicit policy replacement retains the selected ingress profile and admits a fresh candidate before releasing the prior namespaces. It has the existing behavior of replacing lab connections. Teardown releases both namespaces and the IFB. Legacy `--research` manifests use their previous topology, while the dedicated bottleneck experiment exercises IFB.

The direct CLI admission and readback were validated with Fedora's iproute2 7.0. Older JSON layouts, unavailable IFB/mirred support or incomplete readback fail admission. Ingress automatic actuation remains outside the adapter's scope. The laboratory checks IPv4 packet counters for both classes and verifies the IPv6 critical rule with a live request and reply.
