#pragma once

#include <cstdint>

// Arrays contain four classes in order: critical, interactive, standard,
// background. All rates are whole decimal kbit/s. See POLICY_OPTIMIZER.md.
struct flowcore_optimizer_class {
    std::int64_t floor;
    std::int64_t limit;
    std::int64_t demand;
    std::int64_t current;
    std::int64_t baseline;
    std::int64_t priority;
    std::int64_t utility;
    std::int64_t eligible;
};

enum flowcore_optimizer_algorithm {
    flowcore_optimizer_static,
    flowcore_optimizer_max_min,
    flowcore_optimizer_proportional,
    flowcore_optimizer_adaptive,
};

extern "C" int flowcore_optimize_v1(int algorithm, std::int64_t capacity,
                                    std::int64_t max_step, std::int64_t min_step,
                                    std::int64_t churn,
                                    const flowcore_optimizer_class* classes,
                                    std::int64_t* output, std::int64_t* score) noexcept;
