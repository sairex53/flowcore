#pragma once

#include <chrono>
#include <cstdint>
#include <optional>

namespace flowcore::v2::deadline {

inline constexpr std::uint64_t maximum_budget_ns = 600'000'000'000ULL;
inline constexpr std::uint64_t maximum_payload_bytes = 64 * 1024 * 1024;
enum class Reliability { Balanced, PreferReliable };
enum class Expiration { Observe, CancelCooperatively };
enum class Decision { Accept, AcceptWithRisk, Defer, RejectInfeasible, InsufficientData };
enum class Reason { Fits, Tight, QueuePressure, Expired, CapacityBound, MissingEvidence,
                    Offline, Invalid, Unauthorized, BudgetExhausted, Throttled };
enum class Quality { None, Limited, Observed };

struct Work {
    std::uint64_t deadline_ns = 0; // Linux CLOCK_MONOTONIC, same boot and time namespace
    std::uint64_t egress_bytes = 0; // remaining application bytes on the controlled sender
    std::optional<std::uint32_t> processing_us, connect_us, remote_us;
    Reliability reliability = Reliability::Balanced;
    Expiration expiration = Expiration::Observe;
};

struct Evidence {
    std::uint64_t observed_ns = 0;
    std::optional<std::uint64_t> capacity_bytes_s, observed_rate_bytes_s, backlog_bytes;
    std::optional<std::uint32_t> rtt_p50_us, rtt_p95_us, transfer_p95_us;
    bool owned_egress = false, class_scoped = false;
    std::optional<std::uint64_t> hard_burst_bytes; // only for a verified upper service envelope
};

struct Admission {
    Decision decision = Decision::InsufficientData;
    Reason reason = Reason::MissingEvidence;
    Quality quality = Quality::None;
    std::uint64_t remaining_ns = 0;
    std::optional<std::uint64_t> capacity_bound_us, queue_us, network_p50_us, network_p95_us;
    std::uint64_t reserved_us = 0;
};

std::uint64_t monotonic_ns() noexcept;
bool valid(const Work&) noexcept;
Admission estimate(const Work&, const Evidence&, std::uint64_t now_ns) noexcept;

} // namespace flowcore::v2::deadline
