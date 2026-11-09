#include "flowcore/hybrid.hpp"

#include <algorithm>

namespace flowcore::hybrid {

Plan assess(const Observation& o) noexcept {
    if (!o.fresh || !o.capacity || o.capacity > 1'500'000 || !o.sockets || o.sockets > 32 ||
        !o.cap_min || o.cap_min > o.cap_sum || !o.background_floor || o.background_floor > o.background_rate ||
        o.background_rate > o.background_ceiling || o.background_ceiling > o.capacity ||
        !o.critical_rate || o.critical_rate > o.critical_ceiling || o.critical_ceiling > o.capacity ||
        o.critical_rate + o.background_rate > o.capacity)
        return {Limit::Unknown, Action::Hold, 0, 0, 0};

    const auto upper = std::min({o.capacity, o.background_ceiling, o.cap_sum});
    const auto spare = o.capacity-std::min(o.capacity, o.wire_rate);
    const bool slack = spare >= o.capacity/20;
    const bool near = o.background_goodput >= upper*85/100;
    if (slack && o.demand && near && o.cap_sum < o.background_ceiling && o.cap_sum < o.capacity) {
        // A limit release while the SLO is already at risk needs a separate
        // experiment. Zero means that the application sample is unavailable.
        const auto action = o.p99_us && o.target_us && o.p99_us <= o.target_us
                          ? Action::ReleaseSocket : Action::Hold;
        return {Limit::Socket, action, spare, upper, 0};
    }
    if (slack && o.demand && near && o.background_ceiling < o.capacity && o.background_ceiling <= o.cap_sum)
        return {Limit::Class, Action::Hold, spare, upper, 0};

    const auto limit = o.wire_rate >= o.capacity*90/100 ? Limit::Link : Limit::Headroom;
    const auto step = (o.capacity/125*5/100)*125;
    const bool eligible = limit == Limit::Link && o.cap_min >= o.capacity &&
        o.critical_backlog >= 1500 && o.target_us && o.p99_us > o.target_us+o.target_us/10 &&
        o.critical_wire_rate >= o.critical_rate*90/100 && step &&
        o.background_rate-o.background_floor >= step && o.critical_ceiling-o.critical_rate >= step &&
        o.background_goodput >= o.background_floor;
    return {limit, eligible ? Action::ShiftClass : Action::Hold, spare, upper, eligible ? step : 0};
}

} // namespace flowcore::hybrid
