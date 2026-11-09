#include <cstddef>
#include <cstdint>

namespace {
constexpr std::size_t max_candidates = 8;
constexpr std::size_t max_objectives = 10;
constexpr std::int64_t max_loss = 1'000'000'000'000;

// All dimensions are losses in millionths of their configured scales.
bool dominates(const std::int64_t* left, const std::int64_t* right, std::size_t dimensions) noexcept {
    bool strictly_better = false;
    for (std::size_t j = 0; j < dimensions; ++j) {
        if (left[j] > right[j]) return false;
        strictly_better |= left[j] < right[j];
    }
    return strictly_better;
}
}

// A bounded, allocation-free companion to the reservation solver.
// Eligibility, provenance and uncertainty are established by the caller.
extern "C" int flowcore_slo_front_v1(std::size_t candidates, std::size_t dimensions,
    const std::int64_t* losses, const std::int64_t* weights, std::uint32_t* front, std::int64_t* scores) noexcept {
    if (!candidates || candidates > max_candidates || !dimensions || dimensions > max_objectives ||
        !losses || !weights || !front || !scores)
        return -1;
    for (std::size_t j = 0; j < dimensions; ++j)
        if (weights[j] < 1 || weights[j] > 100) return -1;
    for (std::size_t i = 0; i < candidates; ++i) {
        for (std::size_t j = 0; j < dimensions; ++j)
            if (losses[i*dimensions+j] < -max_loss || losses[i*dimensions+j] > max_loss) return -1;
    }

    *front = 0;
    for (std::size_t i = 0; i < candidates; ++i) {
        scores[i] = 0;
        const auto* row = losses+i*dimensions;
        for (std::size_t j = 0; j < dimensions; ++j) scores[i] += weights[j]*row[j];
        bool dominated = false;
        for (std::size_t other = 0; other < candidates && !dominated; ++other)
            if (other != i)
                dominated = dominates(losses+other*dimensions, row, dimensions);
        if (!dominated) *front |= std::uint32_t{1} << i;
    }
    return 0;
}
