#pragma once
#include "flowcore/deadline.hpp"
#include "flowcore/overload_prevention.hpp"

#include <chrono>
#include <cstdint>
#include <expected>
#include <memory>
#include <optional>
#include <string>

namespace flowcore::udp { class MessageSender; }

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
enum class Capability : std::uint64_t {
    LeaseRenewal = 1ULL << 0, SocketAssociation = 1ULL << 1,
    SocketDissociation = 1ULL << 2, NotificationQuality = 1ULL << 3,
    TransportSessions = 1ULL << 4, DeadlineAdmission = 1ULL << 5,
    UdpPerMessageIntents = 1ULL << 6
};
struct CapabilitySnapshot {
    std::uint32_t protocol_version = 0;
    std::uint64_t features = 0;
    bool udp_message_authorized = false;
    bool supports(Capability value) const noexcept {
        return (features & static_cast<std::uint64_t>(value)) != 0;
    }
};
struct NotificationSnapshot {
    std::uint32_t flags = 0;
    std::uint64_t observed_ns = 0;
    std::uint64_t application_samples = 0;
    bool network_observed = false;
};
enum class PolicyState : std::uint32_t { Unknown = 0, RegisteredOnly = 1, KernelVerified = 2 };
struct EffectivePolicy {
    Intent accepted_intent;
    std::uint64_t generation = 0;
    PolicyState state = PolicyState::Unknown;
    std::uint32_t class_rate_kbit = 0;
    std::uint32_t class_ceil_kbit = 0;
    bool kernel_verified = false;
};
struct OpenError {
    Error code = Error::InvalidRequest;
    std::string message;
};
// Optional application-owned traffic counters. Byte fields are independent
// directions; local transport acceptance does not imply remote delivery.
struct TrafficAccounting {
    bool valid = false;
    std::uint64_t bytes_offered = 0, bytes_admitted = 0;
    std::uint64_t bytes_transmitted = 0, bytes_received = 0;
    std::uint32_t udp_messages_attempted = 0, udp_messages_transmitted = 0;
    std::uint32_t udp_send_failures = 0, udp_urgent_messages = 0, udp_background_messages = 0;
};
namespace detail { struct Channel; }
class Request {
public:
    Request(Request&&) noexcept;
    Request& operator=(Request&&) noexcept;
    Request(const Request&) = delete;
    Request& operator=(const Request&) = delete;
    ~Request();
    bool complete(Outcome = Outcome::Ok) noexcept;
    bool complete(Outcome, const TrafficAccounting&) noexcept;
    bool cancel() noexcept { return complete(Outcome::Cancelled); }
    std::uint64_t id() const noexcept { return id_; }
private:
    friend class Service;
    friend class DeadlineRequest;
    bool finish_at(Outcome, std::uint64_t, const TrafficAccounting* = nullptr) noexcept;
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
    DeadlineRequest& operator=(DeadlineRequest&&) noexcept;
    std::uint64_t id() const noexcept { return request_.id(); }
    std::chrono::nanoseconds remaining() const noexcept;
    bool should_cancel() const noexcept;
    bool progress(std::uint64_t completed_egress_bytes) noexcept;
    deadline::Work remaining_work() const noexcept;
    std::optional<DeadlineCompletion> complete(Outcome = Outcome::Ok) noexcept;
    std::optional<DeadlineCompletion> complete(Outcome, const TrafficAccounting&) noexcept;
    std::optional<DeadlineCompletion> cancel() noexcept { return complete(Outcome::Cancelled); }
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
    Service(const Service&) = default; // Copies share one lease, queue and background connection.
    Service& operator=(const Service&) = default;
    Service(Service&&) noexcept = default;
    Service& operator=(Service&&) noexcept = default;
    // Non-throwing local construction. Daemon availability remains asynchronous and optional.
    static std::expected<Service, OpenError> try_open(std::string socket_path, Intent intent) noexcept;
    Request begin_request(std::uint32_t deadline_us = 0, std::uint64_t cookie = 0) const;
    Request begin_request(std::chrono::microseconds deadline, std::uint64_t cookie = 0) const;
    Request begin_stream_request(std::uint64_t stream_id, unsigned priority,
                                 std::uint32_t deadline_us = 0, std::uint64_t cookie = 0) const;
    Request begin_stream_request(std::uint64_t stream_id, unsigned priority,
                                 std::chrono::microseconds deadline, std::uint64_t cookie = 0) const;
    DeadlineRequest begin_deadline_request(const deadline::Work&, std::uint64_t cookie = 0) const;
    DeadlineRequest begin_deadline_stream(const deadline::Work&, std::uint64_t stream_id,
                                          unsigned advisory_priority, std::uint64_t cookie = 0) const;
    // Bounded IPC for an optional advisory query. Use deadline::estimate for cached local evidence.
    deadline::Admission admit(const deadline::Work&, std::uint64_t cookie = 0) const;
    // Local 2.0 estimate over caller-owned request costs and a sampled evidence snapshot.
    // It is advisory, performs no IPC and cannot change kernel policy.
    overload::Assessment assess(const overload::RequestCost&, const overload::Evidence&,
                                std::uint64_t now_ns = deadline::monotonic_ns()) const noexcept;
    overload::Assessment assess(const overload::RequestCost&, const overload::Evidence&,
                                const overload::AdmissionDecisionCallback&,
                                std::uint64_t now_ns = deadline::monotonic_ns()) const noexcept;
    bool wait_ready(std::chrono::milliseconds timeout) const;
    bool flush(std::chrono::milliseconds timeout) const;
    bool ready() const noexcept;
    Statistics statistics() const noexcept;
    TransportStatistics transport_statistics() const noexcept;
    CapabilitySnapshot capabilities() const noexcept;
    NotificationSnapshot notification_snapshot() const noexcept;
    // Opt in to periodic empty-batch polling for degradation notifications. Off by default.
    void subscribe_notifications(bool enabled = true) const noexcept;
    Error last_error() const noexcept;
    std::uint64_t registration_generation() const noexcept;
    std::uint64_t notifications_time_ns() const noexcept;
    // Synchronous, read-only and bounded to the local IPC timeout. Kernel state is reported
    // separately from an accepted intent; a registration is not itself a QoS guarantee.
    std::expected<EffectivePolicy, OpenError> effective_policy() const noexcept;
    // Blocking setup operation. Returned cookie is attested by a received FD.
    std::uint64_t associate_socket(int fd) const;
    // Requires a daemon-authorized service lease and a socket bearing the operator-required SO_MARK.
    std::unique_ptr<flowcore::udp::MessageSender> udp_message_sender(int fd, bool enable_timestamps = false) const;
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
