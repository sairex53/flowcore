#include "flowcore/optimizer.hpp"

#include <algorithm>
#include <array>
#include <cstdlib>
#include <numeric>

namespace {
using Rate = std::int64_t;
using Rates = std::array<Rate, 4>;
using Classes = std::array<flowcore_optimizer_class, 4>;

Rate sum(const Rates& rates) {
    return std::accumulate(rates.begin(), rates.end(), Rate{0});
}

Rate benefit(const flowcore_optimizer_class& row, Rate rate, Rate capacity, Rate churn) {
    const auto useful = std::max<Rate>(0, std::min(rate, row.demand) - row.floor);
    return row.utility * useful * (2 * capacity - useful)
        - churn * capacity * std::abs(rate - row.current);
}

Rate score_all(const Classes& rows, const Rates& allocation, Rate capacity, Rate churn) {
    Rate score = 0;
    for (std::size_t i = 0; i < rows.size(); ++i)
        score += benefit(rows[i], allocation[i], capacity, churn);
    return score;
}

// Capped weighted water filling over surplus above hard floors. Saturate caps
// before apportioning the last budget with largest remainders in class order.
Rates distribute(const Classes& rows, Rate capacity, bool proportional) {
    Rates result{}, heads{}, weights{};
    for (std::size_t i = 0; i < rows.size(); ++i) {
        result[i] = rows[i].floor;
        heads[i] = std::max<Rate>(0, std::min(rows[i].limit, rows[i].demand) - result[i]);
        if (heads[i] > 0)
            weights[i] = rows[i].priority * (proportional ? heads[i] : 1);
    }
    auto remaining = std::min(capacity - sum(result), sum(heads));
    while (remaining > 0) {
        const auto total = sum(weights);
        bool capped = false;
        const auto budget = remaining;
        for (std::size_t i = 0; i < rows.size(); ++i) {
            if (weights[i] > 0 && budget * weights[i] >= heads[i] * total) {
                result[i] += heads[i];
                remaining -= heads[i];
                weights[i] = 0;
                capped = true;
            }
        }
        if (capped) continue;
        Rates remainders{};
        for (std::size_t i = 0; i < rows.size(); ++i) {
            const auto product = budget * weights[i];
            result[i] += product / total;
            remaining -= product / total;
            remainders[i] = product % total;
        }
        while (remaining-- > 0) {
            const auto next = std::max_element(remainders.begin(), remainders.end());
            ++result[static_cast<std::size_t>(next - remainders.begin())];
            *next = 0;
        }
        break;
    }
    return result;
}

// Maximize the discrete concave score over hold and at most one transfer.
// Donor index rows.size() denotes unreserved parent capacity.
Rates adapt(const Classes& rows, Rate capacity, Rate step, Rate minimum, Rate churn, Rates current) {
    auto best = current;
    Rate best_gain = 0;
    const auto unused = capacity - sum(current);
    for (std::size_t receiver = 0; receiver < rows.size(); ++receiver) {
        const auto& target = rows[receiver];
        if (!target.eligible) continue;
        const auto head = std::min(target.limit, target.demand) - target.current;
        for (std::size_t donor = 0; donor <= rows.size(); ++donor) {
            if (donor == receiver) continue;
            const bool spare = donor == rows.size();
            const auto supply = spare ? unused : rows[donor].current - rows[donor].floor;
            const auto max_move = std::min({head, step, supply});
            if (max_move < minimum) continue;
            const auto gain = [&](Rate amount) {
                auto delta = benefit(target, target.current + amount, capacity, churn)
                    - benefit(target, target.current, capacity, churn);
                if (!spare) {
                    const auto& source = rows[donor];
                    delta += benefit(source, source.current - amount, capacity, churn)
                        - benefit(source, source.current, capacity, churn);
                }
                return delta;
            };
            Rate left = minimum, right = max_move;
            while (left < right) {
                const auto middle = left + (right - left) / 2;
                if (gain(middle + 1) > gain(middle)) left = middle + 1;
                else right = middle;
            }
            if (const auto value = gain(left); value > best_gain) {
                best = current;
                best[receiver] += left;
                if (!spare) best[donor] -= left;
                best_gain = value;
            }
        }
    }
    return best;
}
}

extern "C" int flowcore_optimize_v1(int algorithm, Rate capacity, Rate max_step, Rate min_step,
                                    Rate churn, const flowcore_optimizer_class* classes,
                                    Rate* output, Rate* score) noexcept {
    if (!classes || !output || !score || algorithm < flowcore_optimizer_static ||
        algorithm > flowcore_optimizer_adaptive || capacity < 100 || capacity > 1000000 ||
        min_step < 1 || max_step < min_step || max_step > capacity || churn < 0 || churn > 1000)
        return -1;

    Classes rows{};
    std::copy_n(classes, rows.size(), rows.begin());
    Rates current{}, baseline{}, floors{};
    for (std::size_t i = 0; i < rows.size(); ++i) {
        const auto& row = rows[i];
        if (row.floor < 1 || row.floor > capacity || row.limit < 1 || row.limit > capacity ||
            row.demand < 0 || row.demand > capacity || row.priority < 1 || row.priority > 100 ||
            row.utility < 1 || row.utility > 1000 || (row.eligible != 0 && row.eligible != 1))
            return -1;
        if (row.limit < row.floor) return -2;
        if (row.baseline < row.floor || row.baseline > row.limit) return -3;
        if (row.current < row.floor || row.current > row.limit) return -4;
        floors[i] = row.floor;
        current[i] = row.current;
        baseline[i] = row.baseline;
    }
    if (sum(floors) > capacity) return -2;
    if (sum(baseline) > capacity) return -3;
    if (sum(current) > capacity) return -4;

    Rates solution = baseline;
    if (algorithm == flowcore_optimizer_max_min || algorithm == flowcore_optimizer_proportional)
        solution = distribute(rows, capacity, algorithm == flowcore_optimizer_proportional);
    if (algorithm == flowcore_optimizer_adaptive)
        solution = adapt(rows, capacity, max_step, min_step, churn, current);

    if (sum(solution) > capacity) return -5;
    for (std::size_t i = 0; i < rows.size(); ++i)
        if (solution[i] < rows[i].floor || solution[i] > rows[i].limit) return -5;
    std::copy(solution.begin(), solution.end(), output);
    *score = score_all(rows, solution, capacity, churn);
    return 0;
}
