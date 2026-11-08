#pragma once
#include "flowcore/deadline.hpp"

#include <chrono>
#include <cstdint>
#include <memory>
#include <string>

namespace flowcore::v2 {
enum class Error : std::uint32_t { None, TransportUnavailable, AuthorizationDenied, InvalidRequest, EventRejected, ProtocolError };
struct TransportStatistics { std::uint64_t session_connections, session_requests, legacy_requests; };
enum class Outcome { Ok, Timeout, Failure, Cancelled };
enum class Priority { Standard, Interactive, Critical };
struct Intent {
    std::string service;
    Priority priority = Priority::Standard;
    std::uint32_t target_latency_us = 30000;
    std::uint32_t desired_kbit = 0, minimum_kbit = 0, maximum_kbit = 0;
    std::uint32_t deadline_us = 0, lifetime_seconds = 300;
    bool prefer_reliable = false, prefer_isolation = false;
};
enum class TransportKind { Tcp, Mptcp, Udp, Quic, Logical };
struct TransportHints {
    std::string session;
    TransportKind kind = TransportKind::Logical;
    bool multiplexed = false, mobility_preference = false;
    std::uint64_t cookie = 0;
};
struct Statistics {
    std::uint64_t queued, accepted, dropped, rejected, transport_errors;
};
namespace detail { struct Channel; }
class Request {
public:
    Request(Request&&) noexcept;
    Request(const Request&) = delete;
    Request& operator=(const Request&) = delete;
    ~Request();
    bool complete(Outcome = Outcome::Ok) noexcept;
    std::uint64_t id() const noexcept { return id_; }
private:
    friend class Service;
    friend class DeadlineRequest;
    bool finish_at(Outcome, std::uint64_t) noexcept;
    Request(std::shared_ptr<detail::Channel>, std::uint32_t, std::uint64_t,
            std::uint64_t stream = 0, unsigned priority = 0, bool logical = false, std::uint64_t absolute_ns = 0);
    std::shared_ptr<detail::Channel> channel_;
    std::uint64_t id_=0, start_=0, generation_=0, cookie_=0;
    std::uint32_t deadline_=0;
    std::uint64_t stream_=0, absolute_ns_=0;
    unsigned priority_=0;
    bool logical_=false;
};
struct DeadlineCompletion { bool missed, telemetry_queued; };
class DeadlineRequest {
public:
    DeadlineRequest(DeadlineRequest&&) noexcept;
    std::uint64_t id() const noexcept { return request_.id(); }
    std::chrono::nanoseconds remaining() const noexcept;
    bool should_cancel() const noexcept;
    bool progress(std::uint64_t completed_egress_bytes) noexcept;
    deadline::Work remaining_work() const noexcept;
    std::optional<DeadlineCompletion> complete(Outcome = Outcome::Ok) noexcept;
private:
    friend class Service;
    DeadlineRequest(Request request, deadline::Work work) noexcept;
    Request request_;
    deadline::Work work_;
    std::uint64_t completed_ = 0;
    bool finished_ = false;
};
class Service {
public:
    Service(std::string socket_path, Intent intent);
    Request begin_request(std::uint32_t deadline_us = 0, std::uint64_t cookie = 0) const;
    Request begin_stream_request(std::uint64_t stream_id, unsigned priority,
                                 std::uint32_t deadline_us = 0, std::uint64_t cookie = 0) const;
    DeadlineRequest begin_deadline_request(const deadline::Work&, std::uint64_t cookie = 0) const;
    DeadlineRequest begin_deadline_stream(const deadline::Work&, std::uint64_t stream_id,
                                          unsigned advisory_priority, std::uint64_t cookie = 0) const;
    // Bounded IPC for an optional advisory query. Use deadline::estimate for cached local evidence.
    deadline::Admission admit(const deadline::Work&, std::uint64_t cookie = 0) const;
    bool wait_ready(std::chrono::milliseconds timeout) const;
    bool flush(std::chrono::milliseconds timeout) const;
    bool ready() const noexcept;
    Statistics statistics() const noexcept;
    TransportStatistics transport_statistics() const noexcept;
    Error last_error() const noexcept;
    std::uint64_t registration_generation() const noexcept;
    std::uint64_t notifications_time_ns() const noexcept;
    // Blocking setup operation. Returned cookie is attested by a received FD.
    std::uint64_t associate_socket(int fd) const;
    // Blocking setup/diagnostic operations, never required in a request hot path.
    void declare_transport(const TransportHints&) const;
    void report_transport_state(const std::string& session, bool degraded,
                                const std::string& error = "none") const;
    // Application-owned socket only; 0..6 are ordinary Linux skb priorities.
    static void set_socket_priority(int fd, unsigned priority);
    // Blocking teardown; returns count of abandoned telemetry records, not network cancellations.
    std::uint64_t dissociate_socket(std::uint64_t cookie) const;
    // Latest coalesced state bits: SLO, congestion, fallback, interface, capacity.
    std::uint32_t notifications() const noexcept;
private:
    std::shared_ptr<detail::Channel> channel_;
};
}
