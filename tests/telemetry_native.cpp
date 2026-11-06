#include "flowcore/telemetry.hpp"
#include <atomic>
#include <limits>
#include <thread>
#include <vector>
using namespace flowcore::telemetry;
int main() {
    const std::array<Bin, 2> bins{{{0, 5}, {9, 5}}};
    const auto result = summarize(bins, 10);
    if (!result || (*result)[0] != 0 || (*result)[1] != 3) return 1;
    if (summarize(bins, 9)) return 2;
    const std::array<Bin, 2> duplicate{{{1, 1}, {1, 1}}};
    if (summarize(duplicate, 2)) return 3;
    const std::array<Bin, 1> invalid{{{257, 1}}};
    if (summarize(invalid, 1)) return 4;
    const std::array<Bin, 2> overflow{{{1, std::numeric_limits<std::uint64_t>::max()}, {2, 1}}};
    if (summarize(overflow, 1)) return 5;
    const auto empty = summarize({}, 0);
    if (!empty || (*empty)[0]) return 6;
    const auto overflow_rank = summarize(bins, 1000);
    if (!overflow_rank || (*overflow_rank)[0]) return 7;
    std::atomic<bool> failed{false};
    {
        std::vector<std::jthread> threads;
        for (int t=0;t<8;++t) threads.emplace_back([&] {
            for (int i=0;i<10000;++i)
                if (summarize(bins,10)!=result) failed=true;
        });
    }
    return failed ? 8 : 0;
}
