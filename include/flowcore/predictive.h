#ifndef FLOWCORE_PREDICTIVE_H
#define FLOWCORE_PREDICTIVE_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
/* Internal versioned ABI. Caller retains chronological samples; no allocation/kernel access. */
struct fc_predict_sample { uint64_t time_ns; double backlog_bytes, departure_bps, capacity_bps; };
struct fc_prediction {
    double ema_bytes, trend_bps, arrival_proxy_bps, forecast_bytes;
    double risk, quality, lead_seconds, residual_bytes;
};
/* Methods: 0 EMA persistence, 1 least-squares trend, 2 queue growth, 3 rate change, 4 hybrid.
   Returns 0 valid, 1 insufficient/discontinuous, -1 malformed. Score is NOT a probability. */
int flowcore_predict_v1(const struct fc_predict_sample*, unsigned count, unsigned method,
                       double horizon_seconds, double threshold_bytes, double root_utilization,
                       struct fc_prediction*)
#ifdef __cplusplus
noexcept
#endif
;
#ifdef __cplusplus
}
#endif
#endif
