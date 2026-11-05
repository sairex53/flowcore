#pragma once

#include <chrono>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>

namespace flowcore {
enum class Priority { Critical, Interactive, Standard };
enum class Outcome { Ok, Timeout, Failure };

struct NetworkIntent {
    std::string name;
    Priority priority = Priority::Critical;
    std::chrono::microseconds target_p99 = std::chrono::milliseconds(30);
    std::uint32_t desired_kbit = 0;
    std::uint32_t minimum_kbit = 0;
    std::uint32_t maximum_kbit = 0;
    unsigned background_min_percent = 20;
    bool prefer_reliable = false;
    std::optional<std::chrono::microseconds> deadline;
    std::chrono::seconds ttl{300};
    bool persistent = false;
};

namespace detail { struct Registration; }

class Request {
public:
    Request(Request&& other) noexcept;
    Request& operator=(Request&&) = delete;
    Request(const Request&) = delete;
    Request& operator=(const Request&) = delete;
    ~Request();
    [[nodiscard]] bool complete(Outcome outcome = Outcome::Ok) noexcept;

private:
    friend class ServiceHandle;
    explicit Request(std::shared_ptr<const detail::Registration> registration);
    std::shared_ptr<const detail::Registration> registration_;
    std::chrono::steady_clock::time_point started_;
};

class ServiceHandle {
public:
    [[nodiscard]] Request begin_request() const;
private:
    friend class Client;
    explicit ServiceHandle(std::shared_ptr<const detail::Registration> registration);
    std::shared_ptr<const detail::Registration> registration_;
};

class Client {
public:
    explicit Client(std::string socket_path);
    [[nodiscard]] ServiceHandle register_service(const NetworkIntent& intent) const;
private:
    std::string socket_path_;
};
}
