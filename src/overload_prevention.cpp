#include "flowcore/overload_prevention.hpp"

#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <utility>

namespace flowcore::v2::overload {
namespace {
constexpr std::uint64_t max_rate = 100'000'000'000ULL;
constexpr std::uint64_t queue_pressure_bytes = 1500;
constexpr std::uint64_t action_cooldown_ns = 1'000'000'000ULL;
constexpr std::uint64_t missing = std::numeric_limits<std::uint64_t>::max();

std::uint64_t saturating_add(std::uint64_t a, std::uint64_t b) noexcept {
    return b > std::numeric_limits<std::uint64_t>::max()-a
        ? std::numeric_limits<std::uint64_t>::max() : a+b;
}

std::optional<std::uint64_t> serialize_us(std::uint64_t bytes, std::uint64_t rate) noexcept {
    if (!rate || rate > max_rate) return {};
    const auto whole = bytes / rate;
    const auto rest = bytes % rate;
    if (whole > std::numeric_limits<std::uint64_t>::max()/1'000'000ULL) return {};
    const auto major = whole*1'000'000ULL;
    const auto minor = (rest*1'000'000ULL + rate-1)/rate;
    return saturating_add(major, minor);
}

bool valid_rate(const std::optional<std::uint64_t>& value) noexcept {
    return value && *value > 0 && *value <= max_rate;
}

std::optional<std::uint64_t> queue_wait(std::uint64_t bytes, std::uint64_t rate) noexcept {
    return serialize_us(bytes, rate);
}

Fairness fairness_for(const Evidence& e) noexcept {
    if (!e.operator_fairness_verified || !e.capacity_bytes_s ||
        *e.capacity_bytes_s > max_rate || !e.background_floor_bytes_s ||
        *e.background_floor_bytes_s > max_rate ||
        (e.background_goodput_bytes_s && *e.background_goodput_bytes_s > max_rate))
        return Fairness::Unknown;
    if (*e.capacity_bytes_s < *e.background_floor_bytes_s) return Fairness::FloorImpossible;
    if (!e.background_goodput_bytes_s) return Fairness::FloorAtRisk;
    return *e.background_goodput_bytes_s >= *e.background_floor_bytes_s
        ? Fairness::FloorObserved : Fairness::FloorAtRisk;
}

std::uint64_t optional_value(const std::optional<std::uint64_t>& value) noexcept {
    return value.value_or(missing);
}

std::uint64_t controller_fairness(const Observation& o) noexcept {
    if (!o.operator_fairness_verified) return static_cast<std::uint64_t>(Fairness::Unknown);
    if (o.capacity_bytes_s < o.background_floor_bytes_s)
        return static_cast<std::uint64_t>(Fairness::FloorImpossible);
    if (o.background_goodput_bytes_s >= o.background_floor_bytes_s)
        return static_cast<std::uint64_t>(Fairness::FloorObserved);
    return static_cast<std::uint64_t>(Fairness::FloorAtRisk);
}

bool valid_observation(const Observation& o) noexcept {
    if (!o.scope_verified || !o.sampled_ns || !o.capacity_bytes_s || o.capacity_bytes_s > max_rate ||
        o.delivered_bytes_s > max_rate || o.backlog_bytes > (1ULL<<34) ||
        o.active_concurrency > 1'000'000 || o.queue_depth > 1'000'000 ||
        o.background_floor_bytes_s > max_rate || o.background_goodput_bytes_s > max_rate)
        return false;
    if (!o.counters_complete) return true;
    return o.offered_requests <= (1ULL<<40) && o.admitted_requests <= o.offered_requests &&
           o.rejected_requests == o.offered_requests-o.admitted_requests &&
           o.completed_requests <= o.admitted_requests &&
           o.failed_requests == o.admitted_requests-o.completed_requests &&
           o.deadline_misses <= o.admitted_requests;
}
} // namespace

bool valid(const RequestCost& w) noexcept {
    const auto duration_ok = [](const std::optional<std::uint32_t>& value) {
        return !value || *value <= 600'000'000;
    };
    if (!w.deadline_ns || w.request_bytes > maximum_request_bytes ||
        (w.response_bytes && *w.response_bytes > maximum_request_bytes) ||
        !duration_ok(w.processing_p50_us) || !duration_ok(w.processing_p95_us) ||
        !duration_ok(w.processing_min_us)) return false;
    if (w.processing_min_us && w.processing_p50_us && *w.processing_min_us > *w.processing_p50_us)
        return false;
    if (w.processing_p50_us && w.processing_p95_us && *w.processing_p50_us > *w.processing_p95_us)
        return false;
    if ((w.observed_request_rate_bytes_s && !valid_rate(w.observed_request_rate_bytes_s)) ||
        (w.observed_response_rate_bytes_s && !valid_rate(w.observed_response_rate_bytes_s)))
        return false;
    return true;
}

Assessment assess(const RequestCost& w, const Evidence& e, std::uint64_t now) noexcept {
    Assessment a;
    if (!valid(w) || !now) { a.reason = Reason::Invalid; return a; }
    if ((e.backlog_bytes && *e.backlog_bytes > (1ULL<<34)) ||
        (e.response_backlog_bytes && *e.response_backlog_bytes > (1ULL<<34)) ||
        (e.capacity_bytes_s && *e.capacity_bytes_s > max_rate) ||
        (e.background_floor_bytes_s && *e.background_floor_bytes_s > max_rate) ||
        (e.background_goodput_bytes_s && *e.background_goodput_bytes_s > max_rate) ||
        (e.verified_min_rtt_us && *e.verified_min_rtt_us > 600'000'000) ||
        (e.active_concurrency && *e.active_concurrency > 1'000'000) ||
        (e.concurrency_limit && *e.concurrency_limit > 1'000'000) ||
        (e.queue_depth && *e.queue_depth > 1'000'000)) {
        a.reason = Reason::Invalid;
        return a;
    }
    if (w.deadline_ns <= now) {
        a.feasibility = Feasibility::Infeasible;
        a.recommendation = Recommendation::RejectRecommended;
        a.reason = Reason::Expired;
        a.remaining_us = 0;
        return a;
    }
    a.remaining_us = (w.deadline_ns-now)/1000;
    a.fairness = fairness_for(e);
    if (a.fairness == Fairness::FloorImpossible) a.reason = Reason::FairnessFloorInfeasible;
    if (!w.response_bytes || !w.processing_p50_us || !w.processing_p95_us ||
        !e.scope_verified || !e.observed_ns || e.observed_ns > now ||
        !valid_rate(e.available_bytes_s) || !e.backlog_bytes ||
        !e.rtt_p50_us || !e.rtt_p95_us || !*e.rtt_p50_us ||
        *e.rtt_p95_us < *e.rtt_p50_us || *e.rtt_p95_us > 600'000'000 ||
        (*w.response_bytes > 0 && (!valid_rate(e.response_available_bytes_s) ||
                                   !e.response_backlog_bytes))) {
        if (e.observed_ns && e.observed_ns <= now && now-e.observed_ns > maximum_observation_age_ns)
            a.reason = Reason::StaleEvidence;
        else if (a.reason != Reason::FairnessFloorInfeasible) a.reason = Reason::MissingEvidence;
        a.stale_evidence = a.reason == Reason::StaleEvidence;
        return a;
    }
    if (now-e.observed_ns > maximum_observation_age_ns) {
        a.reason = Reason::StaleEvidence;
        a.stale_evidence = true;
        return a;
    }

    const auto up_rate = w.observed_request_rate_bytes_s
        ? std::min(*e.available_bytes_s, *w.observed_request_rate_bytes_s) : *e.available_bytes_s;
    const auto up = serialize_us(w.request_bytes, up_rate);
    const auto response_path_rate = *w.response_bytes ? *e.response_available_bytes_s : e.available_bytes_s;
    const auto down_rate = *w.response_bytes && w.observed_response_rate_bytes_s
        ? std::min(*response_path_rate, *w.observed_response_rate_bytes_s) : *response_path_rate;
    const auto down_backlog = *w.response_bytes ? *e.response_backlog_bytes : std::optional<std::uint64_t>{0};
    const auto down = serialize_us(*w.response_bytes, down_rate);
    const auto up_queue = queue_wait(*e.backlog_bytes, *e.available_bytes_s);
    const auto down_queue = queue_wait(*down_backlog, down_rate);
    if (!up || !down || !up_queue || !down_queue) {
        a.reason = Reason::MissingEvidence;
        return a;
    }

    // One RTT represents path traversal; queue and payload serialization are
    // charged separately. The high estimate uses p95 RTT and service time.
    const auto network_p50 = std::max<std::uint64_t>(*e.rtt_p50_us,
        saturating_add(saturating_add(*up, *up_queue), saturating_add(*down, *down_queue)));
    const auto network_p95 = std::max<std::uint64_t>(*e.rtt_p95_us,
        saturating_add(saturating_add(*up, *up_queue), saturating_add(*down, *down_queue)));
    std::uint64_t wait_p50 = 0, wait_p95 = 0;
    const bool concurrency_known = e.active_concurrency && e.concurrency_limit &&
                                   e.queue_depth && *e.concurrency_limit > 0;
    if (concurrency_known && *e.active_concurrency >= *e.concurrency_limit && *e.queue_depth) {
        const auto waves = (*e.queue_depth + *e.concurrency_limit - 1)/ *e.concurrency_limit;
        wait_p50 = std::uint64_t(waves) * *w.processing_p50_us;
        wait_p95 = std::uint64_t(waves) * *w.processing_p95_us;
    }
    a.queue_pressure = *e.backlog_bytes >= queue_pressure_bytes ||
                       (*w.response_bytes > 0 && *e.response_backlog_bytes >= queue_pressure_bytes) ||
                       (concurrency_known && *e.active_concurrency >= *e.concurrency_limit && *e.queue_depth > 0);
    a.queue_wait_p50_us = wait_p50;
    a.queue_wait_p95_us = wait_p95;
    const auto low = saturating_add(saturating_add(network_p50, wait_p50), *w.processing_p50_us);
    const auto high = saturating_add(saturating_add(network_p95, wait_p95), *w.processing_p95_us);
    a.predicted_p50_us = low;
    a.predicted_p95_us = high;
    a.uncertainty_us = high >= low ? high-low : 0;

    // This bound is only used when an operator-verified maximum service rate
    // and an application-declared minimum processing time are both present.
    if (valid_rate(e.verified_max_bytes_s) &&
        (!w.request_bytes || e.verified_max_bytes_s) &&
        (w.response_bytes.value_or(0) == 0 || valid_rate(e.verified_response_max_bytes_s))) {
        const auto hard_up = w.request_bytes ? serialize_us(w.request_bytes, *e.verified_max_bytes_s) : std::optional<std::uint64_t>{0};
        const auto hard_down = *w.response_bytes ? serialize_us(*w.response_bytes, *e.verified_response_max_bytes_s) : std::optional<std::uint64_t>{0};
        if (hard_up && hard_down) {
            auto floor = saturating_add(*hard_up, *hard_down);
            if (e.verified_min_rtt_us) floor = saturating_add(floor, *e.verified_min_rtt_us);
            if (w.processing_min_us) floor = saturating_add(floor, *w.processing_min_us);
            a.hard_minimum_us = floor;
            if (floor > a.remaining_us) {
                a.feasibility = Feasibility::Infeasible;
                a.recommendation = Recommendation::RejectRecommended;
                a.reason = Reason::HardDeadlineBound;
                return a;
            }
        }
    }

    const auto margin_pct = w.prefer_reliable ? 25ULL : 10ULL;
    const auto guarded_high = saturating_add(high, high*margin_pct/100);
    const bool quality_complete = (e.response_path_verified || *w.response_bytes == 0) && concurrency_known &&
                                  e.operator_fairness_verified;
    if (guarded_high <= a.remaining_us && quality_complete) {
        a.feasibility = Feasibility::Feasible;
        a.recommendation = Recommendation::Accept;
        if (a.reason != Reason::FairnessFloorInfeasible) a.reason = Reason::Fits;
    } else if (a.queue_pressure && wait_p50 >= 1'000 &&
               saturating_add(wait_p50, low) < a.remaining_us) {
        a.feasibility = Feasibility::AtRisk;
        a.recommendation = Recommendation::Defer;
        a.reason = Reason::QueuePressure;
    } else {
        a.feasibility = Feasibility::AtRisk;
        a.recommendation = Recommendation::AcceptWithRisk;
        if (a.reason != Reason::FairnessFloorInfeasible) a.reason = Reason::Marginal;
    }
    return a;
}

BackpressureController::BackpressureController(ControllerConfig config) : config_(config) {
    if (!config_.minimum_concurrency || config_.minimum_concurrency > config_.maximum_concurrency ||
        config_.maximum_concurrency > 1'000'000 || !config_.maximum_admissions_per_second ||
        config_.maximum_admissions_per_second > 1'000'000)
        throw std::invalid_argument("invalid backpressure controller limits");
    recommended_concurrency_ = config_.maximum_concurrency;
    admission_budget_ = config_.maximum_admissions_per_second;
}

void BackpressureController::set_callback(Callback callback) {
    const std::lock_guard lock(mutex_);
    callback_ = std::move(callback);
}

Update BackpressureController::observe(const Observation& o, std::uint64_t now) {
    Callback callback;
    Update result;
    bool notify = false;
    {
        const std::lock_guard lock(mutex_);
        const auto prior_state = state_;
        const auto prior_concurrency = recommended_concurrency_;
        const auto prior_budget = admission_budget_;
        const bool coherent = valid_observation(o) && o.sampled_ns <= now &&
                              now-o.sampled_ns <= maximum_observation_age_ns;
        if (coherent && o.sampled_ns == last_sample_ns_) {
            const auto fairness = o.operator_fairness_verified ? controller_fairness(o) :
                                  static_cast<std::uint64_t>(Fairness::Unknown);
            return {state_, Action::Hold, static_cast<Fairness>(fairness), recommended_concurrency_,
                    o.active_concurrency < recommended_concurrency_
                        ? recommended_concurrency_-o.active_concurrency : 0,
                    admission_budget_, false, false};
        }
        const bool fresh = coherent && o.sampled_ns > last_sample_ns_;
        Action action = Action::Hold;
        if (!fresh) {
            state_ = State::InsufficientData;
            pressure_streak_ = healthy_streak_ = 0;
        } else {
            const bool collapsed = previous_capacity_ && o.capacity_bytes_s*100 <= previous_capacity_*85;
            const double utilization = static_cast<double>(o.delivered_bytes_s)/static_cast<double>(o.capacity_bytes_s);
            bool errors = false, healthy_errors = true;
            if (o.counters_complete && o.offered_requests) {
                const double failure_rate = static_cast<double>(o.failed_requests)/static_cast<double>(o.offered_requests);
                const double miss_rate = o.admitted_requests
                    ? static_cast<double>(o.deadline_misses)/static_cast<double>(o.admitted_requests) : 0.0;
                errors = failure_rate >= .05 || miss_rate >= .10;
                healthy_errors = failure_rate < .02 && miss_rate < .05;
            }
            const bool network_pressure = utilization >= .90 && o.backlog_bytes > 0;
            const bool service_pressure = o.queue_depth > 0 &&
                                          o.active_concurrency >= recommended_concurrency_;
            const bool pressure = network_pressure || service_pressure || errors;
            const bool healthy = utilization < .75 && o.backlog_bytes == 0 && o.queue_depth == 0 && healthy_errors;
            const bool was_pressured = state_ == State::Overloaded || state_ == State::CapacityCollapse ||
                                       state_ == State::Recovering || state_ == State::ApproachingCapacity;
            const auto cooldown_elapsed = !last_action_ns_ ||
                (now >= last_action_ns_ && now-last_action_ns_ >= action_cooldown_ns);

            if (collapsed) {
                state_ = State::CapacityCollapse;
                pressure_streak_ = 0;
                healthy_streak_ = 0;
                if (cooldown_elapsed) {
                    recommended_concurrency_ = std::max(config_.minimum_concurrency,
                        recommended_concurrency_*3/4);
                    admission_budget_ = std::max(1U, admission_budget_*3/4);
                    last_action_ns_ = now;
                    action = Action::ReduceConcurrency;
                }
            } else if (pressure) {
                ++pressure_streak_;
                healthy_streak_ = 0;
                state_ = pressure_streak_ >= 2 ? State::Overloaded : State::ApproachingCapacity;
                if (pressure_streak_ >= 2 && cooldown_elapsed) {
                    recommended_concurrency_ = std::max(config_.minimum_concurrency,
                        recommended_concurrency_*3/4);
                    admission_budget_ = std::max(1U, admission_budget_*3/4);
                    last_action_ns_ = now;
                    action = Action::ReduceConcurrency;
                }
            } else if (healthy && was_pressured) {
                pressure_streak_ = 0;
                ++healthy_streak_;
                state_ = State::Recovering;
                if (healthy_streak_ >= 3 && cooldown_elapsed) {
                    recommended_concurrency_ = std::min(config_.maximum_concurrency,
                                                         recommended_concurrency_+1);
                    const auto step = std::max(1U, config_.maximum_admissions_per_second/10);
                    admission_budget_ = std::min(config_.maximum_admissions_per_second,
                                                 admission_budget_+step);
                    healthy_streak_ = 0;
                    last_action_ns_ = now;
                    action = Action::IncreaseConcurrency;
                }
                if (recommended_concurrency_ == config_.maximum_concurrency && healthy_streak_ >= 2)
                    state_ = State::Normal;
            } else if (healthy) {
                state_ = State::Normal;
                pressure_streak_ = healthy_streak_ = 0;
            } else {
                state_ = State::ApproachingCapacity;
                pressure_streak_ = healthy_streak_ = 0;
            }

            previous_capacity_ = o.capacity_bytes_s;
            last_sample_ns_ = o.sampled_ns;
        }
        const bool changed = prior_state != state_ || prior_concurrency != recommended_concurrency_ ||
                             prior_budget != admission_budget_;
        const auto available = fresh && o.active_concurrency < recommended_concurrency_
            ? recommended_concurrency_-o.active_concurrency : 0;
        const auto fairness_value = fresh ? controller_fairness(o) :
                             static_cast<std::uint64_t>(Fairness::Unknown);
        result = {state_, action, static_cast<Fairness>(fairness_value), recommended_concurrency_, available,
                  admission_budget_, changed, false};
        notify = changed;
        callback = callback_;
    }
    if (notify && callback) {
        try { callback(result); }
        catch (...) { result.callback_failed = true; }
    }
    return result;
}

void BackpressureController::reset() noexcept {
    const std::lock_guard lock(mutex_);
    state_ = State::InsufficientData;
    recommended_concurrency_ = config_.maximum_concurrency;
    admission_budget_ = config_.maximum_admissions_per_second;
    last_sample_ns_ = previous_capacity_ = last_action_ns_ = 0;
    pressure_streak_ = healthy_streak_ = 0;
}

std::optional<std::uint64_t> choose(std::span<const Candidate> candidates) noexcept {
    const Candidate* best = nullptr;
    long double best_score = -1;
    for (const auto& candidate : candidates) {
        if (!candidate.id || !candidate.remaining_us || !candidate.predicted_p95_us ||
            candidate.predicted_p95_us > candidate.remaining_us ||
            candidate.bytes > maximum_request_bytes ||
            candidate.fairness_debt_percent > 1'000 ||
            candidate.priority > ServicePriority::Critical) continue;
        const long double slack = candidate.remaining_us-candidate.predicted_p95_us;
        const long double urgency = 1.0L/(1.0L+slack/10'000.0L);
        const long double size_cost = 1.0L+std::log2(1.0L+candidate.bytes/4096.0L)*.15L;
        const long double priority = 1.0L+static_cast<unsigned>(candidate.priority)*.2L;
        const long double fairness = 1.0L+std::min(candidate.fairness_debt_percent, 300U)/100.0L;
        const auto score = urgency*priority*fairness/size_cost;
        if (!best || score > best_score || (score == best_score && candidate.id < best->id)) {
            best = &candidate;
            best_score = score;
        }
    }
    if (!best) return {};
    return best->id;
}

} // namespace flowcore::v2::overload

// Narrow C bridge used by the optional Python experiment adapter. UINT64_MAX
// is the sole missing-value sentinel; the bridge never performs a policy write.
extern "C" int flowcore_overload_assess_v1(const std::uint64_t* work,
                                             const std::uint64_t* evidence,
                                             std::uint64_t now,
                                             std::uint64_t* output) noexcept {
    using namespace flowcore::v2::overload;
    if (!work || !evidence || !output) return 1;
    for (std::size_t i = 3; i <= 5; ++i)
        if (work[i] != missing && work[i] > UINT32_MAX) return 1;
    const auto optional = [](std::uint64_t value) -> std::optional<std::uint64_t> {
        return value == missing ? std::nullopt : std::optional{value};
    };
    RequestCost cost{work[0], work[1], optional(work[2]),
        optional(work[3]) ? std::optional{static_cast<std::uint32_t>(work[3])} : std::nullopt,
        optional(work[4]) ? std::optional{static_cast<std::uint32_t>(work[4])} : std::nullopt,
        optional(work[5]) ? std::optional{static_cast<std::uint32_t>(work[5])} : std::nullopt,
        work[6] != 0, optional(work[7]), optional(work[8])};
    const auto opt32 = [&](std::size_t i) -> std::optional<std::uint32_t> {
        if (evidence[i] == missing || evidence[i] > UINT32_MAX) return {};
        return static_cast<std::uint32_t>(evidence[i]);
    };
    Evidence sample{evidence[0], evidence[13] != 0, optional(evidence[1]), optional(evidence[3]),
        opt32(5), opt32(6), optional(evidence[2]), optional(evidence[4]), optional(evidence[10]),
        optional(evidence[11]), opt32(12), opt32(7), opt32(8), opt32(9), optional(evidence[14]),
        optional(evidence[15]), optional(evidence[16]), evidence[17] != 0, evidence[18] != 0};
    const auto result = assess(cost, sample, now);
    output[0] = static_cast<std::uint64_t>(result.feasibility);
    output[1] = static_cast<std::uint64_t>(result.recommendation);
    output[2] = static_cast<std::uint64_t>(result.reason);
    output[3] = static_cast<std::uint64_t>(result.fairness);
    output[4] = result.remaining_us;
    output[5] = optional_value(result.hard_minimum_us);
    output[6] = optional_value(result.predicted_p50_us);
    output[7] = optional_value(result.predicted_p95_us);
    output[8] = optional_value(result.uncertainty_us);
    output[9] = optional_value(result.queue_wait_p50_us);
    output[10] = optional_value(result.queue_wait_p95_us);
    output[11] = result.queue_pressure;
    output[12] = result.stale_evidence;
    return 0;
}

struct flowcore_overload_controller_v1 {
    flowcore::v2::overload::BackpressureController value;
    explicit flowcore_overload_controller_v1(flowcore::v2::overload::ControllerConfig config)
        : value(config) {}
};

extern "C" flowcore_overload_controller_v1* flowcore_overload_controller_create_v1(
    std::uint32_t minimum, std::uint32_t maximum, std::uint32_t budget) noexcept {
    try { return new flowcore_overload_controller_v1({minimum, maximum, budget}); }
    catch (...) { return nullptr; }
}

extern "C" void flowcore_overload_controller_destroy_v1(flowcore_overload_controller_v1* controller) noexcept {
    delete controller;
}

extern "C" int flowcore_overload_controller_observe_v1(flowcore_overload_controller_v1* controller,
    const std::uint64_t* sample, std::uint64_t now, std::uint64_t* output) noexcept {
    using namespace flowcore::v2::overload;
    if (!controller || !sample || !output) return 1;
    if (sample[4] > UINT32_MAX || sample[5] > UINT32_MAX) return 1;
    try {
        Observation observation{sample[0], sample[1], sample[2], sample[3],
            static_cast<std::uint32_t>(sample[4]), static_cast<std::uint32_t>(sample[5]),
            sample[6], sample[7], sample[8], sample[9], sample[10], sample[11],
            sample[12], sample[13], sample[14] != 0, sample[15] != 0, sample[16] != 0};
        const auto result = controller->value.observe(observation, now);
        output[0] = static_cast<std::uint64_t>(result.state);
        output[1] = static_cast<std::uint64_t>(result.action);
        output[2] = static_cast<std::uint64_t>(result.fairness);
        output[3] = result.recommended_concurrency;
        output[4] = result.available_admission_slots;
        output[5] = result.admission_budget_per_second;
        output[6] = result.changed;
        return 0;
    } catch (...) { return 2; }
}
