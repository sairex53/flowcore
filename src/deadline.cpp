#include "flowcore/deadline.hpp"

#include <algorithm>
#include <ctime>

namespace flowcore::v2::deadline {
namespace {
constexpr std::uint64_t max_rate = 100'000'000'000ULL;
constexpr std::uint64_t max_sample_age_ns = 2'500'000'000ULL;
constexpr std::uint64_t max_backlog = 1ULL << 34;

std::uint64_t serialization(std::uint64_t bytes, std::uint64_t rate) noexcept {
    return (bytes*1'000'000 + rate-1)/rate;
}
bool duration(const std::optional<std::uint32_t>& value) noexcept {
    return !value || *value <= maximum_budget_ns/1000;
}
}

std::uint64_t monotonic_ns() noexcept {
    timespec ts{};
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return std::uint64_t(ts.tv_sec)*1'000'000'000 + ts.tv_nsec;
}

bool valid(const Work& w) noexcept {
    return w.deadline_ns && w.egress_bytes <= maximum_payload_bytes
        && duration(w.processing_us) && duration(w.connect_us) && duration(w.remote_us)
        && (w.reliability == Reliability::Balanced || w.reliability == Reliability::PreferReliable)
        && (w.expiration == Expiration::Observe || w.expiration == Expiration::CancelCooperatively);
}

Admission estimate(const Work& w, const Evidence& e, std::uint64_t now) noexcept {
    Admission a;
    if (!valid(w) || (w.deadline_ns > now && w.deadline_ns-now > maximum_budget_ns)) {
        a.reason = Reason::Invalid;
        return a;
    }
    if (now >= w.deadline_ns) {
        a.decision = Decision::RejectInfeasible;
        a.reason = Reason::Expired;
        return a;
    }
    a.remaining_ns = w.deadline_ns-now;
    a.reserved_us = std::uint64_t(w.processing_us.value_or(0)) + w.connect_us.value_or(0) + w.remote_us.value_or(0);
    if (a.reserved_us > a.remaining_ns/1000) {
        a.decision = Decision::RejectInfeasible;
        a.reason = Reason::BudgetExhausted;
        a.quality = Quality::Limited;
        return a;
    }
    if (!e.owned_egress || e.observed_ns == 0 || e.observed_ns > now || now-e.observed_ns > max_sample_age_ns
        || !e.capacity_bytes_s || *e.capacity_bytes_s == 0 || *e.capacity_bytes_s > max_rate)
        return a;
    // With integer microseconds, use a floor for the impossibility bound.
    const auto capacity = *e.capacity_bytes_s;
    if (e.hard_burst_bytes) {
        const auto remaining = w.egress_bytes-std::min(w.egress_bytes, *e.hard_burst_bytes);
        a.capacity_bound_us = remaining*1'000'000/capacity;
    }
    if (a.capacity_bound_us && *a.capacity_bound_us > a.remaining_ns/1000) {
        a.decision = Decision::RejectInfeasible;
        a.reason = Reason::CapacityBound;
        a.quality = Quality::Limited;
        return a;
    }
    if (!e.class_scoped || !e.observed_rate_bytes_s || !*e.observed_rate_bytes_s
        || *e.observed_rate_bytes_s > max_rate || !e.backlog_bytes || *e.backlog_bytes > max_backlog
        || !e.rtt_p50_us || !*e.rtt_p50_us || !e.rtt_p95_us || *e.rtt_p95_us < *e.rtt_p50_us
        || !duration(e.rtt_p95_us) || !duration(e.transfer_p95_us))
        return a;

    const auto rate = std::min(capacity, *e.observed_rate_bytes_s);
    const auto transfer = serialization(w.egress_bytes, rate);
    a.queue_us = serialization(*e.backlog_bytes, rate);
    // RTT already includes some queue residence. Use the larger indication.
    a.network_p50_us = transfer + std::max<std::uint64_t>(*a.queue_us, *e.rtt_p50_us);
    a.network_p95_us = std::max(transfer + std::max<std::uint64_t>(*a.queue_us, *e.rtt_p95_us),
                                std::uint64_t(e.transfer_p95_us.value_or(0)));
    const auto left = a.remaining_ns/1000;
    const auto margin = w.reliability == Reliability::PreferReliable ? 25 : 10;
    const auto high = *a.network_p95_us + a.reserved_us;
    const auto typical = *a.network_p50_us + a.reserved_us;
    a.quality = e.transfer_p95_us && w.processing_us && w.connect_us && w.remote_us ? Quality::Observed : Quality::Limited;
    if (high + high*margin/100 <= left) {
        a.decision = a.quality == Quality::Observed ? Decision::Accept : Decision::AcceptWithRisk;
        a.reason = a.quality == Quality::Observed ? Reason::Fits : Reason::Tight;
    } else if (typical > left && *a.queue_us >= 2'000) {
        a.decision = Decision::Defer;
        a.reason = Reason::QueuePressure;
    } else {
        a.decision = Decision::AcceptWithRisk;
        a.reason = Reason::Tight;
    }
    return a;
}

} // namespace flowcore::v2::deadline
