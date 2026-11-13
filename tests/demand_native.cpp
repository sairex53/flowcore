#include "flowcore/demand.hpp"

#include <cmath>
#include <cstdint>
#include <limits>

namespace {
fc_demand_input_v1 base() {
    fc_demand_input_v1 in{};
    in.version = 1;
    in.size = sizeof(in);
    in.interval_seconds = 1.0;
    in.evidence = FC_DEMAND_APP_TRAFFIC | FC_DEMAND_APP_COUNTS | FC_DEMAND_DELIVERED |
                  FC_DEMAND_ROOT_QUEUE | FC_DEMAND_CAPACITY | FC_DEMAND_CONFIGURED_CAP |
                  FC_DEMAND_CLASS | FC_DEMAND_CLASS_DROPS | FC_DEMAND_ROOT_DROPS;
    in.app_offered_bytes = in.app_admitted_bytes = in.app_transmitted_bytes = 250'000;
    in.app_received_bytes = 500'000;
    in.requests_attempted = in.requests_admitted = in.requests_completed = 100;
    in.delivered_bytes = in.class_served_bytes = 250'000;
    in.capacity_bps = in.configured_shaping_bps = 10'000'000;
    in.observed_bps = 2'000'000;
    in.class_rate_bps = in.class_ceil_bps = 1'000'000;
    return in;
}
bool is(fc_demand_input_v1 input, std::uint32_t expected) {
    fc_demand_output_v1 out{};
    return flowcore_demand_assess_v1(&input, &out) == 0 && out.classification == expected;
}
}

int main() {
    auto in = base();
    fc_demand_output_v1 out{};
    if (flowcore_demand_assess_v1(&in, &out) || out.offered_bps != 2'000'000 ||
        out.delivered_bps != 2'000'000 || out.received_bps != 4'000'000 ||
        out.unused_capacity_bps != 8'000'000 || out.classification != FC_DEMAND_DEMAND_LIMITED) return 1;

    in = base();
    in.app_offered_bytes = in.app_admitted_bytes = in.app_transmitted_bytes = 1'187'500;
    in.delivered_bytes = 1'187'500;
    in.observed_bps = 9'500'000;
    in.root_backlog_after_bytes = 4096;
    if (!is(in, FC_DEMAND_LOCAL_CAPACITY_LIMITED)) return 2;

    in = base();
    in.evidence |= FC_DEMAND_TCP | FC_DEMAND_TCP_LIMITS | FC_DEMAND_SOCKET_PACING;
    in.socket_pacing_bps = in.socket_delivery_bps = 1'000'000;
    in.socket_send_queue_after_bytes = in.socket_notsent_bytes = 4096;
    in.class_rate_bps = 2'000'000;
    in.class_ceil_bps = 4'000'000;
    in.class_backlog_after_bytes = 4096;
    if (flowcore_demand_assess_v1(&in, &out) || !(out.flags & FC_DEMAND_F_DOUBLE_THROTTLING)) return 3;

    in = base();
    in.evidence |= FC_DEMAND_TCP | FC_DEMAND_TCP_LIMITS;
    in.tcp_rwnd_limited_fraction = .8;
    in.socket_notsent_bytes = 4096;
    if (!is(in, FC_DEMAND_RECEIVER_LIMITED_SUSPECTED)) return 4;
    in = base();
    in.evidence |= FC_DEMAND_REMOTE_PRESSURE;
    if (!is(in, FC_DEMAND_REMOTE_BOTTLENECK_SUSPECTED)) return 5;

    in = base();
    in.evidence |= FC_DEMAND_APP_COUNTS;
    in.requests_attempted = 20;
    in.requests_admitted = 18;
    in.requests_completed = 15;
    in.requests_failed = 3;
    in.requests_rejected = 2;
    in.deadline_misses = 4;
    if (flowcore_demand_assess_v1(&in, &out) || out.failure_ratio != .25) return 6;
    in.requests_completed = 16;
    if (flowcore_demand_assess_v1(&in, &out) != -1) return 7;

    in = base();
    in.class_backlog_before_bytes = 100;
    in.class_backlog_after_bytes = 500;
    in.class_served_bytes = 1000;
    if (flowcore_demand_assess_v1(&in, &out) || out.queue_accounted_demand_bps != 11'200) return 8;
    in.class_drops = 1;
    // Class drop evidence invalidates that class's demand lower bound; the
    // independent zero-drop root queue remains observable and usable.
    if (flowcore_demand_assess_v1(&in, &out) || out.queue_accounted_demand_bps != 2'000'000) return 9;

    fc_demand_forecast_sample_v1 samples[8]{};
    for (unsigned i = 0; i < 8; ++i)
        samples[i] = {static_cast<std::uint64_t>(i + 1) * 1'000'000'000ULL,
                      static_cast<double>(i + 1) * 100'000.0, 10'000'000.0};
    fc_demand_forecast_v1 forecast{};
    if (flowcore_demand_forecast_v1(samples, 8, 2.0, 2.0, &forecast) ||
        !(forecast.forecast_bps > forecast.last_value_baseline_bps) ||
        forecast.forecast_bps > 40'000'000.0) return 10;
    samples[7].time_ns += 31'000'000'000ULL;
    if (flowcore_demand_forecast_v1(samples, 8, 2.0, 2.0, &forecast) != 1) return 11;
    samples[7].time_ns = samples[6].time_ns + 1'000'000'000ULL;
    samples[7].demand_bps = std::numeric_limits<double>::quiet_NaN();
    if (flowcore_demand_forecast_v1(samples, 8, 2.0, 2.0, &forecast) != -1) return 12;
    return 0;
}
