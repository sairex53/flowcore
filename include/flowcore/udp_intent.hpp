#pragma once

#include <chrono>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <span>
#include <sys/socket.h>
#include <vector>

namespace flowcore::v2 { class Service; }
namespace flowcore::v2::detail { struct Channel; }

namespace flowcore::udp {
enum class MessageClass : std::uint8_t { Standard, Urgent, Background };
enum class ExpirationPolicy : std::uint8_t { Drop, DemoteToBackground };
enum class SendStatus : std::uint8_t {
    Sent, BudgetExhaustedDemoted, Expired, InvalidDeadline, BudgetExhausted, LocalError, NotAttempted
};
enum class TimestampStage : std::uint8_t { Scheduling, SoftwareTransmit };
struct MessageIntent {
    MessageClass traffic_class = MessageClass::Standard;
    std::uint64_t deadline_monotonic_ns = 0;
    ExpirationPolicy expiration = ExpirationPolicy::Drop;
    std::uint64_t message_id = 0;
};
struct Message {
    std::span<const std::byte> payload;
    MessageIntent intent{};
    const sockaddr* destination = nullptr;
    socklen_t destination_length = 0;
};
struct SendResult {
    SendStatus status = SendStatus::NotAttempted;
    MessageClass applied_class = MessageClass::Standard;
    std::size_t bytes = 0;
    std::uint64_t message_id = 0;
    // Time at which sendmmsg returned; local software submission evidence only.
    std::uint64_t submitted_monotonic_ns = 0;
    int error_number = 0;
    bool deadline_missed = false;
};
struct TxTimestamp {
    std::uint32_t message_id = 0;
    TimestampStage stage = TimestampStage::SoftwareTransmit;
    // Linux software TX timestamps are CLOCK_REALTIME based, not monotonic.
    std::uint64_t realtime_ns = 0;
};
struct Capabilities {
    bool linux_message_priority = false;
    bool ipv4_tos_ancillary = false;
    bool ipv6_tclass_ancillary = false;
    bool sendmmsg = false;
    bool txtime_socket_option = false;
    bool software_tx_timestamping = false;
};
class UrgencyBudget {
public:
    ~UrgencyBudget();
    UrgencyBudget(const UrgencyBudget&) = delete;
    UrgencyBudget& operator=(const UrgencyBudget&) = delete;
private:
    friend class flowcore::v2::Service;
    friend class flowcore::v2::detail::Channel;
    friend class MessageSender;
    struct State;
    explicit UrgencyBudget(std::uint32_t rate_pps, std::uint32_t burst, std::uint64_t expires_ns);
    static std::shared_ptr<UrgencyBudget> create(std::uint32_t rate_pps, std::uint32_t burst, std::uint64_t expires_ns);
    bool reserve() noexcept;
    void refund() noexcept;
    void revoke() noexcept;
    void renew(std::uint64_t expires_ns) noexcept;
    bool active() const noexcept;
    std::unique_ptr<State> state_;
};
class MessageSender {
public:
    MessageSender(const MessageSender&) = delete;
    MessageSender& operator=(const MessageSender&) = delete;
    MessageSender(MessageSender&&) noexcept;
    MessageSender& operator=(MessageSender&&) noexcept;
    ~MessageSender();

    SendResult send_message(std::span<const std::byte> payload,
                            MessageIntent intent = {},
                            const sockaddr* destination = nullptr,
                            socklen_t destination_length = 0,
                            int flags = MSG_DONTWAIT);
    SendResult send_urgent(std::span<const std::byte> payload,
                           std::uint64_t deadline_monotonic_ns = 0,
                           std::uint64_t message_id = 0,
                           const sockaddr* destination = nullptr,
                           socklen_t destination_length = 0,
                           int flags = MSG_DONTWAIT);
    SendResult send_background(std::span<const std::byte> payload,
                               std::uint64_t message_id = 0,
                               const sockaddr* destination = nullptr,
                               socklen_t destination_length = 0,
                               int flags = MSG_DONTWAIT);
    SendResult send_with_deadline(std::span<const std::byte> payload,
                                  std::uint64_t deadline_monotonic_ns,
                                  ExpirationPolicy expiration = ExpirationPolicy::DemoteToBackground,
                                  std::uint64_t message_id = 0,
                                  const sockaddr* destination = nullptr,
                                  socklen_t destination_length = 0,
                                  int flags = MSG_DONTWAIT);
    std::vector<SendResult> send_batch(std::span<const Message> messages,
                                       int flags = MSG_DONTWAIT);
    std::vector<TxTimestamp> drain_tx_timestamps(std::size_t maximum = 128) noexcept;
    static Capabilities detect_capabilities() noexcept;
    int family() const noexcept { return family_; }
    bool timestamping_enabled() const noexcept { return timestamping_enabled_; }

private:
    friend class flowcore::v2::Service;
    MessageSender(int fd, std::uint32_t required_mark, std::uint32_t maximum_deadline_ms,
                  std::shared_ptr<UrgencyBudget> budget, bool enable_timestamps);
    void send_into(std::span<const Message>, std::span<SendResult>, int flags);
    struct State;
    std::unique_ptr<State> state_;
    int fd_ = -1;
    int family_ = AF_UNSPEC;
    bool timestamping_enabled_ = false;
};
}
