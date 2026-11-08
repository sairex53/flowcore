# Deadline-Aware Networking Engine

FLOWCORE estimates feasibility for work declared by a cooperating application. A deadline belongs to a logical request. The daemon can associate the request with an application-owned socket, while Linux classifies the socket's packets under the existing operator policy. The request and its estimate confer no additional traffic privileges.

## Request lifecycle

The C++23 SDK uses Linux CLOCK_MONOTONIC nanoseconds for absolute deadlines. Compare timestamps within the same boot and time namespace. The supported future window is 600 seconds, with up to 64 MiB of declared remaining application egress bytes. A request may reserve explicit microsecond budgets for application processing, connection establishment and remote processing. Leave unknown components unset. Zero explicitly declares that a component has no reserved budget.

Use the existing service lease and optional FD-based socket association. On reconnect, associate the new socket before querying its path. You can query an existing request's remaining work with the replacement cookie. Its original lifecycle telemetry still uses the original cookie, so close the original request scope before dissociating that socket if you need a terminal report. If the application treats a retry as a new request, create a new scope with the original absolute deadline and the remaining work. Stream scopes keep logical request identity and advisory priorities independent on a shared connection.

~~~cpp
#include <flowcore/intent_v2.hpp>

namespace v2 = flowcore::v2;
namespace d = v2::deadline;

void report_request(v2::Service& service, std::uint64_t cookie) {
    d::Work work;
    work.deadline_ns = d::monotonic_ns() + 40'000'000;
    work.egress_bytes = 4096;
    work.connect_us = 0; // Connection already established.
    work.expiration = d::Expiration::CancelCooperatively;

    auto request = service.begin_deadline_request(work, cookie);
    // Run the application's I/O here. Progress means application-reported bytes.
    request.progress(4096);
    const auto status = request.should_cancel() ? v2::Outcome::Cancelled : v2::Outcome::Ok;
    const auto completion = request.complete(status);
    // completion->missed is local. completion->telemetry_queued is best-effort.
}
~~~

The owner calls completion, timeout, failure or cancellation explicitly. A scope that leaves without completion attempts to record cancellation through the existing bounded asynchronous queue. Progress is monotonic, local to the scope and usable for a fresh admission query. It records the caller's definition of completed egress bytes. It does not attest that the peer acknowledged them. Serialize access to each request scope. Independent requests can share one Service.

The daemon negotiates **deadline_admission_v1** inside the Intent v2 protocol. Negotiated peers send both the precise absolute deadline and the rounded microsecond compatibility budget. Legacy peers receive the rounded budget. The observer compares the terminal timestamp with the absolute value when available. A successful request at the deadline attains it. Cancellation, failure and timeout count as misses for a declared deadline. The SDK still reports a local result if telemetry is dropped or the daemon is offline.

## Flow policy and adaptation

HTB+fq_codel provides the same class-level QoS used elsewhere in FLOWCORE. Linux may use socket priority and pacing when the application owns the socket and the configured kernel scheduler supports the requested behavior. Request expiry never rewrites TCP state. HTTP/2, HTTP/3 and persistent connections can carry multiple intents with distinct deadlines. The class and pacing controls apply to the whole flow, so packet urgency for individual encrypted streams is unavailable. The application can instead defer work or limit concurrency before adding to its own queue.

The Research Lab integrates the deadline signal with the native pacing controller. A one-second application window needs at least 12 usable critical estimates. At least 25% must indicate tight forecasts with 2 ms or more of estimated class queueing. The native controller also requires fresh scoped feedback, measured background demand, about 8 ms of aggregate root backlog and a stable capacity and socket cohort for its early branch. It can reduce the maximum pacing rate of application-owned background TCP sockets by up to 12.5% per early action. The existing hybrid queue/SLO strategy remains the fallback for ordinary pressure and can take its existing bounded 25% steps. One-second cooldown, a deadband, an action budget, readback, group rollback and the class controller lock apply. There is no deadline-driven production actuator. A missing or failed observation can hold or restore the configured safe pacing baseline.

The research manifest rejects configurations where the derived one-flow pacing floor exceeds its initial cap. In the reference scenario the floor is 300,000 B/s, enough cap for one bulk flow to use the largest configured background HTB reservation if demand and the transport allow. The cap is an upper bound and the HTB rate a scheduler reservation. Neither predicts application goodput. Per-flow fairness is reported as an observed metric.

See [admission semantics](ADMISSION_CONTROL.md) and [experimental evidence](DEADLINE_EXPERIMENTS.md).
