#pragma once
#include <string>
namespace flowcore {
struct Config {
    std::string interface = "fc0";
    std::string tc;
    int capacity_kbit = 20000;
    double slo_ms = 15;
    int minimum_samples = 20;
    int balanced_percent = 20;
    int protected_percent = 40;
};
Config read_config(const std::string& path);
class Policy {
public:
    explicit Policy(Config config) : config_(config) {}
    bool update(double p99_ms, int samples);
    void reset() { protected_ = false; high_ = low_ = 0; }
    bool protected_mode() const { return protected_; }
private:
    Config config_;
    bool protected_ = false;
    int high_ = 0;
    int low_ = 0;
};
struct LinkStats {
    unsigned long long tx_bytes, tx_dropped, rx_bytes, rx_dropped;
    unsigned long long tx_packets, rx_packets, tx_errors, rx_errors;
};
LinkStats link_stats(const std::string& interface);
}
