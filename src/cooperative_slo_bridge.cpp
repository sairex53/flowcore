#include "flowcore/cooperative_slo.hpp"

#include <array>
#include <limits>

extern "C" std::uint32_t flowcore_slo_receive_budget_v1(std::uint32_t advertised,
                                                        std::uint32_t elapsed) noexcept {
    return flowcore::slo::receive_budget(advertised, elapsed);
}

extern "C" unsigned flowcore_slo_forecast_v1(const std::uint32_t* dependencies,
    const std::uint32_t* high_us, unsigned count, std::uint32_t remaining_us, std::uint64_t* out) noexcept {
    namespace slo = flowcore::slo;
    if (!count || count > slo::maximum_nodes) return static_cast<unsigned>(slo::Verdict::Invalid);
    std::array<slo::Stage, slo::maximum_nodes> stages{};
    for (unsigned i = 0; i < count; ++i) {
        stages[i].predecessors = dependencies[i];
        if (high_us[i] != std::numeric_limits<std::uint32_t>::max()) stages[i].high_us = high_us[i];
    }
    const auto result = slo::forecast({stages.data(), count}, remaining_us);
    *out = result.critical_path_us;
    return static_cast<unsigned>(result.verdict);
}
