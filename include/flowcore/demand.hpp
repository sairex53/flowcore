#pragma once

#include <cstdint>

// Versioned internal C ABI. Rates are bits/second and counters are bytes or
// events. An absent evidence bit means the value is not observable.
extern "C" {

enum fc_demand_evidence_v1 : std::uint64_t {
    FC_DEMAND_APP_TRAFFIC       = 1ULL << 0,
    FC_DEMAND_APP_COUNTS        = 1ULL << 1,
    FC_DEMAND_DELIVERED         = 1ULL << 2,
    FC_DEMAND_ROOT_QUEUE        = 1ULL << 3,
    FC_DEMAND_CAPACITY          = 1ULL << 4,
    FC_DEMAND_CLASS             = 1ULL << 5,
    FC_DEMAND_TCP               = 1ULL << 6,
    FC_DEMAND_TCP_LIMITS        = 1ULL << 7,
    FC_DEMAND_SOCKET_PACING     = 1ULL << 8,
    FC_DEMAND_REMOTE_PRESSURE   = 1ULL << 9,
    FC_DEMAND_UDP_ACCOUNTING    = 1ULL << 10,
    FC_DEMAND_CONFIGURED_CAP    = 1ULL << 11,
    FC_DEMAND_NOMINAL_CAP       = 1ULL << 12,
    FC_DEMAND_APP_INCOMPLETE    = 1ULL << 13,
    FC_DEMAND_CLASS_DROPS       = 1ULL << 14,
    FC_DEMAND_ROOT_DROPS        = 1ULL << 15
};

enum fc_demand_classification_v1 : std::uint32_t {
    FC_DEMAND_UNKNOWN = 0,
    FC_DEMAND_DEMAND_LIMITED = 1,
    FC_DEMAND_LOCAL_CAPACITY_LIMITED = 2,
    FC_DEMAND_CLASS_RESTRICTED = 3,
    FC_DEMAND_SOCKET_PACING_RESTRICTED = 4,
    FC_DEMAND_RECEIVER_LIMITED_SUSPECTED = 5,
    FC_DEMAND_REMOTE_BOTTLENECK_SUSPECTED = 6
};

enum fc_demand_flags_v1 : std::uint32_t {
    FC_DEMAND_F_QUEUE_LOWER_BOUND = 1U << 0,
    FC_DEMAND_F_DOUBLE_THROTTLING = 1U << 1,
    FC_DEMAND_F_CAPACITY_UNCERTAIN = 1U << 2,
    FC_DEMAND_F_APP_REPORT_INCOMPLETE = 1U << 3,
    FC_DEMAND_F_RECEIVER_SUSPECTED = 1U << 4,
    FC_DEMAND_F_REMOTE_SUSPECTED = 1U << 5,
    FC_DEMAND_F_SENDER_BUFFER_LIMITED = 1U << 6
};

struct fc_demand_input_v1 {
    std::uint32_t version, size;
    std::uint64_t evidence;
    double interval_seconds;
    double app_offered_bytes, app_admitted_bytes, app_transmitted_bytes, app_received_bytes;
    std::uint64_t requests_attempted, requests_admitted, requests_completed, requests_failed;
    std::uint64_t requests_rejected, deadline_misses;
    std::uint64_t udp_messages_attempted, udp_messages_transmitted, udp_send_failures;
    std::uint64_t udp_urgent_messages, udp_background_messages;
    double delivered_bytes, capacity_bps, configured_shaping_bps, observed_bps;
    double root_backlog_before_bytes, root_backlog_after_bytes, root_drops;
    double class_rate_bps, class_ceil_bps, class_served_bytes;
    double class_backlog_before_bytes, class_backlog_after_bytes, class_drops;
    double socket_delivery_bps, socket_pacing_bps;
    double socket_send_queue_before_bytes, socket_send_queue_after_bytes;
    double socket_notsent_bytes, socket_sndbuf_bytes;
    double tcp_sendbuf_limited_fraction, tcp_rwnd_limited_fraction, tcp_app_limited_fraction;
    double retransmit_ratio, rtt_us, min_rtt_us;
};

struct fc_demand_output_v1 {
    std::uint32_t version, classification, confidence_percent, flags;
    std::uint64_t evidence_used;
    double offered_bps, admitted_bps, transmitted_bps, received_bps, delivered_bps;
    double queue_accounted_demand_bps, socket_demand_bps, selected_demand_bps;
    double demand_uncertainty_bps, socket_demand_uncertainty_bps;
    double configured_shaping_bps, observed_utilization, unused_capacity_bps, reclaimable_capacity_bps;
    double class_rate_bps, class_ceil_bps, class_headroom_bps, socket_queue_growth_bps;
    double failure_ratio, deadline_miss_ratio;
    std::uint64_t requests_attempted, requests_admitted, requests_completed, requests_failed;
    std::uint64_t requests_rejected, deadline_misses;
    std::uint64_t udp_messages_attempted, udp_messages_transmitted, udp_send_failures;
    std::uint64_t udp_urgent_messages, udp_background_messages;
};

struct fc_demand_forecast_sample_v1 {
    std::uint64_t time_ns;
    double demand_bps, capacity_bps;
};

struct fc_demand_forecast_v1 {
    double latest_bps, ema_bps, trend_bps, forecast_bps, last_value_baseline_bps;
    double uncertainty_bps, burst_coefficient, confidence;
};

int flowcore_demand_assess_v1(const struct fc_demand_input_v1*, struct fc_demand_output_v1*) noexcept;
int flowcore_demand_forecast_v1(const struct fc_demand_forecast_sample_v1*, unsigned count,
                                double horizon_seconds, double half_life_seconds,
                                struct fc_demand_forecast_v1*) noexcept;

}
