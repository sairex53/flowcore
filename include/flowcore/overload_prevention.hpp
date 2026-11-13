#pragma once

#include <cstdint>
#include <functional>
#include <mutex>
#include <optional>
#include <span>

namespace flowcore::v2::overload {

inline constexpr std::uint64_t maximum_request_bytes = 64ULL * 1024 * 1024;
inline constexpr std::uint64_t maximum_observation_age_ns = 2'500'000'000ULL;

enum class Feasibility : std::uint32_t { Feasible, AtRisk, Infeasible, InsufficientEvidence };
enum class Recommendation : std::uint32_t { Accept, AcceptWithRisk, Defer, RejectRecommended, Unknown };
enum class Reason : std::uint32_t {
    Fits, Marginal, QueuePressure, HardDeadlineBound, Expired, MissingEvidence,
    StaleEvidence, Invalid, CapacityCollapsed, Recovering, FairnessFloorInfeasible
};
enum class Fairness : std::uint32_t { Unknown, FloorFeasible, FloorAtRisk, FloorImpossible, FloorObserved };

struct RequestCost {
    std::uint64_t deadline_ns = 0; // CLOCK_MONOTONIC, same boot and time namespace.
    std::uint64_t request_bytes = 0;
    std::optional<std::uint64_t> response_bytes; // Missing means unknown, zero means no response payload.
    std::optional<std::uint32_t> processing_p50_us, processing_p95_us;
    std::optional<std::uint32_t> processing_min_us; // Only a documented hard lower bound proves infeasibility.
    bool prefer_reliable = false;
    // App-observed rates may lower the predicted rate, but never establish a hard bound.
    std::optional<std::uint64_t> observed_request_rate_bytes_s, observed_response_rate_bytes_s;
};

struct Evidence {
    std::uint64_t observed_ns = 0;
    bool scope_verified = false;
    std::optional<std::uint64_t> available_bytes_s, backlog_bytes;
    std::optional<std::uint32_t> rtt_p50_us, rtt_p95_us;
    std::optional<std::uint64_t> response_available_bytes_s, response_backlog_bytes;
    std::optional<std::uint64_t> verified_max_bytes_s, verified_response_max_bytes_s;
    std::optional<std::uint32_t> verified_min_rtt_us;
    std::optional<std::uint32_t> active_concurrency, concurrency_limit, queue_depth;
    std::optional<std::uint64_t> capacity_bytes_s, background_floor_bytes_s, background_goodput_bytes_s;
    bool response_path_verified = false;
    bool operator_fairness_verified = false;
};

struct Assessment {
    Feasibility feasibility = Feasibility::InsufficientEvidence;
    Recommendation recommendation = Recommendation::Unknown;
    Reason reason = Reason::MissingEvidence;
    Fairness fairness = Fairness::Unknown;
    std::uint64_t remaining_us = 0;
    std::optional<std::uint64_t> hard_minimum_us, predicted_p50_us, predicted_p95_us;
    std::optional<std::uint64_t> uncertainty_us, queue_wait_p50_us, queue_wait_p95_us;
    bool queue_pressure = false;
    bool stale_evidence = false;
    bool callback_failed = false;
};

using AdmissionDecisionCallback = std::function<void(const Assessment&)>;

bool valid(const RequestCost&) noexcept;
Assessment assess(const RequestCost&, const Evidence&, std::uint64_t now_ns) noexcept;

enum class State : std::uint32_t { Normal, ApproachingCapacity, Overloaded, CapacityCollapse,
                                    Recovering, InsufficientData };
enum class Action : std::uint32_t { Hold, ReduceConcurrency, IncreaseConcurrency, NoIntervention };

struct ControllerConfig {
    std::uint32_t minimum_concurrency = 1;
    std::uint32_t maximum_concurrency = 64;
    std::uint32_t maximum_admissions_per_second = 256;
};

struct Observation {
    std::uint64_t sampled_ns = 0, capacity_bytes_s = 0, delivered_bytes_s = 0, backlog_bytes = 0;
    std::uint32_t active_concurrency = 0, queue_depth = 0;
    std::uint64_t offered_requests = 0, admitted_requests = 0, rejected_requests = 0;
    std::uint64_t completed_requests = 0, failed_requests = 0, deadline_misses = 0;
    std::uint64_t background_goodput_bytes_s = 0, background_floor_bytes_s = 0;
    bool scope_verified = false, counters_complete = false, operator_fairness_verified = false;
};

struct Update {
    State state = State::InsufficientData;
    Action action = Action::NoIntervention;
    Fairness fairness = Fairness::Unknown;
    std::uint32_t recommended_concurrency = 0, available_admission_slots = 0;
    std::uint32_t admission_budget_per_second = 0;
    bool changed = false, callback_failed = false;
};

class BackpressureController {
public:
    using Callback = std::function<void(const Update&)>;

    explicit BackpressureController(ControllerConfig);
    BackpressureController(const BackpressureController&) = delete;
    BackpressureController& operator=(const BackpressureController&) = delete;
    void set_callback(Callback);
    Update observe(const Observation&, std::uint64_t now_ns);
    void reset() noexcept;

private:
    ControllerConfig config_;
    mutable std::mutex mutex_;
    Callback callback_;
    State state_ = State::InsufficientData;
    std::uint32_t recommended_concurrency_ = 0, admission_budget_ = 0;
    std::uint64_t last_sample_ns_ = 0, previous_capacity_ = 0, last_action_ns_ = 0;
    unsigned pressure_streak_ = 0, healthy_streak_ = 0;
};

// Bounded, application-local ranking for already-authorized work. Priority is a
// three-level enum; this helper never changes network marks or kernel policy.
enum class ServicePriority : std::uint32_t { Background, Standard, Interactive, Critical };
struct Candidate {
    std::uint64_t id = 0, remaining_us = 0, predicted_p95_us = 0, bytes = 0;
    std::uint32_t fairness_debt_percent = 0;
    ServicePriority priority = ServicePriority::Standard;
};
std::optional<std::uint64_t> choose(std::span<const Candidate>) noexcept;

} // namespace flowcore::v2::overload
