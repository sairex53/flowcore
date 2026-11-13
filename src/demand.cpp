#include "flowcore/demand.hpp"

#include <algorithm>
#include <array>
#include <cmath>
#include <limits>

namespace {
constexpr double max_value = 1.0e15;
constexpr double queue_threshold_bytes = 1500.0;
constexpr std::uint64_t all_evidence = (1ULL << 16) - 1;

bool finite_nonnegative(double value) noexcept {
    return std::isfinite(value) && value >= 0.0 && value <= max_value;
}
bool fraction(double value) noexcept {
    return std::isfinite(value) && value >= 0.0 && value <= 1.0;
}
double bit_rate(double bytes, double seconds) noexcept { return bytes * 8.0 / seconds; }

bool valid(const fc_demand_input_v1& in) noexcept {
    if (in.version != 1 || in.size != sizeof(in) || (in.evidence & ~all_evidence) ||
        !std::isfinite(in.interval_seconds) || in.interval_seconds < 0.05 || in.interval_seconds > 60.0)
        return false;
    const std::array<double, 29> values{
        in.app_offered_bytes, in.app_admitted_bytes, in.app_transmitted_bytes, in.app_received_bytes,
        in.delivered_bytes, in.capacity_bps, in.configured_shaping_bps, in.observed_bps,
        in.root_backlog_before_bytes, in.root_backlog_after_bytes, in.root_drops,
        in.class_rate_bps, in.class_ceil_bps, in.class_served_bytes,
        in.class_backlog_before_bytes, in.class_backlog_after_bytes, in.class_drops,
        in.socket_delivery_bps, in.socket_pacing_bps, in.socket_send_queue_before_bytes,
        in.socket_send_queue_after_bytes, in.socket_notsent_bytes, in.socket_sndbuf_bytes,
        in.tcp_sendbuf_limited_fraction, in.tcp_rwnd_limited_fraction,
        in.tcp_app_limited_fraction, in.retransmit_ratio, in.rtt_us, in.min_rtt_us
    };
    if (!std::all_of(values.begin(), values.end(), finite_nonnegative) ||
        !fraction(in.tcp_sendbuf_limited_fraction) || !fraction(in.tcp_rwnd_limited_fraction) ||
        !fraction(in.tcp_app_limited_fraction) || !fraction(in.retransmit_ratio)) return false;
    if ((in.evidence & FC_DEMAND_APP_TRAFFIC) &&
        (in.app_admitted_bytes > in.app_offered_bytes || in.app_transmitted_bytes > in.app_admitted_bytes))
        return false;
    if (in.evidence & FC_DEMAND_APP_COUNTS) {
        if (in.requests_admitted > in.requests_attempted ||
            in.requests_completed > in.requests_admitted ||
            in.requests_failed > in.requests_admitted - in.requests_completed ||
            in.requests_rejected > in.requests_attempted - in.requests_admitted ||
            in.deadline_misses > in.requests_admitted) return false;
    }
    if (in.evidence & FC_DEMAND_UDP_ACCOUNTING) {
        if (in.udp_messages_transmitted > in.udp_messages_attempted ||
            in.udp_send_failures > in.udp_messages_attempted ||
            in.udp_urgent_messages > in.udp_messages_attempted ||
            in.udp_background_messages > in.udp_messages_attempted - in.udp_urgent_messages) return false;
    }
    return true;
}

void no_measurement(double& value) noexcept { value = -1.0; }
}

extern "C" int flowcore_demand_assess_v1(const fc_demand_input_v1* input,
                                           fc_demand_output_v1* output) noexcept {
    if (!input || !output || !valid(*input)) return -1;
    *output = {};
    auto& out = *output;
    const auto& in = *input;
    out.version = 1;
    out.evidence_used = in.evidence;
    out.requests_attempted = in.requests_attempted;
    out.requests_admitted = in.requests_admitted;
    out.requests_completed = in.requests_completed;
    out.requests_failed = in.requests_failed;
    out.requests_rejected = in.requests_rejected;
    out.deadline_misses = in.deadline_misses;
    out.udp_messages_attempted = in.udp_messages_attempted;
    out.udp_messages_transmitted = in.udp_messages_transmitted;
    out.udp_send_failures = in.udp_send_failures;
    out.udp_urgent_messages = in.udp_urgent_messages;
    out.udp_background_messages = in.udp_background_messages;

    for (double* field : {&out.offered_bps, &out.admitted_bps, &out.transmitted_bps, &out.received_bps,
                          &out.delivered_bps, &out.queue_accounted_demand_bps, &out.socket_demand_bps,
                          &out.selected_demand_bps, &out.demand_uncertainty_bps,
                          &out.socket_demand_uncertainty_bps, &out.configured_shaping_bps,
                          &out.observed_utilization, &out.unused_capacity_bps,
                          &out.reclaimable_capacity_bps, &out.class_rate_bps, &out.class_ceil_bps,
                          &out.class_headroom_bps, &out.socket_queue_growth_bps,
                          &out.failure_ratio, &out.deadline_miss_ratio}) no_measurement(*field);

    const double seconds = in.interval_seconds;
    const bool app = (in.evidence & FC_DEMAND_APP_TRAFFIC) != 0;
    const bool app_complete = (in.evidence & FC_DEMAND_APP_INCOMPLETE) == 0;
    const bool counts = (in.evidence & FC_DEMAND_APP_COUNTS) != 0;
    const bool root_queue = (in.evidence & FC_DEMAND_ROOT_QUEUE) != 0;
    const bool capacity_known = (in.evidence & FC_DEMAND_CAPACITY) != 0;
    const bool class_known = (in.evidence & FC_DEMAND_CLASS) != 0;
    const bool tcp = (in.evidence & FC_DEMAND_TCP) != 0;
    const double capacity = (in.evidence & FC_DEMAND_CONFIGURED_CAP) && in.configured_shaping_bps > 0
                          ? in.configured_shaping_bps : in.capacity_bps;

    if (app) {
        out.offered_bps = bit_rate(in.app_offered_bytes, seconds);
        out.admitted_bps = bit_rate(in.app_admitted_bytes, seconds);
        out.transmitted_bps = bit_rate(in.app_transmitted_bytes, seconds);
        out.received_bps = bit_rate(in.app_received_bytes, seconds);
        out.selected_demand_bps = out.offered_bps;
        out.demand_uncertainty_bps = app_complete ? 0.0 : -1.0;
        if (!app_complete) out.flags |= FC_DEMAND_F_APP_REPORT_INCOMPLETE;
    }
    if (in.evidence & FC_DEMAND_DELIVERED) out.delivered_bps = bit_rate(in.delivered_bytes, seconds);
    else if (class_known) out.delivered_bps = bit_rate(in.class_served_bytes, seconds);
    else if (root_queue) out.delivered_bps = in.observed_bps;

    if (counts) {
        const double attempts = static_cast<double>(in.requests_attempted);
        out.failure_ratio = attempts > 0.0 ?
            (static_cast<double>(in.requests_failed) + static_cast<double>(in.requests_rejected)) / attempts : 0.0;
        out.deadline_miss_ratio = in.requests_admitted > 0 ?
            static_cast<double>(in.deadline_misses) / static_cast<double>(in.requests_admitted) : 0.0;
        if (in.requests_admitted + in.requests_rejected != in.requests_attempted ||
            in.requests_completed + in.requests_failed < in.requests_admitted)
            out.flags |= FC_DEMAND_F_APP_REPORT_INCOMPLETE;
    }

    if (in.evidence & FC_DEMAND_CONFIGURED_CAP) out.configured_shaping_bps = in.configured_shaping_bps;
    if (capacity_known && capacity > 0.0) {
        out.observed_utilization = in.observed_bps / capacity;
        out.unused_capacity_bps = std::max(0.0, capacity - in.observed_bps);
        const bool root_quiet = root_queue && in.root_backlog_after_bytes < queue_threshold_bytes &&
                                (!(in.evidence & FC_DEMAND_ROOT_DROPS) || in.root_drops == 0.0);
        out.reclaimable_capacity_bps = root_quiet ? out.unused_capacity_bps : 0.0;
        if (in.evidence & FC_DEMAND_NOMINAL_CAP) out.flags |= FC_DEMAND_F_CAPACITY_UNCERTAIN;
    }

    if (root_queue && (in.evidence & FC_DEMAND_ROOT_DROPS) && in.root_drops == 0.0 &&
        (in.evidence & FC_DEMAND_DELIVERED) && in.root_backlog_after_bytes >= in.root_backlog_before_bytes) {
        const double growth = in.root_backlog_after_bytes - in.root_backlog_before_bytes;
        out.queue_accounted_demand_bps = bit_rate(in.delivered_bytes + growth, seconds);
        if (growth > 0.0) out.flags |= FC_DEMAND_F_QUEUE_LOWER_BOUND;
        if (!app) out.selected_demand_bps = out.queue_accounted_demand_bps;
    }
    const bool class_drop_free = !(in.evidence & FC_DEMAND_CLASS_DROPS) || in.class_drops == 0.0;
    if (class_known && (in.evidence & FC_DEMAND_CLASS_DROPS) && class_drop_free &&
        in.class_backlog_after_bytes >= in.class_backlog_before_bytes) {
        const double queued_growth = in.class_backlog_after_bytes - in.class_backlog_before_bytes;
        out.queue_accounted_demand_bps = bit_rate(in.class_served_bytes + queued_growth, seconds);
        if (queued_growth > 0.0) out.flags |= FC_DEMAND_F_QUEUE_LOWER_BOUND;
        if (!app) out.selected_demand_bps = out.queue_accounted_demand_bps;
    }
    if (class_known) {
        if (in.class_rate_bps > 0.0) out.class_rate_bps = in.class_rate_bps;
        if (in.class_ceil_bps > 0.0) out.class_ceil_bps = in.class_ceil_bps;
        if (in.class_ceil_bps > 0.0 && out.delivered_bps >= 0.0)
            out.class_headroom_bps = std::max(0.0, in.class_ceil_bps - out.delivered_bps);
    }

    if (tcp && (in.evidence & FC_DEMAND_SOCKET_PACING) &&
        in.socket_send_queue_after_bytes >= in.socket_send_queue_before_bytes)
        out.socket_queue_growth_bps = bit_rate(in.socket_send_queue_after_bytes -
                                               in.socket_send_queue_before_bytes, seconds);
    const bool root_busy = root_queue && (in.root_backlog_after_bytes >= queue_threshold_bytes ||
        ((in.evidence & FC_DEMAND_ROOT_DROPS) && in.root_drops > 0.0));
    const bool class_busy = class_known && in.class_backlog_after_bytes >= queue_threshold_bytes;
    const bool socket_busy = tcp && (in.socket_notsent_bytes >= queue_threshold_bytes ||
        in.socket_send_queue_after_bytes >= queue_threshold_bytes || out.socket_queue_growth_bps >= 8.0);
    if (tcp && (in.evidence & FC_DEMAND_SOCKET_PACING) && in.socket_delivery_bps > 0.0 &&
        (in.tcp_app_limited_fraction < 0.8 || socket_busy)) {
        out.socket_demand_bps = in.socket_delivery_bps + std::max(0.0, out.socket_queue_growth_bps);
        out.socket_demand_uncertainty_bps = out.socket_queue_growth_bps > 0.0 ? -1.0 : in.socket_delivery_bps * .25;
    }

    const double class_cap = in.class_ceil_bps > 0.0 ? in.class_ceil_bps : in.class_rate_bps;
    const bool class_binding = class_busy && class_cap > 0.0 && capacity_known && capacity > 0.0 &&
        class_cap < capacity * .85 && out.delivered_bps >= class_cap * .75;
    const bool pacing_binding = tcp && (in.evidence & FC_DEMAND_SOCKET_PACING) && socket_busy &&
        in.socket_pacing_bps > 0.0 && capacity_known && capacity > 0.0 &&
        in.socket_pacing_bps < capacity * .85 && in.observed_bps >= in.socket_pacing_bps * .75;
    const bool receiver_limited = (in.evidence & FC_DEMAND_TCP_LIMITS) && socket_busy &&
        in.tcp_rwnd_limited_fraction >= .65;
    const bool sendbuf_limited = (in.evidence & FC_DEMAND_TCP_LIMITS) && socket_busy &&
        in.tcp_sendbuf_limited_fraction >= .65;
    if (sendbuf_limited) out.flags |= FC_DEMAND_F_SENDER_BUFFER_LIMITED;
    const bool local_busy = root_busy && capacity_known && capacity > 0.0 && in.observed_bps >= capacity * .85;
    const bool remote = (in.evidence & FC_DEMAND_REMOTE_PRESSURE) != 0;
    const bool app_below_capacity = app && app_complete && capacity_known && capacity > 0.0 &&
                                    out.offered_bps < capacity * .85;
    const bool tcp_app_limited = tcp && in.tcp_app_limited_fraction >= .8;
    const bool queue_quiet = !root_busy && !class_busy;

    if (class_busy && capacity_known && capacity > 0.0 && class_cap > 0.0 &&
        class_cap < capacity * .85 && in.class_ceil_bps > in.class_rate_bps * 1.05 &&
        in.socket_pacing_bps > 0.0 && in.socket_pacing_bps < in.class_ceil_bps * .85 &&
        in.observed_bps >= in.socket_pacing_bps * .75)
        out.flags |= FC_DEMAND_F_DOUBLE_THROTTLING;

    if (local_busy) {
        out.classification = FC_DEMAND_LOCAL_CAPACITY_LIMITED;
        out.confidence_percent = static_cast<std::uint32_t>(std::clamp(
            75.0 + ((in.evidence & FC_DEMAND_ROOT_DROPS) && in.root_drops > 0.0 ? 15.0 : 0.0), 0.0, 95.0));
    } else if (class_binding) {
        out.classification = FC_DEMAND_CLASS_RESTRICTED;
        out.confidence_percent = in.class_ceil_bps == in.class_rate_bps ? 90 : 75;
    } else if (pacing_binding && !receiver_limited && !sendbuf_limited) {
        out.classification = FC_DEMAND_SOCKET_PACING_RESTRICTED;
        out.confidence_percent = 80;
    } else if (receiver_limited) {
        out.classification = FC_DEMAND_RECEIVER_LIMITED_SUSPECTED;
        out.confidence_percent = 60;
        out.flags |= FC_DEMAND_F_RECEIVER_SUSPECTED;
    } else if (remote && queue_quiet) {
        out.classification = FC_DEMAND_REMOTE_BOTTLENECK_SUSPECTED;
        out.confidence_percent = 55;
        out.flags |= FC_DEMAND_F_REMOTE_SUSPECTED;
    } else if ((app_below_capacity || tcp_app_limited) && queue_quiet && !socket_busy) {
        out.classification = FC_DEMAND_DEMAND_LIMITED;
        out.confidence_percent = app_below_capacity ? (counts ? 90 : 80) : 60;
    } else if (app && app_complete && queue_quiet && out.offered_bps <= out.transmitted_bps * 1.05 &&
               (!capacity_known || out.offered_bps < capacity * .95)) {
        out.classification = FC_DEMAND_DEMAND_LIMITED;
        out.confidence_percent = 65;
    } else if (!sendbuf_limited) {
        out.classification = FC_DEMAND_UNKNOWN;
        out.confidence_percent = 0;
    } else {
        out.classification = FC_DEMAND_UNKNOWN;
        out.confidence_percent = 0;
    }
    return 0;
}

extern "C" int flowcore_demand_forecast_v1(const fc_demand_forecast_sample_v1* samples,
                                             unsigned count, double horizon, double half_life,
                                             fc_demand_forecast_v1* output) noexcept {
    if (!output) return -1;
    *output = {};
    if (!samples || count > 64 || !std::isfinite(horizon) || horizon < .1 || horizon > 30.0 ||
        !std::isfinite(half_life) || half_life < .25 || half_life > 60.0) return -1;
    if (count < 4) return 1;
    const unsigned begin = count > 16 ? count - 16 : 0;
    const double capacity = samples[count - 1].capacity_bps;
    if (!finite_nonnegative(capacity) || capacity <= 0.0) return -1;
    std::array<double, 16> x{}, y{};
    const unsigned n = count - begin;
    double ema = 0.0;
    for (unsigned i = 0; i < count; ++i) {
        const auto& row = samples[i];
        if (!row.time_ns || !finite_nonnegative(row.demand_bps) || !finite_nonnegative(row.capacity_bps)) return -1;
        if (i && (row.time_ns <= samples[i - 1].time_ns ||
                  row.time_ns - samples[i - 1].time_ns > 30'000'000'000ULL)) return 1;
        if (std::abs(row.capacity_bps - capacity) > std::max(1.0, capacity * .01)) return 1;
        if (i < begin) continue;
        x[i - begin] = static_cast<double>(row.time_ns - samples[begin].time_ns) / 1e9;
        y[i - begin] = row.demand_bps;
        if (i == begin) ema = row.demand_bps;
        else {
            const double dt = static_cast<double>(row.time_ns - samples[i - 1].time_ns) / 1e9;
            if (dt < .1) return 1;
            const double alpha = 1.0 - std::exp2(-dt / half_life);
            ema += alpha * (row.demand_bps - ema);
        }
    }
    double mean_x = 0.0, mean_y = 0.0;
    for (unsigned i = 0; i < n; ++i) { mean_x += x[i]; mean_y += y[i]; }
    mean_x /= n;
    mean_y /= n;
    double xx = 0.0, xy = 0.0, variance = 0.0;
    for (unsigned i = 0; i < n; ++i) {
        const double dx = x[i] - mean_x;
        xx += dx * dx;
        xy += dx * (y[i] - mean_y);
        variance += (y[i] - mean_y) * (y[i] - mean_y);
    }
    if (xx <= 0.0) return 1;
    const double slope = std::clamp(xy / xx, -capacity, capacity);
    double residual2 = 0.0;
    for (unsigned i = 0; i < n; ++i) {
        const double error = y[i] - (mean_y + slope * (x[i] - mean_x));
        residual2 += error * error;
    }
    const double deviation = std::sqrt(variance / n);
    const double residual = std::sqrt(residual2 / n);
    std::array<double, 16> sorted = y;
    std::sort(sorted.begin(), sorted.begin() + n);
    const double median = n % 2 ? sorted[n / 2] : (sorted[n / 2 - 1] + sorted[n / 2]) / 2.0;
    const double burst = mean_y > 1.0 ? deviation / mean_y : (deviation > 0.0 ? 1.0 : 0.0);
    const double latest = samples[count - 1].demand_bps;
    output->latest_bps = latest;
    output->ema_bps = ema;
    output->trend_bps = slope;
    output->forecast_bps = std::clamp(latest + slope * horizon, 0.0, capacity * 4.0);
    output->last_value_baseline_bps = latest;
    output->uncertainty_bps = std::max(residual, deviation * .5);
    const double scale = std::max({output->uncertainty_bps, capacity * .05, 1.0});
    output->confidence = std::clamp((static_cast<double>(n) / 8.0) /
                                    (1.0 + output->uncertainty_bps / scale), 0.0, 1.0);
    if (latest > std::max(1.0, median) * 2.0 || burst >= .75) output->confidence *= .5;
    return 0;
}
