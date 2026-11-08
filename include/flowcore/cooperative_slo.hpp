#pragma once

#include <cstdint>
#include <optional>
#include <span>

namespace flowcore::slo {

inline constexpr std::uint32_t maximum_budget_us = 10'000'000;
inline constexpr unsigned maximum_nodes = 32;

// The receiver sends a fresh challenge before the sender transmits its budget.
std::uint32_t receive_budget(std::uint32_t advertised_us, std::uint32_t challenge_elapsed_us) noexcept;

struct Stage {
    std::uint32_t predecessors = 0; // Bits refer only to earlier stages.
    std::optional<std::uint32_t> high_us; // Local non-overlapping duration.
};
enum class Verdict { Fits, AtRisk, Insufficient, Invalid, Expired };
struct Forecast {
    Verdict verdict;
    std::uint64_t critical_path_us;
};
Forecast forecast(std::span<const Stage>, std::uint32_t remaining_us) noexcept;

} // namespace flowcore::slo
