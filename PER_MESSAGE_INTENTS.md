# Per-message UDP intents

FLOWCORE can classify native IPv4 and IPv6 UDP datagrams independently on a single
socket. A cooperating service requests standard, background or urgent handling.
It can supply a local monotonic deadline, an expiration policy and an application
message identifier. Linux still transmits ordinary UDP datagrams. The SDK does
not add headers, retransmit, acknowledge receipt or change a socket's default
priority.

| Intent | skb priority | IP DSCP | Owned HTB hierarchy |
| --- | ---: | --- | --- |
| Standard | 0 | Default | Default leaf |
| Background | 0 | CS1 | Default leaf |
| Authorized urgent | 6 | EF | Urgent leaf when the socket mark also matches |

For IPv4 the sender appends IP_TOS before SO_PRIORITY. For IPv6 it uses
IPV6_TCLASS and SO_PRIORITY. Priority is local skb metadata. DSCP can leave the
host and may be rewritten by the path. It carries no service identity or
authorization. Upstream Linux exercises per-packet priority in its [networking
selftest](https://github.com/torvalds/linux/blob/master/tools/testing/selftests/net/cmsg_so_priority.sh).

## Authorization

The existing Intent V2 daemon grants the feature only when operator policy
enables the named critical service and the peer UID and unified cgroup match.
The lease is process bound. Registration and revocation use the existing intent
audit log. The daemon checks the service identity and operator grant again on
renewal. The SDK disables a sender at local lease expiry or when the channel
detects invalidation. A syscall already in progress can still finish.

The SDK duplicates the supplied UDP descriptor and verifies that it carries the
configured nonzero SO_MARK. The mark must be assigned by a trusted launcher or
operator in the relevant namespace. Neither the daemon nor SDK mints a mark,
installs a host qdisc, or accepts a mark value in a message. The default
configuration contains no UDP grants. The installed network helper does not
provision or revoke marked UDP descriptors.

The mark gates urgent HTB classification. An application without a matching
mark cannot promote packets by supplying EF or priority 6. Operators must keep
the capability to assign marks and access to marked descriptors within the
trusted boundary. Revoking a daemon lease disables the cooperating SDK after
invalidation. It does not strip a mark from a delegated descriptor. For kernel
revocation, withdraw the corresponding filter or close all copies of the socket.

## Budgets and background protection

A bounded token bucket regulates urgent datagrams per SDK service instance.
All wrappers created from that instance share its bucket. A budget or deadline
failure can suppress the datagram or submit it in the background class, according
to the caller's policy. Each result carries the effective class. An urgent
message without a deadline still consumes a token.

The SDK bucket regulates cooperation. Separate service instances or raw
sendmsg calls can bypass its aggregate limit. The isolated HTB backend can
attach one Linux packet policer to a trusted mark and priority match. It
enforces a shared packet budget at that filter across sockets and processes.
An over-budget skb continues classification and uses the default leaf in the
validated hierarchy. Use distinct marks and filters for independently policed
services. The public daemon configuration does not automatically reconcile
these filters. Each IPv4 or IPv6 filter has its own bucket. The experiments
verify separate families and do not combine the two buckets into a dual-stack
global budget.

The existing 12 Mbit/s HTB experiment reserves 30% for urgent traffic and 70%
for default traffic, caps urgent borrowing at 60%, and lets the default leaf
borrow up to the root rate. Both leaves use fq_codel. These are shaping
parameters. Actual delivery also depends on demand, queuing, loss and the
receiver. Results report background goodput, Jain fairness and intervals with
no observed flow delivery. CAKE DiffServ uses DSCP, so an operator must
independently manage DSCP trust when using that baseline.

## Scheduler scope

A deadline is an application submission constraint and a scheduling hint.
HTB does not expire already queued datagrams. FLOWCORE compares application
response time against the budget in the lab and counts missing responses as
failures. ETF with SO_TXTIME schedules a supplied transmit time. TAPRIO applies
traffic class gates. Neither substitutes for request deadlines, and the SDK
does not activate either mechanism. The earlier isolated probes and their
reproduction commands are in [SCHEDULER_PROTOTYPE.md](SCHEDULER_PROTOTYPE.md).
