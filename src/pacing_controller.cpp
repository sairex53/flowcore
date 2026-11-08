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
    if (strategy > Strategy::Deadline || !floor || floor > fallback || fallback > ceiling ||
        ceiling > maximum_supported_rate || !target_us || target_us > 30'000'000)
        throw std::invalid_argument("invalid pacing controller bounds");
}

Proposal Controller::evaluate(const Observation& o, Rate current, Clock::time_point now) noexcept {
    if (proposed_ != Decision::Hold) return {Decision::Hold, current};
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
        pressure_ = quiet_ = 0;
        evaluating_ = false;
    }
    if (evaluating_) {
        evaluating_ = false;
        if (o.throughput < last_throughput_/2 && (!prior_p99_ || o.requests < 12 ||
                                                 o.request_p99_us >= prior_p99_))
            return propose(Decision::Rollback, previous_);
    }

    const bool backlog = o.backlog_bytes >= o.capacity/125; // 8 ms at the configured local rate
    const bool demand = o.background_demand >= o.capacity/4;
    const bool rtt_pressure = o.rtt_us >= 12'000;
    const bool slo_risk = o.requests >= 12 && o.request_p99_us > target_us_;
    const bool early = !regime_changed && ((strategy_ == Strategy::Ecn && o.ecn_pre_loss) ||
                                          (strategy_ == Strategy::Deadline && backlog && o.deadline_pressure));
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
    desired = std::clamp(desired, floor_, ceiling_);
    const Rate difference = std::max(desired,current)-std::min(desired,current);
    if (difference == 0 || difference < current/8) return {Decision::Hold, current};
    previous_ = current;
    prior_p99_ = o.requests >= 12 ? o.request_p99_us : 0;
    last_throughput_ = o.throughput;
    pressure_ = quiet_ = 0;
    return propose(Decision::Adjust, desired);
}

void Controller::confirm(bool success) noexcept {
    if (proposed_ == Decision::Hold) return;
    evaluating_ = success && proposed_ == Decision::Adjust;
    failed_ = failed_ || !success;
    proposed_ = Decision::Hold;
}

} // namespace flowcore::v2::pacing
