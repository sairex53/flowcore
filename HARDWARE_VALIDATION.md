# Hardware validation

Capture role inventories before reserving experiment time. They report Linux architecture and kernel, CPU affinity and topology context, namespace and boot identity, IP routes, link capabilities and MTU. Compare boot identities to detect when several roles share one Linux boot. A namespace or a second SSH session on the same VM is still the same host.

## NIC and path checks

The inventory reads driver and link details, TSO, GSO, GRO, checksum settings, rings, timestamping capabilities, configured NIC channels, sysfs queue CPU maps, interrupt counters and available IRQ affinity data. A probe can fail or a driver can omit a field. Preserve the failure and leave the value unknown. The lab does not change NIC offloads, IRQ routing, MTU or host routes. Record any operator changes with their before and after inventories.

Use separate test and management paths. Confirm link speed and duplex on both ends where available, MTU along the path, and the actual egress route in both directions. A driver's reported link speed is a link capability. Configured shaping is a local kernel limit. Measured goodput is workload-specific and includes protocol behavior. Report these three quantities separately. Before comparing qdiscs on hardware, measure an unshaped capacity control in each direction and ensure it has headroom over the intended bottleneck. A successful large transfer and the shaper counters are the basic check. If an unshaped path cannot exceed the configured shaping rate, that trial cannot establish that the selected qdisc is the limiting hop.

Keep VM and hardware measurements separate. Compare CPU pressure, interrupt placement, dispatch lag, offloads, and packet and byte counters when interpreting tail latency differences. Define the workload, static policy, expected fault and acceptance metrics before running the evaluation. Use independent paired repetitions for each kernel and hardware combination.

## Timing

Each request duration and transfer goodput use elapsed time from the worker's local monotonic clock. Each worker records clock implementation and nominal resolution and performs sixteen 1 ms sleeps before the workload. Sleep overshoot measures wakeup scheduling in that short probe. The workload records actual request dispatch lag in the raw samples. These are useful context when comparing VM and hardware jitter.

Agent startup schedules each worker with a relative lead time. The coordinator records its own request and response bounds around remote clock samples and keeps the worker's monotonic and realtime readings. NTP status and chrony tracking are collected when available. The exchange duration includes transport and userspace scheduling. These samples are diagnostic and do not synchronize the workers or provide one-way network timing. Treat start and fault offsets on the coordinator and remote hosts as approximate. Compare per-host elapsed intervals and use clock error bounds before correlating events across hosts.

Ethtool reports whether a device supports hardware timestamping. This lab does not request packet timestamps or read a PTP clock. Only use hardware timestamps in a separate validation after verifying driver support, clock synchronization and which timestamps the workload actually collected.
