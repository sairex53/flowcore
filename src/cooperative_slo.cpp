#include "flowcore/cooperative_slo.hpp"

#include <algorithm>
#include <array>

namespace flowcore::slo {

std::uint32_t receive_budget(std::uint32_t advertised_us, std::uint32_t challenge_elapsed_us) noexcept {
    if (advertised_us > maximum_budget_us || challenge_elapsed_us >= advertised_us) return 0;
    return advertised_us - challenge_elapsed_us;
}

Forecast forecast(std::span<const Stage> stages, std::uint32_t remaining_us) noexcept {
    if (stages.empty() || stages.size() > maximum_nodes || remaining_us > maximum_budget_us)
        return {Verdict::Invalid, 0};
    std::array<std::uint64_t, maximum_nodes> finish{};
    std::uint64_t total = 0;
    bool missing = false;
    for (unsigned i = 0; i < stages.size(); ++i) {
        const auto& stage = stages[i];
        if ((stage.predecessors >> i) || (stage.high_us && *stage.high_us > maximum_budget_us))
            return {Verdict::Invalid, 0};
        std::uint64_t prior = 0;
        for (unsigned j = 0; j < i; ++j) {
            if (stage.predecessors & (1U << j)) prior = std::max(prior, finish[j]);
        }
        missing |= !stage.high_us;
        finish[i] = prior + stage.high_us.value_or(0);
        total = std::max(total, finish[i]);
    }
    if (!remaining_us) return {Verdict::Expired, total};
    if (total >= remaining_us) return {Verdict::AtRisk, total};
    return {missing ? Verdict::Insufficient : Verdict::Fits, total};
}

} // namespace flowcore::slo
