# Machine validation

Historical initial-MVP capability snapshot. Fresh review runs record kernel, architecture, source/build hashes and virtualization per experiment; see BENCHMARK_REPORT.md. The current target reports QEMU virtualization.

| Capability | Observed result |
|---|---|
| OS and architecture | Fedora 45 Server, AArch64 |
| Kernel | 7.2.11-300.fc45.aarch64, PREEMPT_DYNAMIC |
| CPU and memory | 4 CPUs, approximately 3.8 GiB RAM |
| cgroup | v2, cpu/io/memory/pids/cpuset controllers available |
| Compiler | GCC 16.2.1, successful C++23 configure/build |
| Build tool | CMake 4.3.0, Ninja |
| iproute | 7.0.0-2.fc45 |
| tc | Fedora iproute-tc 7.0.0-2.fc45, locally extracted under tools |
| RPM integrity | rpm -K reports digests and signatures OK |
| Namespace lifecycle | unshare/namespace-holder PIDs/nsenter verified |
| veth and MTU | two endpoints created and raised at MTU 1500 |
| Offloads | TSO/GSO/GRO disabled on both endpoints, flags archived |
| HTB/pfifo/fq_codel | actual qdisc/class creation and workload runs passed |
| CAKE | actual shaped qdisc creation and workload run passed |
| flower | TCP destination-port classifier attached and exercised |
| rtnetlink | C++ RTM_GETLINK returned real IFLA_STATS64 counters |
| Controller | real rate change, stale feedback recovery, hysteresis and rollback passed |

Kernel config has CONFIG_NET_NS=y, CONFIG_NET_SCH_FIFO=y,
CONFIG_NET_SCH_FQ_CODEL=y, CONFIG_VETH=m, CONFIG_NET_SCH_HTB=m,
CONFIG_NET_SCH_CAKE=m and CONFIG_NET_CLS_FLOWER=m. The successful lab operations
establish availability beyond config declarations. Standard kernel modules may
autoload when tc/ip requests their functions.

CONFIG_BPF_SYSCALL=y and CONFIG_CGROUP_BPF=y were present in the initial snapshot. The current project includes optional libbpf/CO-RE observer and TCX flow programs. Fresh kernel attachment, export and packet-fixture checks are described in FEATURE_MATRIX.md; compile-time flags alone do not prove attachment support.

All benchmark topology is created within fresh anonymous namespaces. The host's
existing interfaces and qdiscs are outside the experiment. Setup requires sudo.
Exact runtime environment, TCP settings, hashes and offload flags are archived
with each benchmark under results/.
