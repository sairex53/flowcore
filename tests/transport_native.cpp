#include <flowcore/intent_v2.hpp>
#include <sys/socket.h>
#include <unistd.h>
#include <stdexcept>
struct Socket {
    int fd=socket(AF_INET,SOCK_DGRAM,0);
    ~Socket() { if (fd>=0) close(fd); }
};
int main() {
    Socket socket;
    if (socket.fd<0) return 1;
    flowcore::v2::Service::set_socket_priority(socket.fd,4);
    int value=0;
    socklen_t length=sizeof(value);
    if (getsockopt(socket.fd,SOL_SOCKET,SO_PRIORITY,&value,&length) || value!=4) return 2;
    try {
        flowcore::v2::Service::set_socket_priority(socket.fd,7);
    } catch (const std::invalid_argument&) { return 0; }
    return 3;
}
