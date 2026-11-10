#include "flowcore/socket_pacing.hpp"
#include "flowcore/intent_v2.hpp"

#include <fcntl.h>
#include <linux/sockios.h>
#include <linux/tcp.h>
#include <netinet/in.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <cstddef>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <utility>

namespace flowcore::v2::pacing {
namespace {
using namespace std::chrono_literals;
constexpr Rate rate_limit = 100'000'000'000ULL;
constexpr auto stale = std::errc::state_not_recoverable;
Error error() { return {errno, std::generic_category()}; }

class Descriptor {
public:
    explicit Descriptor(int fd) noexcept : fd_(fd) {}
    Descriptor(Descriptor&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
    ~Descriptor() { if (fd_ >= 0) close(fd_); }
    int get() const noexcept { return fd_; }
private:
    int fd_;
};

template<class T>
std::expected<T, Error> get(int fd, int level, int option) noexcept {
    T value{};
    socklen_t length = sizeof(value);
    if (getsockopt(fd, level, option, &value, &length)) return std::unexpected(error());
    if (length != sizeof(value)) return std::unexpected(std::make_error_code(std::errc::protocol_error));
    return value;
}

template<class T>
Error set(int fd, int level, int option, const T& value) noexcept {
    return setsockopt(fd, level, option, &value, sizeof(value)) ? error() : Error{};
}

std::expected<Descriptor, Error> pin(int fd, std::uint64_t expected = 0) noexcept {
    const int copied = fcntl(fd, F_DUPFD_CLOEXEC, 0);
    if (copied < 0) return std::unexpected(error());
    Descriptor result(copied);
    const auto protocol = get<int>(copied, SOL_SOCKET, SO_PROTOCOL);
    if (!protocol) return std::unexpected(protocol.error());
    const auto domain = get<int>(copied, SOL_SOCKET, SO_DOMAIN);
    if (!domain) return std::unexpected(domain.error());
    if (*protocol != IPPROTO_TCP || (*domain != AF_INET && *domain != AF_INET6))
        return std::unexpected(std::make_error_code(std::errc::protocol_not_supported));
    if (expected) {
        const auto cookie = get<std::uint64_t>(copied, SOL_SOCKET, SO_COOKIE);
        if (!cookie) return std::unexpected(cookie.error());
        if (*cookie != expected) return std::unexpected(std::make_error_code(stale));
    }
    return result;
}

template<class T>
Error change(int fd, int level, int option, T wanted, T& expected,
             RateTiming* timing = nullptr, bool verify_noop = false) noexcept {
    if (wanted == expected && !verify_noop) return {};
    auto started = timing ? Clock::now() : Clock::time_point{};
    auto before = get<T>(fd, level, option);
    if (timing) timing->precheck_ns = (Clock::now()-started).count();
    if (!before) return before.error();
    if (*before != expected) return std::make_error_code(std::errc::device_or_resource_busy);
    if (wanted == expected) return {};
    started = timing ? Clock::now() : Clock::time_point{};
    const auto failure = set(fd, level, option, wanted);
    if (timing) {
        timing->write_attempted = true;
        timing->write_ns = (Clock::now()-started).count();
    }
    if (failure) return failure;
    expected = wanted; // Kernel accepted a mutation. Compensate only if we still own it.
    started = timing ? Clock::now() : Clock::time_point{};
    auto observed = get<T>(fd, level, option);
    if (timing) timing->readback_ns = (Clock::now()-started).count();
    if (!observed) return observed.error();
    return *observed == expected ? Error{} : std::make_error_code(stale);
}

struct Registry {
    std::mutex lock;
    std::array<std::uint64_t, 256> cookies{};
};
Registry& registry() { static Registry value; return value; }

Error claim(std::uint64_t cookie) {
    auto& state = registry();
    const std::lock_guard guard(state.lock);
    if (std::ranges::find(state.cookies, cookie) != state.cookies.end())
        return std::make_error_code(std::errc::device_or_resource_busy);
    auto slot = std::ranges::find(state.cookies, 0);
    if (slot == state.cookies.end()) return std::make_error_code(std::errc::no_buffer_space);
    *slot = cookie;
    return {};
}
void release(std::uint64_t cookie) {
    auto& state = registry();
    const std::lock_guard guard(state.lock);
    auto slot = std::ranges::find(state.cookies, cookie);
    if (slot != state.cookies.end()) *slot = 0;
}

std::expected<Snapshot, Error> read(int fd) {
    Snapshot out;
    auto cookie = get<std::uint64_t>(fd, SOL_SOCKET, SO_COOKIE);
    auto rate = get<Rate>(fd, SOL_SOCKET, SO_MAX_PACING_RATE);
    auto lowat = get<std::uint32_t>(fd, IPPROTO_TCP, TCP_NOTSENT_LOWAT);
    auto priority = get<int>(fd, SOL_SOCKET, SO_PRIORITY);
    if (!cookie) return std::unexpected(cookie.error());
    if (!rate) return std::unexpected(rate.error());
    if (!lowat) return std::unexpected(lowat.error());
    if (!priority) return std::unexpected(priority.error());
    out.cookie = *cookie;
    out.maximum_rate = *rate;
    out.notsent_lowat = *lowat;
    out.priority = static_cast<unsigned>(*priority);

    std::array<char, 16> congestion{};
    socklen_t size = congestion.size();
    if (!getsockopt(fd, IPPROTO_TCP, TCP_CONGESTION, congestion.data(), &size)) {
        const auto end = std::ranges::find(congestion, '\0');
        out.congestion.assign(congestion.begin(), end);
    }
    tcp_info info{};
    size = sizeof(info);
    if (getsockopt(fd, IPPROTO_TCP, TCP_INFO, &info, &size)) return std::unexpected(error());
    const auto copy = [&]<class T>(std::size_t offset, T value, auto& dest) {
        if (offset + sizeof(T) <= size) dest = value;
    };
    copy(offsetof(tcp_info, tcpi_pacing_rate), info.tcpi_pacing_rate, out.pacing_rate);
    copy(offsetof(tcp_info, tcpi_delivery_rate), info.tcpi_delivery_rate, out.delivery_rate);
    copy(offsetof(tcp_info, tcpi_bytes_acked), info.tcpi_bytes_acked, out.bytes_acked);
    copy(offsetof(tcp_info, tcpi_notsent_bytes), info.tcpi_notsent_bytes, out.notsent_bytes);
    copy(offsetof(tcp_info, tcpi_snd_cwnd), info.tcpi_snd_cwnd, out.cwnd_segments);
    copy(offsetof(tcp_info, tcpi_rtt), info.tcpi_rtt, out.rtt_us);
    if (size > offsetof(tcp_info, tcpi_options)) out.ecn_negotiated = (info.tcpi_options & TCPI_OPT_ECN) != 0;
#ifdef TCPI_ECN_MODE_ACCECN
    if (size >= offsetof(tcp_info, tcpi_received_ce_bytes)+8)
        out.ecn_mode = static_cast<unsigned>(info.tcpi_ecn_mode);
#endif
    copy(offsetof(tcp_info, tcpi_delivered), info.tcpi_delivered, out.delivered_segments);
    copy(offsetof(tcp_info, tcpi_delivered_ce), info.tcpi_delivered_ce, out.delivered_ce_segments);
    copy(offsetof(tcp_info, tcpi_total_retrans), info.tcpi_total_retrans, out.total_retransmits);
    int queued = 0;
    if (!ioctl(fd, SIOCOUTQ, &queued) && queued >= 0) out.output_queue_bytes = queued;
    if (const auto buffer = get<int>(fd, SOL_SOCKET, SO_SNDBUF); buffer && *buffer > 0)
        out.send_buffer_bytes = *buffer;
    return out;
}

bool within(const Policy& policy, Rate rate) noexcept {
    return rate && policy.minimum_cap <= rate && rate <= policy.maximum_cap;
}

bool valid(const Policy& policy) {
    return !policy.service.empty() && policy.service.size() <= 64
        && policy.service.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
            == std::string::npos
        && policy.minimum_cap > 0 && policy.minimum_cap <= policy.maximum_cap
        && policy.maximum_cap <= rate_limit && (!policy.initial.maximum_rate || within(policy, policy.initial.maximum_rate))
        && (!policy.initial.priority || *policy.initial.priority <= 6)
        && (!policy.initial.notsent_lowat || *policy.initial.notsent_lowat <= 16*1024*1024)
        && policy.lifetime >= 1s && policy.lifetime <= 24h
        && policy.latency_target >= 0us && policy.latency_target <= 30s
        && policy.request_deadline >= 0us && policy.request_deadline <= 30s
        && policy.preferred_throughput <= rate_limit;
}
} // namespace

std::expected<Capabilities, Error> probe() noexcept {
    Descriptor socket_fd(socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, IPPROTO_TCP));
    const int fd = socket_fd.get();
    if (fd < 0) return std::unexpected(error());
    const auto cap = get<Rate>(fd, SOL_SOCKET, SO_MAX_PACING_RATE);
    const auto priority = get<int>(fd, SOL_SOCKET, SO_PRIORITY);
    const auto mark = get<unsigned>(fd, SOL_SOCKET, SO_MARK);
    const auto lowat = get<std::uint32_t>(fd, IPPROTO_TCP, TCP_NOTSENT_LOWAT);
    std::array<char, 16> congestion{};
    socklen_t size = congestion.size();
    const auto cc = getsockopt(fd, IPPROTO_TCP, TCP_CONGESTION, congestion.data(), &size) ? error() : Error{};
    tcp_info info{};
    size = sizeof(info);
    const auto metrics = getsockopt(fd, IPPROTO_TCP, TCP_INFO, &info, &size) ? error() : Error{};
    const auto cookie = get<std::uint64_t>(fd, SOL_SOCKET, SO_COOKIE);
    return Capabilities{cap ? set(fd, SOL_SOCKET, SO_MAX_PACING_RATE, *cap) : cap.error(),
                        priority ? set(fd, SOL_SOCKET, SO_PRIORITY, *priority) : priority.error(),
                        mark ? Error{} : mark.error(),
                        mark ? set(fd, SOL_SOCKET, SO_MARK, *mark) : mark.error(),
                        lowat ? set(fd, IPPROTO_TCP, TCP_NOTSENT_LOWAT, *lowat) : lowat.error(), cc, metrics,
                        cookie ? Error{} : cookie.error()};
}

std::expected<Snapshot, Error> inspect(int fd) {
    auto pinned = pin(fd);
    if (!pinned) return std::unexpected(pinned.error());
    return read(pinned->get());
}

Error select_congestion(int fd, std::string_view algorithm) {
    if (algorithm.empty() || algorithm.size() >= 16 ||
        algorithm.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789_") != std::string_view::npos)
        return std::make_error_code(std::errc::invalid_argument);
    auto pinned = pin(fd);
    if (!pinned) return pinned.error();
    std::array<char, 16> value{};
    std::ranges::copy(algorithm, value.begin());
    if (setsockopt(pinned->get(), IPPROTO_TCP, TCP_CONGESTION, value.data(), algorithm.size()+1)) return error();
    socklen_t length = value.size();
    value.fill('\0');
    if (getsockopt(pinned->get(), IPPROTO_TCP, TCP_CONGESTION, value.data(), &length)) return error();
    const auto end = std::ranges::find(value, '\0');
    return std::string_view(value.begin(), end) == algorithm ? Error{} : std::make_error_code(stale);
}

SocketPolicy::SocketPolicy(int fd, std::uint64_t cookie, Policy policy) noexcept
    : fd_(fd), owner_(getpid()), cookie_(cookie), policy_(std::move(policy)),
      expires_(Clock::now()+policy_.lifetime) {}

SocketPolicy::SocketPolicy(SocketPolicy&& other) noexcept
    : fd_(other.fd_), owner_(other.owner_), cookie_(other.cookie_), policy_(std::move(other.policy_)),
      expires_(other.expires_), original_(other.original_), applied_(other.applied_),
      active_(std::exchange(other.active_, false)), tainted_(other.tainted_),
      removal_error_(other.removal_error_) {}

std::expected<SocketPolicy, Error> SocketPolicy::attach(int fd, const Policy& policy) {
    if (!valid(policy)) return std::unexpected(std::make_error_code(std::errc::invalid_argument));
    auto pinned = pin(fd);
    if (!pinned) return std::unexpected(pinned.error());
    const int socket_fd = pinned->get();
    sockaddr_storage peer{};
    socklen_t length = sizeof(peer);
    if (getpeername(socket_fd, reinterpret_cast<sockaddr*>(&peer), &length)) return std::unexpected(error());
    auto identity = get<std::uint64_t>(socket_fd, SOL_SOCKET, SO_COOKIE);
    if (!identity) return std::unexpected(identity.error());
    if (!*identity) return std::unexpected(std::make_error_code(stale));
    SocketPolicy managed(fd, *identity, policy);
    if (auto failure = claim(*identity)) return std::unexpected(failure);
    managed.active_ = true;
    auto info = read(socket_fd);
    if (!info) return std::unexpected(info.error());
    managed.original_ = managed.applied_ = {info->maximum_rate, info->notsent_lowat, static_cast<int>(info->priority)};
    const auto& desired = policy.initial;
    auto& values = managed.applied_;
    Error failure;
    if (desired.maximum_rate) failure = change(socket_fd, SOL_SOCKET, SO_MAX_PACING_RATE, desired.maximum_rate, values.rate);
    if (!failure && desired.notsent_lowat) failure = change(socket_fd, IPPROTO_TCP, TCP_NOTSENT_LOWAT,
                                                          *desired.notsent_lowat, values.lowat);
    if (!failure && desired.priority) failure = change(socket_fd, SOL_SOCKET, SO_PRIORITY,
                                                       static_cast<int>(*desired.priority), values.priority);
    if (failure) {
        // Keep using the pin acquired before mutation, even if the
        // application's numeric descriptor was closed or replaced.
        const auto rollback = managed.restore(socket_fd);
        managed.active_ = false;
        release(managed.cookie_);
        if (rollback) return std::unexpected(std::make_error_code(stale));
        return std::unexpected(failure);
    }
    return managed;
}

SocketPolicy::~SocketPolicy() { remove(); }

std::uint64_t SocketPolicy::attest(const Service& service) const {
    if (getpid() != owner_) throw std::system_error(std::make_error_code(stale));
    const std::lock_guard lock(mutex_);
    if (!active_ || tainted_ || Clock::now() >= expires_)
        throw std::system_error(std::make_error_code(stale));
    auto pinned = pin(fd_, cookie_);
    if (!pinned) throw std::system_error(pinned.error());
    auto reported = service.associate_socket(pinned->get());
    if (reported != cookie_) throw std::system_error(std::make_error_code(stale));
    return reported;
}

std::expected<Snapshot, Error> SocketPolicy::snapshot() const {
    if (getpid() != owner_) return std::unexpected(std::make_error_code(stale));
    const std::lock_guard lock(mutex_);
    if (!active_ || tainted_) return std::unexpected(std::make_error_code(stale));
    auto pinned = pin(fd_, cookie_);
    if (!pinned) return std::unexpected(pinned.error());
    return read(pinned->get());
}

Error SocketPolicy::tick(Clock::time_point now) noexcept {
    if (getpid() != owner_) return std::make_error_code(stale);
    const std::lock_guard lock(mutex_);
    return tick_locked(now);
}

Error SocketPolicy::tick_locked(Clock::time_point now) noexcept {
    if (!active_) return std::make_error_code(stale);
    if (now < expires_) return tainted_ ? std::make_error_code(stale) : Error{};
    if (auto failure = remove_locked()) return failure;
    return std::make_error_code(std::errc::timed_out);
}

Error SocketPolicy::set_rate(Rate rate) noexcept { return set_rate(rate, nullptr); }

Error SocketPolicy::set_rate(Rate rate, RateTiming* timing) noexcept {
    if (timing) *timing = {};
    if (getpid() != owner_) return std::make_error_code(stale);
    const std::lock_guard lock(mutex_);
    if (auto failure = tick_locked(Clock::now())) return failure;
    if (!within(policy_, rate)) return std::make_error_code(std::errc::invalid_argument);
    const auto began = timing ? Clock::now() : Clock::time_point{};
    auto pinned = pin(fd_, cookie_);
    if (timing) timing->pin_ns = (Clock::now()-began).count();
    if (!pinned) return pinned.error();
    const auto before = applied_.rate;
    if (auto failure = change(pinned->get(), SOL_SOCKET, SO_MAX_PACING_RATE, rate, applied_.rate, timing, true)) {
        if (before != applied_.rate && change(pinned->get(), SOL_SOCKET, SO_MAX_PACING_RATE, before, applied_.rate)) {
            tainted_ = true;
            return std::make_error_code(stale);
        }
        return failure;
    }
    return {};
}

Error SocketPolicy::remove() noexcept {
    if (getpid() != owner_) return {};
    const std::lock_guard lock(mutex_);
    return remove_locked();
}

Error SocketPolicy::restore(int fd) noexcept {
    const auto priority = change(fd, SOL_SOCKET, SO_PRIORITY, original_.priority, applied_.priority);
    const auto lowat = change(fd, IPPROTO_TCP, TCP_NOTSENT_LOWAT, original_.lowat, applied_.lowat);
    const auto rate = change(fd, SOL_SOCKET, SO_MAX_PACING_RATE, original_.rate, applied_.rate);
    return rate ? rate : lowat ? lowat : priority;
}

Error SocketPolicy::remove_locked() noexcept {
    if (!active_) return removal_error_;
    active_ = false;
    if (applied_.rate != original_.rate || applied_.lowat != original_.lowat
            || applied_.priority != original_.priority) {
        auto pinned = pin(fd_, cookie_);
        removal_error_ = pinned ? restore(pinned->get()) : pinned.error();
    }
    release(cookie_);
    return removal_error_;
}

} // namespace flowcore::v2::pacing
