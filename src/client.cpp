#include "flowcore/client.hpp"
#include <algorithm>
#include <cerrno>
#include <cstring>
#include <poll.h>
#include <stdexcept>
#include <string_view>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <utility>

namespace flowcore {
namespace {
using Clock = std::chrono::steady_clock;
struct Descriptor {
    int value;
    ~Descriptor() { if (value >= 0) close(value); }
};

void wait_for(int fd, short events, Clock::time_point deadline) {
    while (true) {
        auto time = std::chrono::ceil<std::chrono::milliseconds>(deadline - Clock::now()).count();
        if (time <= 0) throw std::runtime_error("FLOWCORE IPC deadline exceeded");
        pollfd target{fd, events, 0};
        int ready = poll(&target, 1, static_cast<int>(time));
        if (ready > 0) return;
        if (ready < 0 && errno == EINTR) continue;
        throw std::runtime_error("FLOWCORE IPC unavailable or timed out");
    }
}

std::string rpc(const std::string& path, std::string data, int passed_fd = -1) {
    sockaddr_un address{};
    address.sun_family = AF_UNIX;
    if (path.empty() || path.size() >= sizeof(address.sun_path) || path.find('\0') != std::string::npos)
        throw std::invalid_argument("invalid FLOWCORE socket path");
    std::memcpy(address.sun_path, path.c_str(), path.size() + 1);
    Descriptor fd{socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0)};
    if (fd.value < 0) throw std::runtime_error("cannot create FLOWCORE socket");
    auto deadline = Clock::now() + std::chrono::milliseconds(250);
    if (connect(fd.value, reinterpret_cast<sockaddr*>(&address), sizeof(address)) != 0) {
        if (errno != EINPROGRESS) throw std::runtime_error("flowcored unavailable");
        wait_for(fd.value, POLLOUT, deadline);
        int error = 0;
        socklen_t length = sizeof(error);
        if (getsockopt(fd.value, SOL_SOCKET, SO_ERROR, &error, &length) || error)
            throw std::runtime_error("cannot connect to flowcored");
    }
    ucred peer{};
    socklen_t size = sizeof(peer);
    if (getsockopt(fd.value, SOL_SOCKET, SO_PEERCRED, &peer, &size) ||
        (peer.uid != 0 && peer.uid != geteuid()))
        throw std::runtime_error("unexpected FLOWCORE socket peer");
    data += '\n';
    std::string_view unsent(data);
    if (passed_fd >= 0) {
        wait_for(fd.value, POLLOUT, deadline);
        iovec payload{data.data(), data.size()};
        alignas(cmsghdr) char control[CMSG_SPACE(sizeof(int))]{};
        msghdr message{};
        message.msg_iov = &payload;
        message.msg_iovlen = 1;
        message.msg_control = control;
        message.msg_controllen = sizeof(control);
        auto* header = CMSG_FIRSTHDR(&message);
        header->cmsg_level = SOL_SOCKET;
        header->cmsg_type = SCM_RIGHTS;
        header->cmsg_len = CMSG_LEN(sizeof(int));
        std::memcpy(CMSG_DATA(header), &passed_fd, sizeof(int));
        auto sent = sendmsg(fd.value, &message, MSG_NOSIGNAL);
        if (sent <= 0) throw std::runtime_error("descriptor association send failed");
        unsent.remove_prefix(static_cast<size_t>(sent));
    }
    while (!unsent.empty()) {
        wait_for(fd.value, POLLOUT, deadline);
        auto count = send(fd.value, unsent.data(), unsent.size(), MSG_NOSIGNAL);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (count <= 0) throw std::runtime_error("FLOWCORE send failed");
        unsent.remove_prefix(static_cast<size_t>(count));
    }
    std::string reply;
    constexpr size_t reply_limit = 32768;
    while (reply.size() < reply_limit) {
        wait_for(fd.value, POLLIN, deadline);
        char buffer[4096];
        auto count = recv(fd.value, buffer, std::min(sizeof(buffer), reply_limit - reply.size()), 0);
        if (count < 0 && (errno == EINTR || errno == EAGAIN)) continue;
        if (count <= 0) break;
        reply.append(buffer, static_cast<size_t>(count));
        if (reply.ends_with('\n')) break;
    }
    if (reply.starts_with("{\"ok\": false,") && reply.find("\"code\": \"forbidden\"") != std::string::npos)
        throw std::runtime_error("FLOWCORE operation forbidden");
    if (!reply.ends_with('\n') || !reply.starts_with("{\"ok\": true,"))
        throw std::runtime_error("flowcored rejected the request or returned an invalid response");
    return reply;
}

bool valid_label(std::string_view name) {
    return !name.empty() && name.size() <= 32 && name.front() >= 'a' && name.front() <= 'z' &&
        name.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789_-") == std::string_view::npos;
}
}

namespace detail {
std::string exchange(const std::string& path, std::string data, int fd) { return rpc(path, std::move(data), fd); }
struct Registration {
    std::string path, name, token, deadline;
    ~Registration() {
        if (token.empty()) return;
        try {
            rpc(path, "{\"op\":\"intent_remove\",\"name\":\"" + name +
                           "\",\"instance_token\":\"" + token + "\"}");
        } catch (...) {}
    }
};
}

Client::Client(std::string path) : socket_path_(std::move(path)) {}

ServiceHandle Client::register_service(const NetworkIntent& intent) const {
    if (!valid_label(intent.name)) throw std::invalid_argument("invalid FLOWCORE service name");
    const char* priority = intent.priority == Priority::Critical ? "critical" :
                           intent.priority == Priority::Interactive ? "interactive" : "standard";
    auto deadline = intent.deadline ? std::to_string(intent.deadline->count()/1000.0) : "null";
    auto registration = std::make_shared<detail::Registration>(
        socket_path_, intent.name, std::string{}, deadline);
    auto result = rpc(socket_path_, "{\"op\":\"intent_register\",\"intent\":{\"name\":\"" + intent.name +
        "\",\"priority\":\"" + priority + "\",\"target_p99_ms\":" + std::to_string(intent.target_p99.count()/1000.0) +
        ",\"desired_kbit\":" + std::to_string(intent.desired_kbit) +
        ",\"min_kbit\":" + std::to_string(intent.minimum_kbit) +
        ",\"max_kbit\":" + std::to_string(intent.maximum_kbit) +
        ",\"background_min_pct\":" + std::to_string(intent.background_min_percent) +
        ",\"reliability\":\"" + (intent.prefer_reliable ? "prefer_reliable" : "balanced") +
        "\",\"deadline_ms\":" + deadline + ",\"ttl_seconds\":" + std::to_string(intent.ttl.count()) +
        ",\"persistent\":" + (intent.persistent ? "true" : "false") + "}}");
    constexpr std::string_view field = "\"instance_token\": \"";
    auto pos = result.find(field);
    if (pos == std::string::npos) throw std::runtime_error("FLOWCORE reply missing instance token");
    pos += field.size();
    auto token = result.substr(pos, 32);
    if (token.size() != 32 || result.size() <= pos+32 || result[pos+32] != '"' ||
        token.find_first_not_of("0123456789abcdef") != std::string::npos)
        throw std::runtime_error("invalid FLOWCORE instance token");
    registration->token = std::move(token);
    return ServiceHandle(std::move(registration));
}

ServiceHandle::ServiceHandle(std::shared_ptr<const detail::Registration> registration)
    : registration_(std::move(registration)) {}
Request ServiceHandle::begin_request() const { return Request(registration_); }
Request::Request(std::shared_ptr<const detail::Registration> registration)
    : registration_(std::move(registration)), started_(Clock::now()) {}
Request::Request(Request&& other) noexcept
    : registration_(std::move(other.registration_)), started_(other.started_) {}
Request::~Request() { if (registration_) static_cast<void>(complete(Outcome::Failure)); }

bool Request::complete(Outcome outcome) noexcept {
    auto ended = Clock::now();
    auto registration = std::move(registration_);
    if (!registration) return false;
    try {
        const char* label = outcome == Outcome::Ok ? "ok" : outcome == Outcome::Timeout ? "timeout" : "failure";
        auto duration = std::chrono::duration_cast<std::chrono::microseconds>(ended-started_).count();
        rpc(registration->path, "{\"op\":\"request_sample\",\"service\":\"" + registration->name +
            "\",\"instance_token\":\"" + registration->token + "\",\"outcome\":\"" + label +
            "\",\"duration_us\":" + std::to_string(duration) + ",\"deadline_ms\":" + registration->deadline + "}");
        return true;
    } catch (...) { return false; }
}
}
