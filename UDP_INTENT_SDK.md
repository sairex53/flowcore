# C++23 UDP intent SDK

Include `flowcore/intent_v2.hpp` and `flowcore/udp_intent.hpp` and link
`flowcore::sdk`. The application creates the UDP socket and manages replies.
A trusted launcher supplies a descriptor bearing the operator's socket mark.
Obtain one sender per socket from the authorized Service and reuse it.

```cpp
flowcore::v2::Intent intent;
intent.service = "udp_demo";
intent.priority = flowcore::v2::Priority::Critical;
flowcore::v2::Service service(control_socket, intent);
if (service.wait_ready(std::chrono::seconds(2))) {
    auto sender = service.udp_message_sender(connected_udp_fd);
    auto deadline = flowcore::v2::deadline::monotonic_ns() + 20'000'000;
    auto submitted = sender->send_with_deadline(payload, deadline,
        flowcore::udp::ExpirationPolicy::DemoteToBackground, 42);
    // submitted.status and submitted.applied_class describe local submission.
}
```

Use `send_message` for an explicit intent, `send_urgent` for budgeted priority,
and `send_background` to set the background class. The methods accept a
destination address for unconnected sockets. For a connected socket, leave it
absent. Registration is bounded by the existing daemon transport. Setup can
throw for unauthorized, unsupported or unmarked sockets. The application can
use ordinary UDP with its own fallback policy when setup fails.

Send calls default to MSG_DONTWAIT. A result identifies the local outcome,
submitted byte count, errno when available, applied class and application
identifier. The local monotonic timestamp records the send syscall return.
Local submission does not imply enqueue completion, delivery or deadline
attainment. The receiver or application must account for replies and timeouts.
A demoted expired message carries the local deadline miss flag.

`send_batch` uses an independent control buffer per datagram, with at most
64 messages per syscall and 4096 per API call. An oversized API batch returns
an empty result. Partial batch results identify which messages were submitted.
Keep spans and destination storage valid for the call. UDP corking and UDP
segmentation enabled at setup are rejected, and send flags that would merge
messages are disallowed. Use a dedicated socket and avoid changing options or
mixing SDK calls with external UDP corking. UDP GSO performance and mapped IPv4
destinations on IPv6 sockets are outside the validated path.

## Software TX timing

Opt in with `service.udp_message_sender(fd, true)`. The SDK uses
SO_TIMESTAMPING_NEW and drains a bounded number of error-queue records.
SOF_TIMESTAMPING_TX_SCHED reports a software scheduler timestamp and
SOF_TIMESTAMPING_TX_SOFTWARE reports a software transmit timestamp. Reports
use the realtime clock. Compare the two kernel stages within their clock domain.
The application uses monotonic time for deadlines and request latency. These
stages cannot establish physical transmission time or delivery. See the [Linux
timestamping documentation](https://docs.kernel.org/networking/timestamping.html).

Timestamp keys are 32 bits. When timestamping, use unique outstanding IDs
within that range, or allow the sender to assign them. Each timestamp can be
missing or arrive out of order. Use one timestamping owner per socket. The SDK
consumes the error queue and discards records outside the timestamp API. Leave
timestamping disabled if another component owns the error queue.

## Operator configuration

Set a specific UID, an operator-owned cgroup prefix and a launcher-assigned
mark. The following is the `intent_api` section of the normal service
configuration. Replace the example identity with the service's actual identity.

```json
{
  "priorities": ["standard", "critical"],
  "udp_message_policies": {
    "udp_demo": {
      "priority": "critical",
      "uids": [991],
      "cgroup_prefix": "/system.slice/udp-demo.service",
      "socket_mark": 42,
      "urgent_rate_pps": 100,
      "urgent_burst": 16,
      "max_deadline_ms": 100
    }
  }
}
```

`flowcore udp-intent capabilities --interface lo` inspects the current namespace.
Socket option acceptance and loaded module entries are reported separately
from observed queue behavior. The installed `flowcore-udp-intent-demo` accepts
a control socket path and an inherited marked, connected UDP descriptor.
See [PER_MESSAGE_INTENTS.md](PER_MESSAGE_INTENTS.md) for kernel classification
and the scope of quotas.
