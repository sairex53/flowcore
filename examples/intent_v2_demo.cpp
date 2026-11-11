#include "flowcore/intent_v2.hpp"
#include <algorithm>
#include <arpa/inet.h>
#include <chrono>
#include <cerrno>
#include <iostream>
#include <netinet/tcp.h>
#include <optional>
#include <stdexcept>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>
using namespace flowcore::v2;
namespace {
void transfer(int fd, void* data, size_t bytes, bool write) {
    auto* cursor=static_cast<char*>(data);
    while (bytes) {
        auto count=write?send(fd,cursor,bytes,MSG_NOSIGNAL):recv(fd,cursor,bytes,0);
        if (count<0 && errno==EINTR) continue;
        if (count<=0) throw std::runtime_error("TCP transfer failed");
        cursor+=count; bytes-=static_cast<size_t>(count);
    }
}
}
int main(int argc,char** argv) {
    if (argc!=5) return 2;
    try {
        auto port=std::stoul(argv[2]);
        auto bytes=std::stoul(argv[4]);
        if (!port || port>65535 || !bytes || bytes>65536) throw std::invalid_argument("port/payload outside supported range");
        Intent intent;
        intent.service=argv[3];
        intent.deadline_us=25000;
        intent.priority=std::string(argv[3])=="payment"?Priority::Critical:
                        std::string(argv[3])=="database"?Priority::Interactive:Priority::Standard;
        Service service(argv[1],intent);
        const bool telemetry_online=service.wait_ready(std::chrono::milliseconds{250});
        int fd=socket(AF_INET,SOCK_STREAM|SOCK_CLOEXEC,0);
        if (fd<0) throw std::runtime_error("socket failed");
        struct Owner { int fd; ~Owner(){close(fd);} } owner{fd};
        int enabled=1;
        if (setsockopt(fd,IPPROTO_TCP,TCP_NODELAY,&enabled,sizeof(enabled))) throw std::runtime_error("TCP_NODELAY failed");
        timeval timeout{2,0};
        if (setsockopt(fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout)) ||
            setsockopt(fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout))) throw std::runtime_error("socket timeouts failed");
        sockaddr_in address{};
        address.sin_family=AF_INET; address.sin_port=htons(static_cast<unsigned short>(port));
        inet_pton(AF_INET,"127.0.0.1",&address.sin_addr);
        if (connect(fd,reinterpret_cast<sockaddr*>(&address),sizeof(address))) throw std::runtime_error("connect failed");
        std::uint64_t cookie=0;
        if (telemetry_online) {
            try { cookie=service.associate_socket(fd); }
            catch (const std::exception&) { /* Network I/O remains independent of optional correlation. */ }
        }
        std::vector<char> payload(static_cast<size_t>(bytes),'Q');
        std::vector<double> latencies;
        for (int i=0;i<60;++i) {
            auto begin=std::chrono::steady_clock::now();
            std::optional<Request> request;
            if (telemetry_online) request.emplace(service.begin_request(std::chrono::microseconds{0},cookie));
            auto length=htonl(static_cast<std::uint32_t>(payload.size()));
            transfer(fd,&length,sizeof(length),true);
            transfer(fd,payload.data(),payload.size(),true);
            char reply[64];
            transfer(fd,reply,sizeof(reply),false);
            if (request) static_cast<void>(request->complete());
            latencies.push_back(std::chrono::duration<double,std::milli>(std::chrono::steady_clock::now()-begin).count());
        }
        if (!service.flush(std::chrono::seconds(3))) throw std::runtime_error("telemetry flush timeout");
        std::sort(latencies.begin(),latencies.end());
        auto stats=service.statistics();
        std::cout<<"{\"service\":\""<<intent.service<<"\",\"requests\":60,\"p99_ms\":"<<latencies.back()
                 <<",\"accepted\":"<<stats.accepted<<",\"rejected\":"<<stats.rejected
                 <<",\"dropped\":"<<stats.dropped<<"}\n";
    } catch (const std::exception& error) { std::cerr<<error.what()<<'\n';return 1; }
}
