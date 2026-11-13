#include "flowcore/intent_v2.hpp"
#include "flowcore/overload_prevention.hpp"

#include <array>
#include <chrono>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <vector>

using namespace std::chrono_literals;
namespace o = flowcore::v2::overload;

#define CHECK(expression) do { if (!(expression)) throw std::runtime_error(#expression); } while (false)

namespace {
constexpr std::uint64_t now = 5'000'000'000ULL;

o::RequestCost cost(std::uint64_t deadline = now+100'000'000) {
    return {deadline, 4096, 64, 2'000, 5'000, 1'000, false, {}, {}};
}

o::Evidence evidence() {
    return {now, true, 1'000'000, 0, 5'000, 10'000, 10'000'000, 0,
            1'000'000, 10'000'000, 3'000, 2, 8, 0,
            1'000'000, 200'000, 200'000, true, true};
}

o::Observation sample(std::uint64_t stamp, std::uint64_t capacity = 12'000'000,
                      std::uint64_t delivered = 4'000'000, std::uint64_t backlog = 0,
                      std::uint32_t active = 8, std::uint32_t queued = 0) {
    return {stamp, capacity, delivered, backlog, active, queued,
            100, 96, 4, 96, 0, 0, 200'000, 150'000, true, true, true};
}

void feasibility() {
    auto request = cost();
    auto path = evidence();
    auto result = o::assess(request, path, now);
    CHECK(result.feasibility == o::Feasibility::Feasible);
    CHECK(result.recommendation == o::Recommendation::Accept);
    CHECK(result.predicted_p95_us && *result.predicted_p95_us > 0);
    CHECK(result.uncertainty_us && *result.uncertainty_us > 0);
    CHECK(result.fairness == o::Fairness::FloorObserved);

    auto queued_path = path;
    queued_path.active_concurrency = 8;
    queued_path.queue_depth = 4;
    result = o::assess(cost(now+14'000'000), queued_path, now);
    CHECK(result.feasibility == o::Feasibility::AtRisk);
    CHECK(result.recommendation == o::Recommendation::Defer);
    CHECK(result.queue_pressure);

    request = cost(now+10'000'000);
    request.request_bytes = 1024*1024;
    path.verified_max_bytes_s = 100'000;
    path.verified_response_max_bytes_s = 10'000'000;
    result = o::assess(request, path, now);
    CHECK(result.feasibility == o::Feasibility::Infeasible);
    CHECK(result.recommendation == o::Recommendation::RejectRecommended);
    CHECK(result.reason == o::Reason::HardDeadlineBound);

    CHECK(o::assess(cost(now), path, now).reason == o::Reason::Expired);
    request = cost();
    request.response_bytes.reset();
    CHECK(o::assess(request, path, now).recommendation == o::Recommendation::Unknown);
    request = cost();
    path.observed_ns = now-o::maximum_observation_age_ns-1;
    result = o::assess(request, path, now);
    CHECK(result.stale_evidence && result.reason == o::Reason::StaleEvidence);
    request.processing_p95_us = 1'000;
    CHECK(!o::valid(request));
    request = cost();
    path = evidence();
    path.active_concurrency = 1'000'001;
    CHECK(o::assess(request, path, now).reason == o::Reason::Invalid);
}

void controller_hysteresis_and_recovery() {
    o::BackpressureController controller({4, 32, 100});
    unsigned notifications = 0;
    controller.set_callback([&](const o::Update&) { ++notifications; });
    auto update = controller.observe(sample(now), now);
    CHECK(update.state == o::State::Normal && update.recommended_concurrency == 32);
    CHECK(update.fairness == o::Fairness::FloorObserved);

    auto busy = sample(now+250'000'000, 12'000'000, 11'500'000, 32'000, 32, 8);
    update = controller.observe(busy, now+250'000'000);
    CHECK(update.state == o::State::ApproachingCapacity);
    CHECK(update.action == o::Action::Hold);
    update = controller.observe(busy, now+500'000'000);
    CHECK(update.recommended_concurrency == 32); // Duplicate sample cannot advance the controller.
    busy.sampled_ns += 250'000'000;
    update = controller.observe(busy, busy.sampled_ns);
    CHECK(update.state == o::State::Overloaded);
    CHECK(update.action == o::Action::ReduceConcurrency);
    CHECK(update.recommended_concurrency == 24 && update.admission_budget_per_second == 75);
    CHECK(update.available_admission_slots == 0);

    auto collapse = sample(now+1'500'000'000, 8'000'000, 7'700'000, 20'000, 24, 4);
    update = controller.observe(collapse, collapse.sampled_ns);
    CHECK(update.state == o::State::CapacityCollapse);
    CHECK(update.action == o::Action::ReduceConcurrency);
    CHECK(update.recommended_concurrency == 18);

    auto recovery = sample(now+2'750'000'000, 8'000'000, 3'000'000, 0, 8, 0);
    update = controller.observe(recovery, recovery.sampled_ns);
    CHECK(update.state == o::State::Recovering && update.action == o::Action::Hold);
    recovery.sampled_ns += 1'100'000'000;
    update = controller.observe(recovery, recovery.sampled_ns);
    CHECK(update.action == o::Action::Hold);
    recovery.sampled_ns += 1'100'000'000;
    update = controller.observe(recovery, recovery.sampled_ns);
    CHECK(update.action == o::Action::IncreaseConcurrency);
    CHECK(update.recommended_concurrency == 19);
    CHECK(notifications >= 4);

    recovery.sampled_ns += 1;
    update = controller.observe(recovery, recovery.sampled_ns+o::maximum_observation_age_ns+1);
    CHECK(update.state == o::State::InsufficientData);
    CHECK(update.action == o::Action::Hold);
}

void accounting_fairness_and_callbacks() {
    o::BackpressureController controller({2, 8, 16});
    auto invalid = sample(now);
    invalid.admitted_requests = 90;
    invalid.rejected_requests = 4;
    CHECK(controller.observe(invalid, now).state == o::State::InsufficientData);

    invalid = sample(now+1);
    invalid.failed_requests = 1;
    CHECK(controller.observe(invalid, now+1).state == o::State::InsufficientData);

    auto floor_shortfall = sample(now+1);
    floor_shortfall.capacity_bytes_s = 100;
    floor_shortfall.background_floor_bytes_s = 200;
    auto result = o::assess(cost(now+100'000'000), evidence(), now+1);
    CHECK(result.fairness == o::Fairness::FloorObserved);
    result = o::assess(cost(now+100'000'000), floor_shortfall.scope_verified
        ? o::Evidence{floor_shortfall.sampled_ns, true, 1'000'000, 0, 5'000, 10'000,
                      10'000'000, 0, {}, {}, {}, 1, 8, 0,
                      floor_shortfall.capacity_bytes_s, floor_shortfall.background_floor_bytes_s,
                      floor_shortfall.background_goodput_bytes_s, true, true}
        : o::Evidence{}, now+1);
    CHECK(result.fairness == o::Fairness::FloorImpossible);

    controller.set_callback([](const o::Update&) { throw std::runtime_error("callback failure"); });
    const auto callback_result = controller.observe(sample(now+2), now+2);
    CHECK(callback_result.callback_failed);
}

void fair_cost_aware_queue_order() {
    const std::array candidates{
        o::Candidate{1, 50'000, 10'000, 256, 0, o::ServicePriority::Critical},
        o::Candidate{2, 60'000, 10'000, 4096, 300, o::ServicePriority::Background},
        o::Candidate{3, 1'000, 2'000, 1, 1000, o::ServicePriority::Standard}};
    CHECK(o::choose(candidates) == 2);
    CHECK(!o::choose(std::span<const o::Candidate>{candidates.data()+2, 1}));
    const auto now = flowcore::v2::deadline::monotonic_ns();
    flowcore::v2::Service offline("/dev/null/flowcore.sock", {"overload_demo"});
    auto path = evidence();
    path.observed_ns = now;
    const auto local = offline.assess(cost(now+100'000'000), path, now);
    CHECK(local.feasibility == o::Feasibility::Feasible);
    bool decision_notified = false;
    const auto notified = offline.assess(cost(now+100'000'000), path,
        [&](const o::Assessment& assessment) {
            decision_notified = assessment.recommendation == o::Recommendation::Accept;
        }, now);
    CHECK(decision_notified && !notified.callback_failed);
    const auto callback_failed = offline.assess(cost(now+100'000'000), path,
        [](const o::Assessment&) { throw std::runtime_error("admission callback failure"); }, now);
    CHECK(callback_failed.callback_failed);
}
} // namespace

int main() {
    try {
        feasibility();
        controller_hysteresis_and_recovery();
        accounting_fairness_and_callbacks();
        fair_cost_aware_queue_order();
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
