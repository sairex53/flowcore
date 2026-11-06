#include "flowcore/telemetry.hpp"
#include <cmath>
#include <limits>

namespace flowcore::telemetry {
std::expected<Summary, Error> summarize(std::span<const Bin> bins, std::uint64_t total) noexcept {
    if (bins.size() > 257) return std::unexpected(Error::invalid_bin);
    std::array<std::uint64_t, 257> dense{};
    std::array<bool, 257> seen{};
    std::uint64_t sum = 0;
    for (const auto& bin : bins) {
        if (bin.index >= dense.size()) return std::unexpected(Error::invalid_bin);
        if (seen[bin.index]) return std::unexpected(Error::duplicate_bin);
        seen[bin.index] = true;
        if (bin.count > std::numeric_limits<std::uint64_t>::max() - sum)
            return std::unexpected(Error::overflow);
        sum += bin.count;
        dense[bin.index] = bin.count;
    }
    if (sum > total) return std::unexpected(Error::invalid_total);
    Summary result{};
    constexpr std::array<unsigned, 3> percentiles{50, 95, 99};
    for (std::size_t p = 0; p < result.size(); ++p) {
        // Exact integer ceiling, including totals above the precision of double.
        auto rank = (total / 100) * percentiles[p] +
                    ((total % 100) * percentiles[p] + 99) / 100;
        if (!rank) continue;
        for (std::size_t i = 0; i < dense.size(); ++i) {
            if (rank <= dense[i]) {
                result[p] = i ? static_cast<std::uint64_t>(std::ceil(std::exp2(i / 8.0))) : 0;
                break;
            }
            rank -= dense[i];
        }
    }
    return result;
}
}

// Fixed ABI v1: no C++ objects or exceptions cross the language boundary.
extern "C" unsigned flowcore_telemetry_abi() noexcept { return 1; }
extern "C" int flowcore_histogram_summary_v1(
    const std::uint16_t* indices, const std::uint64_t* counts, std::size_t length,
    std::uint64_t total, std::uint64_t* output) noexcept {
    if (!output || length > 257 || (length && (!indices || !counts))) return -1;
    std::array<flowcore::telemetry::Bin, 257> bins{};
    for (std::size_t i = 0; i < length; ++i) bins[i] = {indices[i], counts[i]};
    const auto result = flowcore::telemetry::summarize(
        std::span<const flowcore::telemetry::Bin>(bins.data(), length), total);
    if (!result) return -2;
    for (std::size_t i = 0; i < 3; ++i)
        output[i] = (*result)[i].value_or(std::numeric_limits<std::uint64_t>::max());
    return 0;
}
