#include "flowcore/policy.hpp"
#include <cmath>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <limits>
void require(bool condition) { if (!condition) throw std::runtime_error("policy assertion failed"); }
int main() {
    flowcore::Config c;
    flowcore::Policy policy(c);
    require(!policy.update(30, 100));
    require(policy.update(30, 100));
    require(policy.protected_mode());
    for (int i = 0; i < 3; ++i) require(!policy.update(5, 100));
    require(policy.update(5, 100));
    require(!policy.protected_mode());
    require(!policy.update(30, 100));
    require(!policy.update(30, 1));
    require(!policy.update(30, 100));
    require(policy.update(30, 100));
    policy.reset();
    require(!policy.protected_mode());
    require(!policy.update(15, 100));
    for (double bad : {-1., std::numeric_limits<double>::infinity(), std::numeric_limits<double>::quiet_NaN()}) {
        bool rejected = false;
        try { policy.update(bad, 100); } catch (const std::exception&) { rejected = true; }
        require(rejected);
    }
    const std::string file = "policy-test.conf";
    { std::ofstream out(file); out << "tc=/usr/bin/tc\n"; }
    require(flowcore::read_config(file).capacity_kbit == 20000);
    for (const std::string bad : {"tc=/usr/bin/tc\nprotected_percent=90\n", "tc=tc\n", "tc=/a\ntc=/b\n", "tc=/a\nslo_ms=nan\n", "tc=/a\ninterface=bad;name\n", "tc=/a\ncapacity_kbit=20x\n", "tc=/a\nunknown=1\n"}) {
        { std::ofstream out(file); out << bad; }
        bool rejected = false;
        try { flowcore::read_config(file); } catch (const std::exception&) { rejected = true; }
        require(rejected);
    }
    std::remove(file.c_str());
    std::cout << "policy and config checks passed\n";
}
