#include "flowcore/intent_v2.hpp"
#include <chrono>
#include <iostream>
#include <thread>
#include <utility>
#include <vector>
using namespace flowcore::v2;
int main(int argc,char** argv) {
    if (argc!=2) return 2;
    Intent intent;intent.service="concurrent";
    Service service(argv[1],intent);
    if (!service.wait_ready(std::chrono::seconds(3))) return 3;
    const auto capabilities=service.capabilities();
    if (capabilities.protocol_version!=2 ||
        !capabilities.supports(Capability::LeaseRenewal) ||
        !capabilities.supports(Capability::TransportSessions)) return 9;
    const auto policy=service.effective_policy();
    if (!policy) { std::cerr << "policy inspection failed: " << policy.error().message << '\n'; return 10; }
    if (policy->accepted_intent.service!="concurrent" ||
        (policy->kernel_verified != (policy->state==PolicyState::KernelVerified))) {
        std::cerr << "unexpected policy snapshot " << policy->accepted_intent.service << ' '
                  << static_cast<unsigned>(policy->state) << ' ' << policy->kernel_verified << '\n';
        return 10;
    }
    auto invalid_open=Service::try_open("/nonexistent-flowcore.sock",Intent{});
    if (invalid_open || invalid_open.error().code!=Error::InvalidRequest) return 11;
    Intent offline_intent;offline_intent.service="offline_open";
    auto offline_open=Service::try_open("/nonexistent-flowcore.sock",offline_intent);
    if (!offline_open || offline_open->ready()) return 12;
    {
        auto first=service.begin_request(std::chrono::microseconds{1000});
        auto second=service.begin_request(std::chrono::microseconds{2000});
        first=std::move(second); // The overwritten RAII scope is reported as cancelled.
        if (!first.cancel()) return 13;
        try { static_cast<void>(service.begin_request(std::chrono::microseconds{-1})); return 14; }
        catch (const std::invalid_argument&) {}
    }
    if (!service.flush(std::chrono::seconds(2))) return 15;
    const auto idle_requests=service.transport_statistics().session_requests;
    std::this_thread::sleep_for(std::chrono::milliseconds(650));
    if (service.transport_statistics().session_requests!=idle_requests) return 16;
    service.subscribe_notifications();
    std::this_thread::sleep_for(std::chrono::milliseconds(650));
    if (service.transport_statistics().session_requests<=idle_requests) return 17;
    service.subscribe_notifications(false);
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
