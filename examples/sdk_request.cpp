#include "flowcore/client.hpp"
#include <sys/socket.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: flowcore-sdk-request CONTROL_SOCKET\n";
        return 2;
    }
    try {
        flowcore::Client client(argv[1]);
        flowcore::NetworkIntent intent;
        intent.name = "sdk_demo";
        auto service = client.register_service(intent);
        auto request = service.begin_request();
        struct Socket { int fd; ~Socket() { if (fd >= 0) close(fd); } } socket_fd{socket(AF_INET, SOCK_STREAM, 0)};
        sockaddr_in target{};
        target.sin_family = AF_INET;
        target.sin_port = htons(9000);
        inet_pton(AF_INET, "10.203.0.2", &target.sin_addr);
        timeval timeout{2, 0};
        if (socket_fd.fd < 0 || setsockopt(socket_fd.fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) ||
            setsockopt(socket_fd.fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) ||
            connect(socket_fd.fd, reinterpret_cast<sockaddr*>(&target), sizeof(target))) {
            static_cast<void>(request.complete(flowcore::Outcome::Failure));
            return 1;
        }
        std::string payload(4096, 'Q');
        std::string reply(64, '\0');
        bool ok = send(socket_fd.fd, payload.data(), payload.size(), MSG_NOSIGNAL) == 4096 &&
                  recv(socket_fd.fd, reply.data(), reply.size(), MSG_WAITALL) == 64 && reply == std::string(64, 'R');
        return request.complete(ok ? flowcore::Outcome::Ok : flowcore::Outcome::Failure) && ok ? 0 : 1;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
