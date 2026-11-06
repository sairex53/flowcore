#include "flowcore/intent_v2.hpp"
#include <atomic>
#include <chrono>
#include <iostream>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>
using namespace flowcore::v2;
int main(int argc,char** argv) {
    if (argc!=2) return 2;
    std::atomic<unsigned> failed{0};
    std::vector<std::thread> clients;
    for (unsigned i=0;i<8;++i) clients.emplace_back([&,i] {
        try {
            Intent intent;intent.service="session_"+std::to_string(i);
            Service service(argv[1],intent);
            if (!service.wait_ready(std::chrono::seconds(3))) { ++failed;return; }
            for (unsigned n=0;n<16;++n) {
                auto request=service.begin_request();
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
                static_cast<void>(request.complete());
            }
            if (!service.flush(std::chrono::seconds(3))) { ++failed;return; }
            auto stats=service.transport_statistics();
            if (stats.session_connections!=1 || stats.legacy_requests ||
                stats.session_requests<2 || !service.statistics().accepted) ++failed;
        } catch (...) { ++failed; }
    });
    for (auto& client:clients) client.join();
    if (failed) return 3;
    Intent intent;intent.service="short_lease";intent.lifetime_seconds=1;
    Service service(argv[1],intent);
    if (!service.wait_ready(std::chrono::seconds(3))) return 4;
    const auto generation=service.registration_generation();
    int descriptor=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC,0);
    if (descriptor<0) return 5;
    try {
        const auto cookie=service.associate_socket(descriptor);
        {
            auto request=service.begin_request(10000,cookie);
            service.flush(std::chrono::seconds(3));
            // An active request and cookie must survive several one-second lease periods.
            std::this_thread::sleep_for(std::chrono::milliseconds(2350));
            static_cast<void>(request.complete(Outcome::Timeout));
        }
        service.flush(std::chrono::seconds(3));
        if (service.registration_generation()!=generation ||
            !service.notifications_time_ns()) { close(descriptor);return 6; }
        for (unsigned i=0;i<8;++i) {
            auto request=service.begin_request(10000,cookie);
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            static_cast<void>(request.complete());
        }
        if (!service.flush(std::chrono::seconds(3))) { close(descriptor);return 7; }
        auto stats=service.statistics();
        auto transport=service.transport_statistics();
        if (stats.rejected || stats.transport_errors || transport.session_connections!=1 ||
            transport.legacy_requests || transport.session_requests<6) { close(descriptor);return 8; }
        std::cout<<"{\"connections\":"<<transport.session_connections<<",\"requests\":"<<transport.session_requests
                 <<",\"generation\":"<<generation<<",\"accepted\":"<<stats.accepted
                 <<",\"dropped\":"<<stats.dropped<<"}\n";
    } catch (...) { close(descriptor);return 9; }
    close(descriptor);
}
