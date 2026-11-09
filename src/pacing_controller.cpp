#include "flowcore/socket_pacing.hpp"

#include <algorithm>
#include <stdexcept>

namespace flowcore::v2::pacing {
namespace {
using namespace std::chrono_literals;
constexpr Rate maximum_supported_rate = 100'000'000'000ULL;
constexpr unsigned action_budget = 16;
constexpr auto minimum_window = 500ms;
constexpr auto cooldown = 1s;
}

Controller::Controller(Strategy strategy, Rate floor, Rate ceiling, Rate fallback,
                       std::uint32_t target_us)
    : strategy_(strategy), floor_(floor), ceiling_(ceiling), fallback_(fallback), target_us_(target_us) {
    if (strategy > Strategy::Guarded || !floor || floor > fallback || fallback > ceiling ||
        ceiling > maximum_supported_rate || !target_us || target_us > 30'000'000)
        throw std::invalid_argument("invalid pacing controller bounds");
}

Proposal Controller::evaluate(const Observation& o, Rate current, Clock::time_point now) noexcept {
    if (proposed_ != Decision::Hold) return {Decision::Hold, current};
    if (strategy_ == Strategy::Guarded) return guarded(o, current, now);
    const bool integrated = strategy_ == Strategy::Integrated;
    if (integrated && (!o.valid || !o.capacity || !o.active_sockets ||
                       !o.background_guarantee || o.background_guarantee > o.capacity ||
                       o.class_controller_active || o.class_transition)) {
        evaluating_ = false;
        pressure_ = quiet_ = 0;
        last_capacity_ = 0;
        if (o.active_sockets && current != fallback_) {
            proposed_ = Decision::Fallback;
            last_action_ = now;
            ++actions_;
            return {proposed_, fallback_};
        }
        return {Decision::Hold, current};
    }
    // A live class controller owns the actuator domain, including its evaluation window.
    if (o.class_controller_active || o.class_transition) {
        evaluating_ = false;
        pressure_ = quiet_ = 0;
        return {o.class_controller_active ? Decision::ClassOnly : Decision::Hold, current};
    }
    if (strategy_ == Strategy::Static || !o.active_sockets ||
        (last_sample_ != Clock::time_point{} && now-last_sample_ < minimum_window)) return {Decision::Hold, current};
    last_sample_ = now;

    const auto propose = [&](Decision decision, Rate rate) -> Proposal {
        proposed_ = decision;
        last_action_ = now;
        ++actions_;
        return {decision, rate};
    };
    if (last_action_ != Clock::time_point{} && now-last_action_ < cooldown) return {Decision::Hold, current};
    if (failed_ || !o.valid || !o.capacity || o.capacity > maximum_supported_rate || actions_ >= action_budget) {
        evaluating_ = false;
        pressure_ = quiet_ = 0;
        if (current != fallback_ && actions_ <= action_budget) return propose(Decision::Fallback, fallback_);
        return {Decision::Hold, current};
    }
    if (current < floor_ || current > ceiling_) return {Decision::Hold, current};
    const bool regime_changed = o.capacity != last_capacity_ || o.active_sockets != prior_sockets_;
    last_capacity_ = o.capacity;
    prior_sockets_ = o.active_sockets;
    if (regime_changed) {
        if (integrated && current != fallback_)
            return propose(Decision::Fallback, fallback_);
        pressure_ = quiet_ = 0;
        evaluating_ = false;
    }
    if (evaluating_) {
        evaluating_ = false;
        if (integrated && (o.throughput < o.background_guarantee ||
                           (prior_p99_ && o.requests >= 12 && o.request_p99_us > prior_p99_ * 1.10)))
            return propose(Decision::Rollback, previous_);
        if (o.throughput < last_throughput_/2 && (!prior_p99_ || o.requests < 12 ||
                                                 o.request_p99_us >= prior_p99_))
            return propose(Decision::Rollback, previous_);
    }

    const bool backlog = o.backlog_bytes >= o.capacity/125; // 8 ms at the configured local rate
    const bool demand = o.background_demand >= o.capacity/4;
    const bool rtt_pressure = o.rtt_us >= 12'000;
    const bool slo_risk = o.requests >= 12 && o.request_p99_us > target_us_;
    const bool early = !regime_changed && ((strategy_ == Strategy::Ecn && o.ecn_pre_loss) ||
                                          (strategy_ == Strategy::Deadline && backlog && o.deadline_pressure) ||
                                          (integrated && (o.ecn_pre_loss || (backlog && o.deadline_pressure))));
    const bool congested = demand && (early || (backlog &&
        (strategy_ == Strategy::Queue || (strategy_ == Strategy::Slo ? slo_risk : slo_risk || rtt_pressure))));
    pressure_ = congested ? std::min(pressure_+1, 2U) : 0;
    quiet_ = !backlog && !slo_risk && o.throughput < o.capacity*3/4 ? std::min(quiet_+1, 3U) : 0;
    Rate desired = current;
    if (pressure_ >= 2 || (early && pressure_ >= 1)) {
        const bool hybrid = strategy_ == Strategy::Hybrid || strategy_ == Strategy::Ecn || strategy_ == Strategy::Deadline;
        const Rate budget = o.capacity*(hybrid ? 70 : 60)/100;
        desired = std::max(current-current/(early ? 8 : 4), std::min(current, budget/o.active_sockets));
    } else if (quiet_ >= 3 && o.background_demand > o.throughput) {
        desired = std::min(fallback_, current+current/4);
    }
    Rate protected_floor = floor_;
    if (integrated) {
        protected_floor = std::max(floor_, (o.background_guarantee + o.active_sockets - 1)/o.active_sockets);
        if (protected_floor > fallback_)
            return {Decision::Hold, current};
        // The initial cap is non-binding at the admitted link rate. Bound the
        // first effective change using measured bulk progress, not that cap.
        if (desired < current) {
            const Rate budget = std::max(o.capacity*75/100, o.throughput*85/100);
            desired = std::min(current, std::max(protected_floor, budget/o.active_sockets));
        }
    }
    desired = std::clamp(desired, protected_floor, ceiling_);
    const Rate difference = std::max(desired,current)-std::min(desired,current);
    if (difference == 0 || difference < current/8) return {Decision::Hold, current};
    previous_ = current;
    prior_p99_ = o.requests >= 12 ? o.request_p99_us : 0;
    last_throughput_ = o.throughput;
    pressure_ = quiet_ = 0;
    return propose(Decision::Adjust, desired);
}

Proposal Controller::guarded(const Observation& o, Rate current, Clock::time_point now) noexcept {
    const auto hold = [&](Reason reason) { return Proposal{Decision::Hold, current, reason}; };
    const auto propose = [&](Decision decision, Rate rate, Reason reason) {
        proposed_ = decision;
        last_action_ = now;
        ++actions_;
        return Proposal{decision, rate, reason};
    };
    const auto reset = [&] {
        baseline_windows_ = outcome_windows_ = goodput_bad_ = latency_bad_ = 0;
        reference_goodput_ = 0;
        evaluating_ = false;
        receiver_end_ = {};
    };
    const bool scoped = o.valid && o.active_sockets && o.active_sockets <= 32 && o.capacity &&
        o.capacity <= fallback_ && o.background_guarantee && o.background_ceiling >= o.background_guarantee &&
        o.background_ceiling <= o.capacity && !o.class_controller_active && !o.class_transition;
    const bool changed = last_capacity_ != o.capacity || prior_sockets_ != o.active_sockets;
    last_capacity_ = o.capacity;
    prior_sockets_ = o.active_sockets;
    if (!scoped || changed || failed_ || actions_ >= action_budget) {
        reset();
        if (o.active_sockets && current != fallback_)
            return propose(Decision::Fallback, fallback_, Reason::ScopeLost);
        return hold(Reason::ScopeLost);
    }
    if (current < floor_ || current > ceiling_) return hold(Reason::ScopeLost);

    // Outcome windows are independently timestamped at the receiver. Neither
    // a repeated counter sample nor an interval crossing a write can qualify.
    const bool measured = o.receiver_begin < o.receiver_end && o.receiver_end <= now &&
        o.receiver_end-o.receiver_begin >= 750ms && o.receiver_end-o.receiver_begin <= 1500ms &&
        now-o.receiver_end <= 1500ms && o.receiver_rate <= maximum_supported_rate;
    if (evaluating_ && now-verified_ >= 4s) {
        reset();
        return propose(Decision::Rollback, fallback_, Reason::UnprovenBenefit);
    }
    if (!measured || o.receiver_begin < receiver_end_ || o.requests < 12)
        return hold(Reason::InsufficientEvidence);
    receiver_end_ = o.receiver_end;
    if (evaluating_) {
        if (o.receiver_begin < verified_ || o.request_begin < verified_) return hold(Reason::Stabilizing);
        ++outcome_windows_;
        const auto minimum = std::max(o.background_guarantee, reference_goodput_*90/100);
        goodput_bad_ += o.receiver_rate < minimum;
        latency_bad_ += o.request_p99_us > prior_p99_ + prior_p99_/10;
        if (outcome_windows_ < 2) return hold(Reason::Stabilizing);
        if (goodput_bad_ == 2 || latency_bad_ == 2) {
            const auto reason = goodput_bad_ == 2 ? Reason::GoodputRegression : Reason::LatencyRegression;
            reset();
            return propose(Decision::Rollback, fallback_, reason);
        }
        // Every cap is a short probe. Even a favorable observation does not
        // grant a persistent restriction or imply a causal latency benefit.
        reset();
        return propose(Decision::Rollback, fallback_, Reason::UnprovenBenefit);
    }
    if (last_action_ != Clock::time_point{} && now-last_action_ < 2s)
        return hold(Reason::Stabilizing);
    if (o.receiver_rate < o.background_guarantee || !o.background_demand ||
        o.backlog_bytes < o.capacity/125 || o.critical_backlog == 0 || o.request_p99_us <= target_us_) {
        baseline_windows_ = 0;
        reference_goodput_ = 0;
        return hold(Reason::InsufficientEvidence);
    }
    reference_goodput_ = std::max(reference_goodput_, o.receiver_rate);
    if (++baseline_windows_ < 2) return hold(Reason::Stabilizing);
    const auto aggregate = std::max(o.background_guarantee, reference_goodput_*97/100);
    const auto target = std::max(floor_, (aggregate+o.active_sockets-1)/o.active_sockets);
    if (target >= current || target*o.active_sockets >= o.background_ceiling) {
        baseline_windows_ = 0;
        reference_goodput_ = 0;
        return hold(Reason::Redundant);
    }
    prior_p99_ = o.request_p99_us;
    return propose(Decision::Adjust, target, Reason::LocalPressure);
}

Proposal Controller::preview(const Observation& observation, Rate current, Clock::time_point now) noexcept {
    if (strategy_ == Strategy::Guarded) {
        auto preview = *this;
        return preview.evaluate(observation, current, now);
    }
    const auto previous = previous_;
    const auto last_throughput = last_throughput_;
    const auto prior_p99 = prior_p99_;
    const auto last_action = last_action_;
    const auto actions = actions_;
    const auto evaluating = evaluating_;
    const auto proposed = proposed_;
    const auto result = evaluate(observation, current, now);
    previous_ = previous;
    last_throughput_ = last_throughput;
    prior_p99_ = prior_p99;
    last_action_ = last_action;
    actions_ = actions;
    evaluating_ = evaluating;
    proposed_ = proposed;
    return result;
}

void Controller::confirm(bool success) noexcept {
    confirm(success, Clock::now());
}

void Controller::confirm(bool success, Clock::time_point verified) noexcept {
    if (proposed_ == Decision::Hold) return;
    evaluating_ = success && proposed_ == Decision::Adjust;
    if (evaluating_) verified_ = verified;
    failed_ = failed_ || !success;
    proposed_ = Decision::Hold;
}

} // namespace flowcore::v2::pacing
