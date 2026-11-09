#pragma once

#include <cstdint>

namespace flowcore::hybrid {

using Rate = std::uint64_t; // bytes per second
enum class Limit : unsigned { Unknown, Headroom, Socket, Class, Link };
enum class Action : unsigned { Hold, ReleaseSocket, ShiftClass };

struct Observation {
    Rate capacity, background_rate, background_ceiling, critical_rate, critical_ceiling;
    Rate cap_sum, cap_min, background_floor, background_goodput, wire_rate, critical_wire_rate;
    std::uint64_t critical_backlog, p99_us, target_us, sockets;
    bool fresh, demand;
};

struct Plan {
    Limit limit;
    Action action;
    Rate headroom, effective_upper, step;
};

// The upper bound is arithmetic. A limiting actuator additionally needs
// receiver progress and demand evidence. No global capacity is inferred.
Plan assess(const Observation&) noexcept;

} // namespace flowcore::hybrid
