// Private stable-width bridge for the daemon and Research Lab.
#include "flowcore/deadline.hpp"

#include <limits>

namespace d = flowcore::v2::deadline;
namespace {
constexpr auto missing = std::numeric_limits<std::uint64_t>::max();
template<class T>
std::optional<T> optional(std::uint64_t value) {
    if (value == missing || value > std::numeric_limits<T>::max()) return {};
    return static_cast<T>(value);
}
}
extern "C" void flowcore_deadline_estimate_v1(const std::uint64_t* work, const std::uint64_t* inputs,
                                             std::uint64_t now, std::uint64_t* out) noexcept {
    const d::Work w{work[0], work[1], optional<std::uint32_t>(work[2]), optional<std::uint32_t>(work[3]),
                    optional<std::uint32_t>(work[4]), static_cast<d::Reliability>(work[5]),
                    static_cast<d::Expiration>(work[6])};
    const d::Evidence e{inputs[0], optional<std::uint64_t>(inputs[1]), optional<std::uint64_t>(inputs[2]),
                        optional<std::uint64_t>(inputs[3]), optional<std::uint32_t>(inputs[4]),
                        optional<std::uint32_t>(inputs[5]), optional<std::uint32_t>(inputs[6]),
                        inputs[7] != 0, inputs[8] != 0, optional<std::uint64_t>(inputs[9])};
    const auto a = d::estimate(w, e, now);
    out[0] = static_cast<std::uint64_t>(a.decision);
    out[1] = static_cast<std::uint64_t>(a.reason);
    out[2] = static_cast<std::uint64_t>(a.quality);
    out[3] = a.remaining_ns;
    out[4] = a.capacity_bound_us.value_or(missing);
    out[5] = a.queue_us.value_or(missing);
    out[6] = a.network_p50_us.value_or(missing);
    out[7] = a.network_p95_us.value_or(missing);
    out[8] = a.reserved_us;
}
