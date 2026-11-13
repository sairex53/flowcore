#include "flowcore/intent_v2.hpp"
#include <atomic>
#include <chrono>
#include <cstring>
#include <iostream>
#include <string>
#include <sys/socket.h>
#include <sys/time.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>
#include <vector>
using namespace flowcore::v2;
namespace {
bool traffic_report_present(const char* path) {
    const int fd=socket(AF_UNIX,SOCK_STREAM|SOCK_CLOEXEC,0);
    if(fd<0) return false;
    sockaddr_un address{};
    address.sun_family=AF_UNIX;
    if(std::strlen(path)>=sizeof(address.sun_path)) { close(fd); return false; }
    std::strcpy(address.sun_path,path);
    timeval timeout{3,0};
    setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout));
    if(connect(fd,reinterpret_cast<sockaddr*>(&address),sizeof(address))<0) { close(fd); return false; }
    constexpr char request[]="{\"op\":\"observe\"}\n";
    if(send(fd,request,sizeof(request)-1,MSG_NOSIGNAL)!=sizeof(request)-1) { close(fd); return false; }
    std::string reply;
    char buffer[4096];
    while(reply.size()<65536) {
        const auto count=recv(fd,buffer,sizeof(buffer),0);
        if(count<=0) break;
        reply.append(buffer,static_cast<std::size_t>(count));
        if(reply.find('\n')!=std::string::npos) break;
    }
    close(fd);
    return reply.find("\"traffic_reports\": 1")!=std::string::npos &&
           reply.find("\"bytes_offered\": 1024")!=std::string::npos &&
           reply.find("\"udp_messages_transmitted\": 2")!=std::string::npos;
}
}
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
    if (!service.wait_ready(std::chrono::seconds(3))) {
        const auto stats=service.statistics();
        const auto transport=service.transport_statistics();
        std::cerr<<"short lease registration failed: accepted="<<stats.accepted
                 <<" rejected="<<stats.rejected<<" errors="<<stats.transport_errors
                 <<" connections="<<transport.session_connections
                 <<" requests="<<transport.session_requests<<"\n";
        return 4;
    }
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
            TrafficAccounting traffic{};
            traffic.valid=true;
            traffic.bytes_offered=1024;
            traffic.bytes_admitted=1024;
            traffic.bytes_transmitted=1000;
            traffic.bytes_received=2048;
            traffic.udp_messages_attempted=3;
            traffic.udp_messages_transmitted=2;
            traffic.udp_send_failures=1;
            traffic.udp_urgent_messages=1;
            traffic.udp_background_messages=2;
            static_cast<void>(request.complete(Outcome::Timeout,traffic));
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
        const bool reported=traffic_report_present(argv[1]);
        if(!reported) { close(descriptor);return 10; }
        std::cout<<"{\"connections\":"<<transport.session_connections<<",\"requests\":"<<transport.session_requests
                 <<",\"generation\":"<<generation<<",\"accepted\":"<<stats.accepted
                 <<",\"dropped\":"<<stats.dropped<<",\"traffic_observed\":"
                 <<(reported?"true":"false")<<"}\n";
    } catch (...) { close(descriptor);return 9; }
    close(descriptor);
}
