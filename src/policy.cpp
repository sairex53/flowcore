#include "flowcore/policy.hpp"
#include <cmath>
#include <cstdlib>
#include <algorithm>
#include <fstream>
#include <set>
#include <stdexcept>
#include <string>
namespace flowcore {
Config read_config(const std::string& path) {
    std::ifstream input(path);
    if (!input) throw std::runtime_error("cannot open configuration");
    Config c;
    std::set<std::string> seen;
    std::string line;
    auto integer = [](const std::string& value) {
        size_t end = 0;
        int number = std::stoi(value, &end);
        if (end != value.size()) throw std::runtime_error("invalid integer");
        return number;
    };
    while (std::getline(input, line)) {
        if (line.empty() || line[0] == '#') continue;
        auto equals = line.find('=');
        if (equals == std::string::npos) throw std::runtime_error("expected key=value");
        auto key = line.substr(0, equals);
        auto value = line.substr(equals + 1);
        if (!seen.insert(key).second) throw std::runtime_error("duplicate config key");
        if (key == "interface") c.interface = value;
        else if (key == "tc") c.tc = value;
        else if (key == "capacity_kbit") c.capacity_kbit = integer(value);
        else if (key == "minimum_samples") c.minimum_samples = integer(value);
        else if (key == "balanced_percent") c.balanced_percent = integer(value);
        else if (key == "protected_percent") c.protected_percent = integer(value);
        else if (key == "slo_ms") {
            size_t end = 0;
            c.slo_ms = std::stod(value, &end);
            if (end != value.size()) throw std::runtime_error("invalid SLO");
        } else throw std::runtime_error("unknown configuration key: " + key);
    }
    if (c.interface.empty() || c.interface.size() >= 16 ||
        c.interface.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-") != std::string::npos)
        throw std::runtime_error("invalid interface");
    if (c.tc.empty()) {
        const auto* tc = std::getenv("FLOWCORE_TC");
        c.tc = tc ? tc : "/usr/sbin/tc";
    }
    if (c.tc.empty() || c.tc.front() != '/') throw std::runtime_error("tc must be an absolute path");
    if (c.capacity_kbit < 1000 || c.capacity_kbit > 10000000 ||
        !std::isfinite(c.slo_ms) || c.slo_ms <= 0 || c.slo_ms > 60000 ||
        c.minimum_samples < 1 || c.minimum_samples > 1000000 ||
        c.balanced_percent < 1 || c.protected_percent < c.balanced_percent || c.protected_percent > 50)
        throw std::runtime_error("configuration outside supported bounds");
    return c;
}
bool Policy::update(double p99_ms, int samples) {
    if (!std::isfinite(p99_ms) || p99_ms < 0 || samples < 0)
        throw std::runtime_error("invalid feedback");
    const bool previous = protected_;
    if (samples < config_.minimum_samples) { high_ = low_ = 0; return false; }
    if (p99_ms > config_.slo_ms) { high_ = std::min(high_ + 1, 2); low_ = 0; }
    else if (p99_ms < config_.slo_ms * 0.7) { low_ = std::min(low_ + 1, 4); high_ = 0; }
    else { high_ = low_ = 0; }
    if (!protected_ && high_ >= 2) { protected_ = true; high_ = 0; }
    if (protected_ && low_ >= 4) { protected_ = false; low_ = 0; }
    return previous != protected_;
}
}
