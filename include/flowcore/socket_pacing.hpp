#pragma once

#include <chrono>
#include <cstdint>
#include <expected>
#include <optional>
#include <string>
#include <string_view>
#include <system_error>

namespace flowcore::v2 {
class Service;
namespace pacing {

using Rate = std::uint64_t; // bytes/s
using Clock = std::chrono::steady_clock;
using Error = std::error_code;

struct Settings {
    Rate maximum_rate = 0; // zero leaves the existing setting alone
    std::optional<std::uint32_t> notsent_lowat;
    std::optional<unsigned> priority;
};

struct Policy {
    std::string service;
    Rate minimum_cap = 0, maximum_cap = 0; // operator bounds on configured caps
    Settings initial;
    std::chrono::seconds lifetime{300};
    Rate preferred_throughput = 0; // advisory
    std::chrono::microseconds latency_target{}, request_deadline{}; // advisory
};

struct Snapshot {
    std::uint64_t cookie = 0;
    Rate maximum_rate = 0;
    std::uint32_t notsent_lowat = 0;
    unsigned priority = 0;
    std::string congestion;
    std::optional<Rate> pacing_rate, delivery_rate;
    std::optional<std::uint64_t> bytes_acked;
    std::optional<std::uint32_t> notsent_bytes, cwnd_segments, rtt_us, total_retransmits;
    std::optional<bool> ecn_negotiated;
    std::optional<unsigned> ecn_mode;
    std::optional<std::uint32_t> delivered_segments, delivered_ce_segments;
    std::optional<std::uint32_t> output_queue_bytes, send_buffer_bytes;
};

struct Capabilities {
    Error maximum_rate, priority, mark_read, mark_write, notsent_lowat, congestion, tcp_info, cookie;
};
std::expected<Capabilities, Error> probe() noexcept;
std::expected<Snapshot, Error> inspect(int fd);
// For an application-owned TCP socket. Linux enforces its current algorithm allowlist.
Error select_congestion(int fd, std::string_view algorithm);

class SocketPolicy {
public:
    static std::expected<SocketPolicy, Error> attach(int fd, const Policy&);
    SocketPolicy(SocketPolicy&&) noexcept;
    SocketPolicy& operator=(SocketPolicy&&) = delete;
    SocketPolicy(const SocketPolicy&) = delete;
    ~SocketPolicy();

    std::uint64_t cookie() const noexcept { return cookie_; }
    const Policy& policy() const noexcept { return policy_; }
    // Optional daemon attestation for request telemetry, repeated after re-registration.
    std::uint64_t attest(const Service&) const;
    std::expected<Snapshot, Error> snapshot() const;
    Error set_rate(Rate) noexcept;
    // Call from the owner's timer even when there is no traffic.
    Error tick(Clock::time_point now = Clock::now()) noexcept;
    Error remove() noexcept;

private:
    SocketPolicy(int fd, std::uint64_t cookie, Policy policy) noexcept;
    int fd_, owner_;
    std::uint64_t cookie_;
    Policy policy_;
    Clock::time_point expires_;
    struct Values {
        Rate rate = 0;
        std::uint32_t lowat = 0;
        int priority = 0;
    };
    Values original_, applied_;
    bool active_ = false;
};

enum class Strategy { Static, Queue, Slo, Hybrid, Ecn, Deadline, Integrated };
enum class Decision { Hold, Adjust, Rollback, Fallback, ClassOnly };
struct Observation {
    Rate capacity = 0, throughput = 0, background_demand = 0;
    std::uint64_t backlog_bytes = 0;
    std::uint32_t rtt_us = 0, request_p99_us = 0, requests = 0;
    unsigned active_sockets = 0;
    bool valid = false, class_controller_active = false, class_transition = false, ecn_pre_loss = false,
         deadline_pressure = false;
    Rate background_guarantee = 0; // aggregate bytes/s from verified HTB readback
};
struct Proposal { Decision decision; Rate rate; };

class Controller {
public:
    Controller(Strategy, Rate floor, Rate ceiling, Rate fallback, std::uint32_t target_us = 30'000);
    Proposal evaluate(const Observation&, Rate current, Clock::time_point now) noexcept;
    // Evaluate one shadow sample without recording a kernel action or counterfactual outcome.
    Proposal preview(const Observation&, Rate current, Clock::time_point now) noexcept;
    // Record the result after the caller verifies its entire local socket update.
    void confirm(bool success) noexcept;
private:
    Strategy strategy_;
    Rate floor_, ceiling_, fallback_, previous_ = 0;
    Rate last_throughput_ = 0, last_capacity_ = 0;
    std::uint32_t prior_p99_ = 0, target_us_;
    Clock::time_point last_action_{}, last_sample_{};
    unsigned prior_sockets_ = 0, pressure_ = 0, quiet_ = 0, actions_ = 0;
    bool evaluating_ = false, failed_ = false;
    Decision proposed_ = Decision::Hold;
};

} // namespace pacing
} // namespace flowcore::v2
