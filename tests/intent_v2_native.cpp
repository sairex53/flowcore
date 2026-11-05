#include "flowcore/intent_v2.hpp"
#include <chrono>
#include <iostream>
#include <thread>
#include <vector>
using namespace flowcore::v2;
int main(int argc,char** argv) {
    if (argc!=2) return 2;
    Intent intent;intent.service="concurrent";
    Service service(argv[1],intent);
    if (!service.wait_ready(std::chrono::seconds(3))) return 3;
    std::vector<std::thread> clients;
    for (int i=0;i<4;++i) clients.emplace_back([&service]{
        for (int n=0;n<32;++n) {
            auto request=service.begin_request(10000);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            if (n%4) static_cast<void>(request.complete(n%3?Outcome::Ok:Outcome::Timeout));
        }
    });
    for (auto& client:clients) client.join();
    if (!service.flush(std::chrono::seconds(3))) return 4;
    auto stats=service.statistics();
    std::cout<<stats.accepted<<' '<<stats.rejected<<' '<<stats.dropped<<' '<<stats.transport_errors<<'\n';
    if (!stats.accepted || stats.transport_errors) return 5;
    // Burst deliberately exceeds queue/event quotas: bounded degradation, not blocking I/O.
    for (int i=0;i<20000;++i) {
        auto request=service.begin_request();
        static_cast<void>(request.complete());
    }
    if (!service.flush(std::chrono::seconds(3))) return 7;
    auto burst=service.statistics();
    if (!burst.dropped || burst.transport_errors) return 8;
    Intent offline;offline.service="offline";
    Service unavailable("/nonexistent-flowcore-socket",offline);
    auto request=unavailable.begin_request();
    if (request.complete()) return 6;
}
