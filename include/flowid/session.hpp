#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace flowid {

enum class Transport { tcp, mptcp };
enum class Status {
    acknowledged, unavailable, timeout, authentication_failed, expired,
    closed, sequence_conflict, protocol_error, unsupported_transport,
    quota, invalid_configuration, pending
};

std::string_view name(Status status) noexcept;
std::string_view name(Transport transport) noexcept;

class Error : public std::runtime_error {
public:
    Error(Status status, const char* description);
    Status status() const noexcept { return status_; }
private:
    Status status_;
};

struct Endpoint {
    std::string address;
    std::uint16_t port = 0;
    Transport transport = Transport::tcp;
    std::chrono::system_clock::time_point expires;
};
using Resolver = std::function<std::vector<Endpoint>(std::string_view service)>;

// Read an atomically replaced, operator-owned endpoint directory on each lookup.
Resolver file_directory(std::filesystem::path path);

struct Options {
    Resolver resolve;
    std::filesystem::path ca, certificate, key;
    Transport transport = Transport::tcp;
    std::chrono::milliseconds operation_timeout{2000};
    std::chrono::milliseconds attempt_timeout{500};
};

struct ReconnectEvent {
    Endpoint endpoint;
    std::uint64_t committed;
    std::uint64_t association;
    std::chrono::microseconds elapsed;
};

struct FlushResult {
    Status status;
    std::uint64_t acknowledged;
    std::size_t pending;
};
struct Receipt {
    std::uint64_t sequence;
    FlushResult progress;
    bool acknowledged() const noexcept { return progress.acknowledged >= sequence; }
};
struct State {
    bool authenticated = false;
    bool closed = false;
    std::optional<Endpoint> endpoint;
    std::uint64_t acknowledged = 0;
    std::uint64_t association = 0;
    std::uint64_t reconnects = 0;
    std::size_t pending = 0;
    std::size_t pending_bytes = 0;
};

inline constexpr std::size_t max_message_bytes = 16 * 1024;
inline constexpr std::size_t max_pending_messages = 64;
inline constexpr std::size_t max_pending_bytes = 256 * 1024;

class Session {
public:
    Session(Session&&) noexcept;
    Session& operator=(Session&&) noexcept;
    ~Session();

    std::uint64_t enqueue(std::string_view message);
    Receipt send(std::string_view message);
    FlushResult flush();
    // Re-resolve endpoints and resume even if the current transport is healthy.
    FlushResult recover();
    // Requires an empty outbox. A failed close can be retried.
    Status close();

    void on_reconnect(std::function<void(const ReconnectEvent&)> callback);
    State state() const;
private:
    struct Impl;
    explicit Session(std::unique_ptr<Impl> impl);
    std::unique_ptr<Impl> impl_;
    friend Session connect(std::string_view, Options);
};

// Establish a new authenticated session. This is a synchronous, single-owner API.
Session connect(std::string_view service, Options options);

} // namespace flowid
