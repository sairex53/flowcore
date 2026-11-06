#include "intent_transport.hpp"
#include <algorithm>
#include <cerrno>
#include <charconv>
#include <cstring>
#include <chrono>
#include <poll.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
namespace flowcore::detail { std::string exchange(const std::string&,std::string,int); }
namespace flowcore::v2::detail {
std::string milliseconds(std::uint32_t microseconds) {
    // Integer-only formatting is independent of the application's LC_NUMERIC.
    return std::to_string(microseconds/1000)+"."+std::to_string(1000+microseconds%1000).substr(1);
}
std::uint64_t unsigned_field(std::string_view text,std::string_view key) {
    auto position=text.find(key);
    if (position==std::string_view::npos) throw RpcError(Error::ProtocolError,"missing integer field");
    auto* begin=text.data()+position+key.size();
    auto* end=text.data()+text.size();
    std::uint64_t value=0;
    auto parsed=std::from_chars(begin,end,value);
    if (parsed.ec!=std::errc{} || parsed.ptr==begin)
        throw RpcError(Error::ProtocolError,"invalid unsigned integer");
    auto* cursor=parsed.ptr;
    while (cursor!=end && (*cursor==' ' || *cursor=='\n' || *cursor=='\r' || *cursor=='\t')) ++cursor;
    if (cursor==end || (*cursor!=',' && *cursor!='}' && *cursor!=']'))
        throw RpcError(Error::ProtocolError,"invalid integer delimiter");
    return value;
}
namespace {
std::string versioned(std::string reply) {
    if (unsigned_field(reply,"\"version\": ")!=2)
        throw RpcError(Error::ProtocolError,"unsupported intent response version");
    return reply;
}
using Clock=std::chrono::steady_clock;
void wait(int fd,short events,Clock::time_point deadline) {
    while (true) {
        auto remaining=std::chrono::ceil<std::chrono::milliseconds>(deadline-Clock::now()).count();
        if (remaining<=0) throw RpcError(Error::TransportUnavailable,"intent IPC deadline exceeded");
        pollfd descriptor{fd,events,0};
        auto result=poll(&descriptor,1,static_cast<int>(remaining));
        if (result>0) return;
        if (result<0 && errno==EINTR) continue;
        throw RpcError(Error::TransportUnavailable,"intent IPC poll failed");
    }
}
}
Transport::Transport(std::string path):path_(std::move(path)) {
    auto slash=path_.find_last_of('/');
    if (path_.substr(slash==std::string::npos?0:slash+1)=="control.sock")
        endpoint_=(slash==std::string::npos?std::string():path_.substr(0,slash+1))+"intent.sock";
    if (path_.empty() || path_.find('\0')!=std::string::npos ||
        path_.size()>=sizeof(sockaddr_un::sun_path) || endpoint_.size()>=sizeof(sockaddr_un::sun_path))
        throw std::invalid_argument("invalid intent socket path");
}
Transport::~Transport() { close(); }
void Transport::close() noexcept { if (fd_>=0) ::close(fd_); fd_=-1; }
std::string Transport::exchange(std::string data) {
    if (endpoint_.empty()) {
        ++legacy; return versioned(flowcore::detail::exchange(path_,std::move(data),-1));
    }
    auto deadline=Clock::now()+std::chrono::milliseconds(250);
    try {
        if (fd_<0) {
            fd_=socket(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0);
            if (fd_<0) throw RpcError(Error::TransportUnavailable,"cannot create intent socket");
            sockaddr_un address{};address.sun_family=AF_UNIX;
            std::memcpy(address.sun_path,endpoint_.c_str(),endpoint_.size()+1);
            if (connect(fd_,reinterpret_cast<sockaddr*>(&address),sizeof(address))) {
                const int error=errno;
                if (error==ENOENT) {
                    close(); ++legacy;
                    return versioned(flowcore::detail::exchange(path_,std::move(data),-1));
                }
                if (error!=EINPROGRESS) throw RpcError(Error::TransportUnavailable,"intent daemon unavailable");
                wait(fd_,POLLOUT,deadline);
                int status=0;socklen_t length=sizeof(status);
                if (getsockopt(fd_,SOL_SOCKET,SO_ERROR,&status,&length) || status)
                    throw RpcError(Error::TransportUnavailable,"intent connect failed");
            }
            ucred peer{};socklen_t length=sizeof(peer);
            if (getsockopt(fd_,SOL_SOCKET,SO_PEERCRED,&peer,&length) ||
                (peer.uid!=0 && peer.uid!=geteuid()))
                throw RpcError(Error::AuthorizationDenied,"unexpected intent peer");
            ++connections;
        }
        data+='\n';
        if (data.size()>65536) throw RpcError(Error::InvalidRequest,"intent frame exceeds bound");
        while (true) {
            wait(fd_,POLLOUT,deadline);
            auto sent=send(fd_,data.data(),data.size(),MSG_NOSIGNAL);
            if (sent<0 && (errno==EAGAIN || errno==EINTR)) continue;
            if (sent!=static_cast<ssize_t>(data.size()))
                throw RpcError(Error::TransportUnavailable,"intent send failed");
            ++requests;
            break;
        }
        char buffer[32769];
        ssize_t length;
        while (true) {
            wait(fd_,POLLIN,deadline);
            length=recv(fd_,buffer,sizeof(buffer),MSG_TRUNC);
            if (length<0 && (errno==EAGAIN || errno==EINTR)) continue;
            break;
        }
        if (length<=0) throw RpcError(Error::TransportUnavailable,"intent connection closed");
        if (length>32768) throw RpcError(Error::ProtocolError,"intent response bound exceeded");
        std::string reply(buffer,static_cast<std::size_t>(length));
        if (!reply.ends_with('\n')) throw RpcError(Error::ProtocolError,"intent framing error");
        if (reply.starts_with("{\"ok\": false,")) {
            if (reply.find("\"code\": \"forbidden\"")!=std::string::npos)
                throw RpcError(Error::AuthorizationDenied,"FLOWCORE operation forbidden",
                    reply.find("\"retry_registration\": true")!=std::string::npos);
            if (reply.find("\"code\": \"operation_failed\"")!=std::string::npos)
                throw RpcError(Error::TransportUnavailable,"intent backend unavailable");
            throw RpcError(Error::InvalidRequest,"intent operation rejected");
        }
        if (!reply.starts_with("{\"ok\": true,"))
            throw RpcError(Error::ProtocolError,"invalid intent response");
        return versioned(std::move(reply));
    } catch (...) { close(); throw; }
}
}
