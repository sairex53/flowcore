#include "flowcore/predictive.h"
#include <algorithm>
#include <cmath>

extern "C" int flowcore_predict_v1(const fc_predict_sample* samples, unsigned count,
                                    unsigned method, double horizon, double threshold,
                                    double utilization, fc_prediction* out) noexcept {
    if (!out) return -1;
    *out={};out->lead_seconds=-1;
    if (!samples || count>64 || method>4 || !std::isfinite(horizon) || horizon<.1 || horizon>4 ||
        !std::isfinite(threshold) || threshold<1 || !std::isfinite(utilization) ||
        utilization<0 || utilization>10) return -1;
    if (count<4) return 1;
    const unsigned begin=count>8?count-8:0;
    const double capacity=samples[count-1].capacity_bps;
    double mx=0,my=0,ema=0,departures=0,growth=0,rate_change=0;
    for (unsigned i=0;i<count;++i) {
        const auto& s=samples[i];
        if (!s.time_ns || !std::isfinite(s.backlog_bytes) || s.backlog_bytes<0 ||
            s.backlog_bytes>1e12 || !std::isfinite(s.departure_bps) || s.departure_bps<0 ||
            s.departure_bps>1e12 || !std::isfinite(s.capacity_bps) || s.capacity_bps<1 ||
            s.capacity_bps>1e12) return -1;
        if (i && s.time_ns<=samples[i-1].time_ns) return -1;
        if (s.capacity_bps!=capacity || (i && s.time_ns-samples[i-1].time_ns>2500000000ULL)) return 1;
        if (i<begin) continue;
        const double x=static_cast<double>(s.time_ns-samples[begin].time_ns)/1e9;
        mx+=x;my+=s.backlog_bytes;
        if (i==begin) { ema=s.backlog_bytes;departures=s.departure_bps; }
        else {
            const double dt=static_cast<double>(s.time_ns-samples[i-1].time_ns)/1e9;
            if (dt<.25) return 1; // Do not treat actuator refreshes as independent samples.
            const double alpha=1-std::exp(-dt/2.0);
            ema+=alpha*(s.backlog_bytes-ema);
            departures+=alpha*(s.departure_bps-departures);
            growth+=alpha*((s.backlog_bytes-samples[i-1].backlog_bytes)/dt-growth);
            rate_change+=alpha*((s.departure_bps-samples[i-1].departure_bps)/dt-rate_change);
        }
    }
    const double n=count-begin;mx/=n;my/=n;
    double xx=0,xy=0;
    for (unsigned i=begin;i<count;++i) {
        const double x=static_cast<double>(samples[i].time_ns-samples[begin].time_ns)/1e9-mx;
        xx+=x*x;xy+=x*(samples[i].backlog_bytes-my);
    }
    if (xx<=0) return 1;
    const double trend=std::clamp(xy/xx,-capacity,capacity);
    double residual=0;
    for (unsigned i=begin;i<count;++i) {
        const double x=static_cast<double>(samples[i].time_ns-samples[begin].time_ns)/1e9-mx;
        const double error=samples[i].backlog_bytes-(my+trend*x);residual+=error*error;
    }
    residual=std::sqrt(residual/n);
    double slope=0,anchor=ema;
    if (method==1) { slope=trend;anchor=samples[count-1].backlog_bytes; }
    if (method==2) { slope=growth;anchor=samples[count-1].backlog_bytes; }
    if (method==3) slope=std::max(0.0,rate_change*horizon);
    if (method==4) { slope=.5*(trend+growth);anchor=samples[count-1].backlog_bytes; }
    slope=std::clamp(slope,-capacity,capacity);
    const double forecast=std::clamp(anchor+slope*horizon,0.0,capacity*4);
    const double quality=std::min(1.0,n/8)/(1+residual/threshold);
    out->ema_bytes=ema;out->trend_bps=slope;
    out->arrival_proxy_bps=std::clamp(departures+growth,0.0,capacity*10);
    out->forecast_bytes=forecast;
    out->risk=std::clamp(forecast/threshold,0.0,1.0)*std::clamp(utilization/.85,0.0,1.0);
    out->quality=quality;out->residual_bytes=residual;
    if (slope>0 && anchor<threshold) out->lead_seconds=(threshold-anchor)/slope;
    return 0;
}
