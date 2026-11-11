#include "flowcore/udp_intent.hpp"
#include "flowcore/deadline.hpp"

#include <algorithm>
#include <atomic>
#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <system_error>
#include <utility>
#include <vector>
#include <netinet/in.h>
#include <linux/udp.h>
#include <sys/uio.h>
#include <unistd.h>
#include <linux/errqueue.h>
#include <linux/net_tstamp.h>

#ifndef SO_MARK
#define SO_MARK 36
#endif
#ifndef SO_PRIORITY
#define SO_PRIORITY 12
#endif
#ifndef SO_TIMESTAMPING_NEW
#define SO_TIMESTAMPING_NEW 65
#endif
#ifndef SCM_TS_OPT_ID
#define SCM_TS_OPT_ID 81
#endif
#ifndef SO_TXTIME
#define SO_TXTIME 61
#endif

namespace flowcore::udp {
namespace {
constexpr std::uint32_t tx_software = 1U << 1;
constexpr std::uint32_t tx_sched = 1U << 8;
constexpr std::uint32_t timestamp_software = 1U << 4;
constexpr std::uint32_t timestamp_opt_id = 1U << 7;
constexpr std::uint32_t timestamp_opt_tsonly = 1U << 11;
constexpr int timestamp_origin = 4;
constexpr std::uint64_t ns_per_second = 1'000'000'000ULL;
constexpr std::size_t max_payload = 65'507;
constexpr std::size_t batch_limit = 64;

using v2::deadline::monotonic_ns;

void append_control(msghdr& header, std::size_t& offset, int level, int type,
                    const void* data, std::size_t size) {
    auto* bytes = static_cast<std::byte*>(header.msg_control);
    if (!bytes) throw std::logic_error("ancillary buffer is missing");
    const auto space = CMSG_SPACE(size);
    if (offset + space > 256) throw std::length_error("UDP ancillary data exceeds fixed bound");
    auto* control = reinterpret_cast<cmsghdr*>(bytes + offset);
    control->cmsg_level = level;
    control->cmsg_type = type;
    control->cmsg_len = CMSG_LEN(size);
    std::memcpy(CMSG_DATA(control), data, size);
    offset += space;
    header.msg_controllen = offset;
}

bool valid_destination(int family, const sockaddr* address, socklen_t length) noexcept {
    if (!address) return length == 0;
    if (length < sizeof(address->sa_family)) return false;
    if ((family == AF_INET && address->sa_family != AF_INET) ||
        (family == AF_INET6 && address->sa_family != AF_INET6))
        return false;
    return (family == AF_INET && length >= sizeof(sockaddr_in)) ||
           (family == AF_INET6 && length >= sizeof(sockaddr_in6));
}

std::uint32_t take_id(std::atomic<std::uint32_t>& next, std::uint64_t requested) noexcept {
    if (requested) return static_cast<std::uint32_t>(requested);
    auto id=next.fetch_add(1,std::memory_order_relaxed);
    return id ? id : next.fetch_add(1,std::memory_order_relaxed);
}

void append_message_controls(msghdr& header, std::array<std::byte, 256>& controls,
                             std::size_t& offset, int family, MessageClass traffic_class,
                             std::uint32_t id, bool timestamps) {
    header.msg_control = controls.data();
    int priority = traffic_class == MessageClass::Urgent ? 6 : 0;
    const int dscp = traffic_class == MessageClass::Urgent ? (46 << 2) :
                     traffic_class == MessageClass::Background ? (8 << 2) : 0;
    if (family == AF_INET)
        append_control(header, offset, IPPROTO_IP, IP_TOS, &dscp, sizeof(dscp));
    else
        append_control(header, offset, IPPROTO_IPV6, IPV6_TCLASS, &dscp, sizeof(dscp));
    append_control(header, offset, SOL_SOCKET, SO_PRIORITY, &priority, sizeof(priority));
    if (timestamps) {
        const std::uint32_t requested = tx_software | tx_sched;
        append_control(header, offset, SOL_SOCKET, SO_TIMESTAMPING_NEW, &requested, sizeof(requested));
        append_control(header, offset, SOL_SOCKET, SCM_TS_OPT_ID, &id, sizeof(id));
    }
    header.msg_control = controls.data();
    header.msg_controllen = offset;
}

struct Prepared {
    MessageClass effective = MessageClass::Standard;
    SendStatus status = SendStatus::Sent;
    bool deadline_missed = false;
    bool urgent_token = false;
    std::uint32_t id = 0;
};
}

struct UrgencyBudget::State {
    State(std::uint32_t rate, std::uint32_t burst_value, std::uint64_t expiry)
        : rate_pps(rate), burst(burst_value), tokens(static_cast<double>(burst_value)), last(std::chrono::steady_clock::now()), expires_ns(expiry) {}
    std::uint32_t rate_pps;
    std::uint32_t burst;
    std::mutex mutex;
    double tokens;
    std::chrono::steady_clock::time_point last;
    const pid_t owner=getpid();
    std::atomic<std::uint64_t> expires_ns;
    std::atomic<std::uint64_t> quota_rejections{0};
    std::atomic<bool> enabled{true};
};

UrgencyBudget::UrgencyBudget(std::uint32_t rate, std::uint32_t burst, std::uint64_t expiry)
    : state_(std::make_unique<State>(rate, burst, expiry)) {}
UrgencyBudget::~UrgencyBudget() = default;
std::shared_ptr<UrgencyBudget> UrgencyBudget::create(std::uint32_t rate, std::uint32_t burst, std::uint64_t expiry) {
    if (!rate || !burst) throw std::invalid_argument("invalid urgent message budget");
    return std::shared_ptr<UrgencyBudget>(new UrgencyBudget(rate, burst, expiry));
}
bool UrgencyBudget::reserve() noexcept {
    if (!active()) return false;
    std::lock_guard lock(state_->mutex);
    if (!state_->enabled.load(std::memory_order_relaxed)) return false;
    const auto now = std::chrono::steady_clock::now();
    const auto elapsed = std::chrono::duration<double>(now - state_->last).count();
    state_->tokens = std::min<double>(state_->burst, state_->tokens + elapsed * state_->rate_pps);
    state_->last = now;
    if (state_->tokens < 1.0) {
        state_->quota_rejections.fetch_add(1,std::memory_order_relaxed);
        return false;
    }
    state_->tokens -= 1.0;
    return true;
}
void UrgencyBudget::refund() noexcept {
    std::lock_guard lock(state_->mutex);
    state_->tokens = std::min<double>(state_->burst, state_->tokens + 1.0);
}
void UrgencyBudget::revoke() noexcept { state_->enabled.store(false, std::memory_order_release); }
void UrgencyBudget::renew(std::uint64_t expires_ns) noexcept { state_->expires_ns=expires_ns; }
bool UrgencyBudget::active() const noexcept {
    return state_->enabled.load(std::memory_order_acquire) && state_->owner==getpid() &&
           monotonic_ns()<state_->expires_ns.load(std::memory_order_acquire);
}
std::uint64_t UrgencyBudget::quota_rejections() const noexcept {
    return state_->quota_rejections.load(std::memory_order_relaxed);
}

struct MessageSender::State {
    explicit State(std::shared_ptr<UrgencyBudget> shared) : budget(std::move(shared)) {}
    std::shared_ptr<UrgencyBudget> budget;
    std::atomic<std::uint32_t> next_id{1};
    int old_timestamp_flags = 0;
    int installed_timestamp_flags = 0;
    bool restore_timestamp_flags = false;
    std::uint32_t maximum_deadline_ms = 0;
};

MessageSender::MessageSender(int fd, std::uint32_t required_mark, std::uint32_t maximum_deadline_ms,
                             std::shared_ptr<UrgencyBudget> budget, bool enable_timestamps) {
    if (fd < 0 || !required_mark || !maximum_deadline_ms || !budget || !budget->active())
        throw std::invalid_argument("invalid authorized UDP sender parameters");
    struct SocketCopy {
        int fd;
        ~SocketCopy() { if (fd >= 0) ::close(fd); }
    } copy{fcntl(fd, F_DUPFD_CLOEXEC, 0)};
    if (copy.fd < 0) throw std::system_error(errno, std::generic_category(), "duplicate UDP socket");
    fd=copy.fd;
    int type = 0, protocol = 0, mark = 0;
    socklen_t length = sizeof(int);
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &length) || type != SOCK_DGRAM)
        throw std::invalid_argument("per-message intents require a UDP socket");
    length = sizeof(int);
    if (getsockopt(fd, SOL_SOCKET, SO_PROTOCOL, &protocol, &length) || protocol != IPPROTO_UDP)
        throw std::invalid_argument("per-message intents require UDP protocol");
    length = sizeof(int);
    if (getsockopt(fd, SOL_SOCKET, SO_MARK, &mark, &length) || static_cast<std::uint32_t>(mark) != required_mark)
        throw std::system_error(EPERM, std::generic_category(),
                                "UDP socket lacks the operator-authorized socket mark");
    length = sizeof(int);
    if (getsockopt(fd, SOL_SOCKET, SO_DOMAIN, &family_, &length) ||
        (family_ != AF_INET && family_ != AF_INET6))
        throw std::invalid_argument("UDP socket address family must be IPv4 or IPv6");
    for (const auto option : {UDP_CORK, UDP_SEGMENT}) {
        int value=0;
        length=sizeof(value);
        if (!getsockopt(fd, IPPROTO_UDP, option, &value, &length) && value)
            throw std::invalid_argument("UDP corking and segmentation are incompatible with message intents");
    }
    state_ = std::make_unique<State>(std::move(budget));
    state_->maximum_deadline_ms = maximum_deadline_ms;
    if (enable_timestamps) {
        socklen_t size = sizeof(int);
        int previous = 0;
        if (getsockopt(fd, SOL_SOCKET, SO_TIMESTAMPING_NEW, &previous, &size)) {
            const int error = errno;
            throw std::system_error(error, std::generic_category(), "read socket timestamping flags");
        }
        const int flags = previous | static_cast<int>(timestamp_software | timestamp_opt_id | timestamp_opt_tsonly);
        if (setsockopt(fd, SOL_SOCKET, SO_TIMESTAMPING_NEW, &flags, sizeof(flags))) {
            const int error = errno;
            throw std::system_error(error, std::generic_category(), "enable software transmit timestamping");
        }
        state_->old_timestamp_flags = previous;
        state_->installed_timestamp_flags = flags;
        state_->restore_timestamp_flags = true;
        timestamping_enabled_ = true;
    }
    fd_=std::exchange(copy.fd,-1);
}

MessageSender::MessageSender(MessageSender&& other) noexcept
    : state_(std::move(other.state_)), fd_(std::exchange(other.fd_, -1)),
      family_(std::exchange(other.family_, AF_UNSPEC)),
      timestamping_enabled_(std::exchange(other.timestamping_enabled_, false)) {}

MessageSender& MessageSender::operator=(MessageSender&& other) noexcept {
    if (this == &other) return *this;
    MessageSender temporary(std::move(other));
    std::swap(state_, temporary.state_);
    std::swap(fd_, temporary.fd_);
    std::swap(family_, temporary.family_);
    std::swap(timestamping_enabled_, temporary.timestamping_enabled_);
    return *this;
}

std::uint64_t MessageSender::urgency_quota_rejections() const noexcept {
    return state_ ? state_->budget->quota_rejections() : 0;
}

MessageSender::~MessageSender() {
    if (fd_ >= 0) {
        if (state_ && state_->restore_timestamp_flags) {
            int current = 0;
            socklen_t length = sizeof(current);
            if (!getsockopt(fd_, SOL_SOCKET, SO_TIMESTAMPING_NEW, &current, &length) &&
                current == state_->installed_timestamp_flags)
                static_cast<void>(setsockopt(fd_, SOL_SOCKET, SO_TIMESTAMPING_NEW,
                                             &state_->old_timestamp_flags,
                                             sizeof(state_->old_timestamp_flags)));
        }
        ::close(fd_);
    }
}

SendResult MessageSender::send_message(std::span<const std::byte> payload, MessageIntent intent,
                                       const sockaddr* destination, socklen_t destination_length,
                                       int flags) {
    const Message message{payload, intent, destination, destination_length};
    SendResult result;
    send_into(std::span(&message, 1), std::span(&result, 1), flags);
    return result;
}

SendResult MessageSender::send_urgent(std::span<const std::byte> payload, std::uint64_t deadline,
                                      std::uint64_t message_id, const sockaddr* destination,
                                      socklen_t destination_length, int flags) {
    return send_message(payload, {MessageClass::Urgent, deadline, ExpirationPolicy::Drop, message_id},
                        destination, destination_length, flags);
}
SendResult MessageSender::send_background(std::span<const std::byte> payload, std::uint64_t message_id,
                                          const sockaddr* destination, socklen_t destination_length,
                                          int flags) {
    return send_message(payload, {MessageClass::Background, 0, ExpirationPolicy::DemoteToBackground, message_id},
                        destination, destination_length, flags);
}
SendResult MessageSender::send_with_deadline(std::span<const std::byte> payload, std::uint64_t deadline,
                                             ExpirationPolicy expiration, std::uint64_t message_id,
                                             const sockaddr* destination, socklen_t destination_length,
                                             int flags) {
    if (!deadline)
        return {.status=SendStatus::InvalidDeadline,.message_id=message_id,.error_number=EINVAL};
    return send_message(payload, {MessageClass::Urgent, deadline, expiration, message_id},
                        destination, destination_length, flags);
}

std::vector<SendResult> MessageSender::send_batch(std::span<const Message> input, int flags) {
    if (input.size() > 4096) return {};
    std::vector<SendResult> results(input.size());
    send_into(input, results, flags);
    return results;
}

bool MessageSender::send_batch_into(std::span<const Message> input, std::span<SendResult> results, int flags) {
    if (input.size() != results.size() || input.size() > 4096) return false;
    send_into(input, results, flags);
    return true;
}

void MessageSender::send_into(std::span<const Message> input, std::span<SendResult> results, int flags) {
    const int error = (flags & ~(MSG_DONTWAIT | MSG_NOSIGNAL)) ? EINVAL :
        (!state_ || fd_ < 0) ? EBADF : !state_->budget->active() ? EPERM : 0;
    if (error) {
        for (std::size_t i=0; i<input.size(); ++i)
            results[i]={.status=SendStatus::LocalError,.message_id=input[i].intent.message_id,.error_number=error};
        return;
    }
    std::size_t base = 0;
    while (base < input.size()) {
        const auto count = std::min(batch_limit, input.size() - base);
        std::array<mmsghdr, batch_limit> messages{};
        std::array<iovec, batch_limit> vectors{};
        alignas(cmsghdr) std::array<std::array<std::byte, 256>, batch_limit> controls;
        std::array<Prepared, batch_limit> prepared{};
        std::array<std::size_t, batch_limit> active_indices{};
        std::size_t active = 0;
        for (std::size_t offset = 0; offset < count; ++offset) {
            const auto input_index = base + offset;
            const auto& request = input[input_index];
            auto& result = results[input_index];
            result.message_id = request.intent.message_id;
            if (request.payload.size() > max_payload ||
                (request.destination && !valid_destination(family_, request.destination, request.destination_length)) ||
                (!request.destination && request.destination_length)) {
                result.status = SendStatus::LocalError;
                result.error_number = EMSGSIZE;
                continue;
            }
            if (request.intent.traffic_class != MessageClass::Standard &&
                request.intent.traffic_class != MessageClass::Urgent &&
                request.intent.traffic_class != MessageClass::Background) {
                result.status = SendStatus::LocalError;
                result.error_number = EINVAL;
                continue;
            }
            if (request.intent.expiration != ExpirationPolicy::Drop &&
                request.intent.expiration != ExpirationPolicy::DemoteToBackground) {
                result.status=SendStatus::LocalError;
                result.error_number=EINVAL;
                continue;
            }
            auto item = Prepared{};
            // Caller-supplied IDs avoid a hot-path lock. Auto IDs use a relaxed atomic;
            // uniqueness is local to this sender and no ordering guarantee is implied.
            item.id = take_id(state_->next_id, request.intent.message_id);
            result.message_id = request.intent.message_id ? request.intent.message_id : item.id;
            item.effective = request.intent.traffic_class;
            const auto now = monotonic_ns();
            if (request.intent.deadline_monotonic_ns) {
                const auto max_delta = static_cast<std::uint64_t>(state_->maximum_deadline_ms) * 1'000'000ULL;
                if (request.intent.deadline_monotonic_ns <= now) {
                    if (request.intent.expiration == ExpirationPolicy::Drop) {
                        result.status = SendStatus::Expired;
                        result.deadline_missed = true;
                        continue;
                    }
                    item.effective = MessageClass::Background;
                    item.deadline_missed = true;
                } else if (request.intent.deadline_monotonic_ns - now > max_delta) {
                    result.status = SendStatus::InvalidDeadline;
                    result.error_number = EINVAL;
                    continue;
                }
            }
            if (item.effective == MessageClass::Urgent) {
                if (!state_->budget->reserve()) {
                    if (request.intent.expiration == ExpirationPolicy::DemoteToBackground) {
                        item.effective = MessageClass::Background;
                        item.status = SendStatus::BudgetExhaustedDemoted;
                    } else {
                        result.status = SendStatus::BudgetExhausted;
                        result.error_number = EAGAIN;
                        continue;
                    }
                } else {
                    item.urgent_token = true;
                }
            }
            const auto timestamp_id = static_cast<std::uint32_t>(item.id);
            result.message_id = request.intent.message_id ? request.intent.message_id : timestamp_id;
            auto& header = messages[active].msg_hdr;
            header.msg_name = const_cast<sockaddr*>(request.destination);
            header.msg_namelen = request.destination ? request.destination_length : 0;
            vectors[active].iov_base = const_cast<std::byte*>(request.payload.data());
            vectors[active].iov_len = request.payload.size();
            header.msg_iov = &vectors[active];
            header.msg_iovlen = 1;
            std::size_t control_offset = 0;
            try {
                controls[active].fill(std::byte{});
                append_message_controls(header, controls[active], control_offset, family_,
                                        item.effective, timestamp_id, timestamping_enabled_);
            } catch (...) {
                if (item.urgent_token) state_->budget->refund();
                result.status = SendStatus::LocalError;
                result.error_number = EINVAL;
                continue;
            }
            prepared[active] = item;
            active_indices[active] = input_index;
            ++active;
        }
        if (active) {
            int sent=-1;
            if (!state_->budget->active()) {
                errno=EPERM;
            } else if (input.size()==1) {
                const auto bytes=sendmsg(fd_, &messages[0].msg_hdr, flags);
                if (bytes>=0) {
                    messages[0].msg_len=static_cast<unsigned>(bytes);
                    sent=1;
                }
            } else {
                sent=sendmmsg(fd_, messages.data(), static_cast<unsigned>(active), flags);
            }
            const int send_error=errno;
            const auto completed_at = monotonic_ns();
            if (sent < 0) {
                for (std::size_t i = 0; i < active; ++i) {
                    auto& result = results[active_indices[i]];
                    result.status = i==0 ? SendStatus::LocalError : SendStatus::NotAttempted;
                    result.error_number = i==0 ? send_error : 0;
                    result.deadline_missed = prepared[i].deadline_missed;
                    if (prepared[i].urgent_token) state_->budget->refund();
                }
            } else {
                for (std::size_t i = 0; i < active; ++i) {
                    auto& result = results[active_indices[i]];
                    if (i < static_cast<std::size_t>(sent)) {
                        result.status = prepared[i].status;
                        result.applied_class = prepared[i].effective;
                        result.bytes = messages[i].msg_len;
                        result.submitted_monotonic_ns = completed_at;
                        const auto expiry=input[active_indices[i]].intent.deadline_monotonic_ns;
                        result.deadline_missed=prepared[i].deadline_missed || (expiry && completed_at>expiry);
                    } else {
                        // A short sendmmsg result does not retain the error for the first unsent datagram.
                        result.status = SendStatus::NotAttempted;
                        result.deadline_missed = prepared[i].deadline_missed;
                        if (prepared[i].urgent_token) state_->budget->refund();
                    }
                }
            }
        }
        base += count;
    }
}

std::vector<TxTimestamp> MessageSender::drain_tx_timestamps(std::size_t maximum) noexcept {
    std::vector<TxTimestamp> output;
    if (fd_ < 0 || !timestamping_enabled_ || maximum > 4096) return output;
    try {
        output.reserve(maximum);
        for (std::size_t i = 0; i < maximum; ++i) {
            std::array<std::byte, 1> data{};
            alignas(cmsghdr) std::array<std::byte, 256> ancillary{};
            iovec vector{data.data(), data.size()};
            msghdr message{};
            message.msg_iov = &vector;
            message.msg_iovlen = 1;
            message.msg_control = ancillary.data();
            message.msg_controllen = ancillary.size();
            const auto received = recvmsg(fd_, &message, MSG_ERRQUEUE | MSG_DONTWAIT);
            if (received < 0) {
                if (errno == EAGAIN || errno == EWOULDBLOCK) break;
                break;
            }
            if (message.msg_flags & MSG_CTRUNC) continue;
            std::uint32_t id = 0;
            std::optional<std::uint32_t> stage;
            std::uint64_t stamp = 0;
            for (auto* control = CMSG_FIRSTHDR(&message); control; control = CMSG_NXTHDR(&message, control)) {
                if (control->cmsg_level == SOL_SOCKET && control->cmsg_type == SO_TIMESTAMPING_NEW &&
                    control->cmsg_len >= CMSG_LEN(16)) {
                    std::int64_t seconds = 0, nanos = 0;
                    std::memcpy(&seconds, CMSG_DATA(control), sizeof(seconds));
                    std::memcpy(&nanos, reinterpret_cast<std::byte*>(CMSG_DATA(control)) + sizeof(seconds), sizeof(nanos));
                    if (seconds >= 0 && nanos >= 0 && nanos < static_cast<std::int64_t>(ns_per_second))
                        stamp = static_cast<std::uint64_t>(seconds) * ns_per_second + static_cast<std::uint64_t>(nanos);
                }
                if ((control->cmsg_level == IPPROTO_IP && control->cmsg_type == IP_RECVERR) ||
                    (control->cmsg_level == IPPROTO_IPV6 && control->cmsg_type == IPV6_RECVERR)) {
                    if (control->cmsg_len >= CMSG_LEN(sizeof(sock_extended_err))) {
                        struct sock_extended_err error{};
                        std::memcpy(&error, CMSG_DATA(control), sizeof(error));
                        if (error.ee_origin == timestamp_origin) {
                            id = error.ee_data;
                            stage = error.ee_info;
                        }
                    }
                }
            }
            if (stamp && stage && (*stage == SCM_TSTAMP_SND || *stage == SCM_TSTAMP_SCHED))
                output.push_back({id, *stage == SCM_TSTAMP_SCHED ? TimestampStage::Scheduling : TimestampStage::SoftwareTransmit, stamp});
        }
    } catch (...) {}
    return output;
}

Capabilities MessageSender::detect_capabilities() noexcept {
    Capabilities result{};
#ifdef __linux__
    result.sendmmsg = sendmmsg(-1, nullptr, 1, 0) == -1 && errno == EBADF;
    auto probe = [](int family, int tos_level, int tos_type) noexcept {
        const int fd = ::socket(family, SOCK_DGRAM | SOCK_CLOEXEC, IPPROTO_UDP);
        if (fd < 0) return false;
        std::array<std::byte, 1> payload{std::byte{0}};
        alignas(cmsghdr) std::array<std::byte, 64> control{};
        msghdr message{};
        iovec vector{payload.data(), payload.size()};
        message.msg_iov = &vector;
        message.msg_iovlen = 1;
        message.msg_control = control.data();
        message.msg_controllen = control.size();
        int priority = 6;
        int tos = 0xb8;
        auto* first = CMSG_FIRSTHDR(&message);
        first->cmsg_level = SOL_SOCKET;
        first->cmsg_type = SO_PRIORITY;
        first->cmsg_len = CMSG_LEN(sizeof(priority));
        std::memcpy(CMSG_DATA(first), &priority, sizeof(priority));
        auto* second = CMSG_NXTHDR(&message, first);
        second->cmsg_level = tos_level;
        second->cmsg_type = tos_type;
        second->cmsg_len = CMSG_LEN(sizeof(tos));
        std::memcpy(CMSG_DATA(second), &tos, sizeof(tos));
        message.msg_controllen = CMSG_SPACE(sizeof(priority)) + CMSG_SPACE(sizeof(tos));
        sockaddr_storage destination{};
        socklen_t destination_length = 0;
        if (family == AF_INET) {
            auto* address = reinterpret_cast<sockaddr_in*>(&destination);
            address->sin_family = AF_INET;
            address->sin_addr.s_addr = htonl(INADDR_LOOPBACK);
            address->sin_port = 0;
            destination_length = sizeof(*address);
        } else {
            auto* address = reinterpret_cast<sockaddr_in6*>(&destination);
            address->sin6_family = AF_INET6;
            address->sin6_addr = in6addr_loopback;
            address->sin6_port = 0;
            destination_length = sizeof(*address);
        }
        if (bind(fd,reinterpret_cast<sockaddr*>(&destination),destination_length) ||
            getsockname(fd,reinterpret_cast<sockaddr*>(&destination),&destination_length)) {
            ::close(fd);
            return false;
        }
        message.msg_name = &destination;
        message.msg_namelen = destination_length;
        const auto sent = sendmsg(fd, &message, MSG_DONTWAIT);
        ::close(fd);
        return sent == 1;
    };
    result.linux_message_priority = probe(AF_INET, IPPROTO_IP, IP_TOS);
    result.ipv4_tos_ancillary = result.linux_message_priority;
    result.ipv6_tclass_ancillary = probe(AF_INET6, IPPROTO_IPV6, IPV6_TCLASS);
    int fd = ::socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, IPPROTO_UDP);
    if (fd >= 0) {
        struct sock_txtime config{};
        config.clockid = CLOCK_MONOTONIC;
        config.flags = SOF_TXTIME_REPORT_ERRORS;
        result.txtime_socket_option = setsockopt(fd, SOL_SOCKET, SO_TXTIME, &config, sizeof(config)) == 0;
        int flags = static_cast<int>(timestamp_software | timestamp_opt_id | timestamp_opt_tsonly);
        result.software_tx_timestamping = setsockopt(fd, SOL_SOCKET, SO_TIMESTAMPING_NEW, &flags, sizeof(flags)) == 0;
        ::close(fd);
    }
#endif
    return result;
}
}
