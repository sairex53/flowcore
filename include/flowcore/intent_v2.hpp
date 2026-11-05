#pragma once
#include <chrono>
#include <cstdint>
#include <memory>
#include <string>

namespace flowcore::v2 {
enum class Outcome { Ok, Timeout, Failure, Cancelled };
enum class Priority { Standard, Interactive, Critical };
struct Intent {
    std::string service;
    Priority priority = Priority::Standard;
    std::uint32_t target_latency_us = 30000;
    std::uint32_t desired_kbit = 0, minimum_kbit = 0, maximum_kbit = 0;
    std::uint32_t deadline_us = 0, lifetime_seconds = 300;
    bool prefer_reliable = false, prefer_isolation = false;
};
struct Statistics {
    std::uint64_t queued, accepted, dropped, rejected, transport_errors;
};
namespace detail { struct Channel; }
class Request {
public:
    Request(Request&&) noexcept;
    Request(const Request&) = delete;
    Request& operator=(const Request&) = delete;
    ~Request();
    bool complete(Outcome = Outcome::Ok) noexcept;
    std::uint64_t id() const noexcept { return id_; }
private:
    friend class Service;
    Request(std::shared_ptr<detail::Channel>, std::uint32_t, std::uint64_t);
    std::shared_ptr<detail::Channel> channel_;
    std::uint64_t id_=0, start_=0, generation_=0, cookie_=0;
    std::uint32_t deadline_=0;
};
class Service {
public:
    Service(std::string socket_path, Intent intent);
    Request begin_request(std::uint32_t deadline_us = 0, std::uint64_t cookie = 0) const;
    bool wait_ready(std::chrono::milliseconds timeout) const;
    bool flush(std::chrono::milliseconds timeout) const;
    bool ready() const noexcept;
    Statistics statistics() const noexcept;
    // Blocking setup operation. Returned cookie is attested by a received FD.
    std::uint64_t associate_socket(int fd) const;
    // Latest coalesced state bits: SLO, congestion, fallback, interface, capacity.
    std::uint32_t notifications() const noexcept;
private:
    std::shared_ptr<detail::Channel> channel_;
};
}
