#include "flowcore/client.hpp"
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>
#include <chrono>
#include <filesystem>
#include <iostream>
#include <string>
#include <thread>

int main() {
    using namespace std::chrono_literals;
    try {
        const auto deadline = std::chrono::steady_clock::now()+20s;
        while (!std::filesystem::exists("/gate/start")) {
            if (std::chrono::steady_clock::now()>=deadline) return 2;
            std::this_thread::sleep_for(50ms);
        }
        flowcore::Client client("/control/control.sock");
        flowcore::NetworkIntent intent;
        intent.name = "container_demo";
        auto service = client.register_service(intent);
        int completed = 0, reports = 0;
        for (int i=0; i<40; ++i) {
            auto request = service.begin_request();
            struct Fd { int fd; ~Fd() { if (fd>=0) close(fd); } } fd{socket(AF_INET,SOCK_STREAM,0)};
            sockaddr_in addr{};
            addr.sin_family = AF_INET;
            addr.sin_port = htons(9003);
            inet_pton(AF_INET,"10.203.0.2",&addr.sin_addr);
            timeval timeout{2,0};
            std::string payload(4096,'Q'), reply(64,'\0');
            bool ok = fd.fd>=0 &&
                !setsockopt(fd.fd,SOL_SOCKET,SO_SNDTIMEO,&timeout,sizeof(timeout)) &&
                !setsockopt(fd.fd,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout)) &&
                !connect(fd.fd,reinterpret_cast<sockaddr*>(&addr),sizeof(addr)) &&
                send(fd.fd,payload.data(),payload.size(),MSG_NOSIGNAL)==4096 &&
                recv(fd.fd,reply.data(),reply.size(),MSG_WAITALL)==64 && reply==std::string(64,'R');
            completed += ok;
            reports += request.complete(ok ? flowcore::Outcome::Ok : flowcore::Outcome::Failure);
            std::this_thread::sleep_for(45ms);
        }
        std::cout << "{\"completed\":" << completed << ",\"reported\":" << reports << "}\n";
        return completed==40 && reports==40 ? 0 : 1;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
