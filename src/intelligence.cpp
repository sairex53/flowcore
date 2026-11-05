#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>

// Stable, bounded C ABI for the daemon. Class order is critical, interactive,
// standard, background. No kernel access or allocation occurs in this solver.
extern "C" int flowcore_allocate(std::int64_t capacity, const std::int64_t* rates,
                                const std::int64_t* ceilings, const std::int64_t* maxima,
                                std::int64_t background_floor, std::int64_t step,
                                const double* scores, std::int64_t* output) noexcept {
    if (!rates || !ceilings || !maxima || !scores || !output ||
        capacity < 100 || capacity > 1000000 || step <= 0 ||
        background_floor < 1 || background_floor > capacity) return -2;
    std::int64_t total = 0;
    for (int i = 0; i < 4; ++i) {
        if (rates[i] < 1 || rates[i] > ceilings[i] || ceilings[i] > capacity ||
            maxima[i] < rates[i] || maxima[i] > ceilings[i] || !std::isfinite(scores[i]))
            return -2;
        output[i] = rates[i];
        total += rates[i];
    }
    if (total > capacity || rates[3] < background_floor) return -2;
    int selected = -1;
    double best = 0;
    for (int i = 0; i < 3; ++i) {
        if (scores[i] > best && rates[i] < maxima[i]) {
            best = scores[i];
            selected = i;
        }
    }
    if (selected < 0) return -1;
    auto change = std::min({step, rates[3] - background_floor,
                            maxima[selected] - rates[selected]});
    if (change <= 0) return -1;
    output[selected] += change;
    output[3] -= change;
    return selected;
}
