#include "flowcore/intent_v2.hpp"
#include "flowcore/udp_intent.hpp"

#include <array>
#include <chrono>
#include <cstring>
#include <iostream>
#include <span>
#include <stdexcept>
#include <string>
#include <vector>
#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

using namespace std::chrono_literals;

namespace {
constexpr int mark_option = 36;
constexpr int recv_tclass6 = 66;
constexpr int recv_tos4 = 13;
std::uint64_t now_ns() {
    return static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count());
}
void check(bool condition, const char* message) {
    if (!condition) throw std::runtime_error(message);
}
struct Received { std::string payload; int dscp = -1; };
Received receive_one(int fd, int family) {
    std::array<char, 128> payload{};
    alignas(cmsghdr) std::array<char, 128> control{};
    iovec vector{payload.data(), payload.size()};
    msghdr header{};
    header.msg_iov = &vector;
    header.msg_iovlen = 1;
    header.msg_control = control.data();
    header.msg_controllen = control.size();
    const auto length = recvmsg(fd, &header, 0);
    check(length >= 0, "receive failed");
    Received result;
    result.payload.assign(payload.data(), static_cast<std::size_t>(length));
    for (auto* item = CMSG_FIRSTHDR(&header); item; item = CMSG_NXTHDR(&header, item)) {
        if (family == AF_INET && item->cmsg_level == IPPROTO_IP && item->cmsg_type == IP_TOS &&
            item->cmsg_len >= CMSG_LEN(sizeof(unsigned char))) {
            const auto dscp = *reinterpret_cast<unsigned char*>(CMSG_DATA(item));
            result.dscp = dscp;
        }
        if (family == AF_INET6 && item->cmsg_level == IPPROTO_IPV6 && item->cmsg_type == IPV6_TCLASS &&
            item->cmsg_len >= CMSG_LEN(sizeof(int))) {
            int tclass = 0;
            std::memcpy(&tclass, CMSG_DATA(item), sizeof(tclass));
            result.dscp = tclass;
        }
    }
    return result;
}
void exercise_family(const std::string& path, const char* service_name, int family) {
    flowcore::v2::Intent intent;
    intent.service = service_name;
    intent.priority = flowcore::v2::Priority::Critical;
    flowcore::v2::Service service(path, intent);
    check(service.wait_ready(3s), "daemon registration failed");
    const int receiver = socket(family, SOCK_DGRAM | SOCK_CLOEXEC, IPPROTO_UDP);
    check(receiver >= 0, "receiver socket unavailable");
    const int option = 1;
    const timeval timeout{2,0};
    check(setsockopt(receiver,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout))==0, "receiver timeout setup failed");
    if (family == AF_INET) {
        check(setsockopt(receiver, IPPROTO_IP, recv_tos4, &option, sizeof(option)) == 0, "IP_RECVTOS unavailable");
        sockaddr_in local{};
        local.sin_family = AF_INET;
        local.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        check(bind(receiver, reinterpret_cast<sockaddr*>(&local), sizeof(local)) == 0, "IPv4 bind failed");
    } else {
        if (setsockopt(receiver, IPPROTO_IPV6, recv_tclass6, &option, sizeof(option)) != 0) {
            close(receiver);
            throw std::runtime_error("IPV6_RECVTCLASS unavailable");
        }
        sockaddr_in6 local{};
        local.sin6_family = AF_INET6;
        local.sin6_addr = in6addr_loopback;
        check(bind(receiver, reinterpret_cast<sockaddr*>(&local), sizeof(local)) == 0, "IPv6 bind failed");
    }
    sockaddr_storage destination{};
    socklen_t destination_length = sizeof(destination);
    check(getsockname(receiver, reinterpret_cast<sockaddr*>(&destination), &destination_length) == 0,
          "receiver address query failed");
    const int sender_fd = socket(family, SOCK_DGRAM | SOCK_CLOEXEC, IPPROTO_UDP);
    check(sender_fd >= 0, "sender socket unavailable");
    try {
        auto unexpected=service.udp_message_sender(sender_fd);
        throw std::runtime_error("unmarked socket received a message sender");
    } catch (const std::system_error& error) {
        check(error.code().value()==EPERM, "unmarked socket returned wrong authorization error");
    }
    const int required_mark = 42;
    if (setsockopt(sender_fd, SOL_SOCKET, mark_option, &required_mark, sizeof(required_mark)) != 0) {
        close(sender_fd);
        close(receiver);
        throw std::system_error(errno, std::generic_category(), "set test SO_MARK");
    }
    check(connect(sender_fd, reinterpret_cast<sockaddr*>(&destination), destination_length) == 0,
          "UDP connect failed");
    const auto caps = flowcore::udp::MessageSender::detect_capabilities();
    auto sender = service.udp_message_sender(sender_fd, caps.software_tx_timestamping);
    check(sender->family() == family, "socket family was lost");
    std::array<std::byte, 3> urgent{std::byte{'u'}, std::byte{'r'}, std::byte{'g'}};
    std::array<std::byte, 3> fallback{std::byte{'b'}, std::byte{'g'}, std::byte{'!'}};
    std::array<std::byte, 3> routine{std::byte{'r'}, std::byte{'t'}, std::byte{'n'}};
    auto first = sender->send_urgent(urgent, now_ns() + 50'000'000, 101);
    auto second_wrapper=service.udp_message_sender(sender_fd);
    const auto shared_budget=second_wrapper->send_urgent(urgent,0,107);
    check(shared_budget.status==flowcore::udp::SendStatus::BudgetExhausted, "sender wrappers did not share a budget");
    const auto cork=sender->send_message(routine,{},nullptr,0,MSG_MORE);
    check(cork.status==flowcore::udp::SendStatus::LocalError && cork.error_number==EINVAL,
          "UDP corking flags were accepted");
    std::array<flowcore::udp::Message, 3> batch{{
        {fallback, {flowcore::udp::MessageClass::Urgent, now_ns() + 50'000'000,
                    flowcore::udp::ExpirationPolicy::DemoteToBackground, 102}},
        {urgent, {flowcore::udp::MessageClass::Background, 0,
                  flowcore::udp::ExpirationPolicy::DemoteToBackground, 103}},
        {routine, {flowcore::udp::MessageClass::Standard, 0,
                   flowcore::udp::ExpirationPolicy::Drop, 104}}
    }};
    std::array<flowcore::udp::SendResult,3> batch_results{};
    check(sender->send_batch_into(batch,batch_results), "reusable sendmmsg batch rejected");
    check(!sender->send_batch_into(batch,std::span<flowcore::udp::SendResult>(batch_results.data(),2)),
          "mismatched output span accepted");
    const auto& second = batch_results[0];
    const auto& background_result = batch_results[1];
    const auto& third = batch_results[2];
    auto expired = sender->send_with_deadline(routine, now_ns() - 1,
        flowcore::udp::ExpirationPolicy::Drop, 105);
    auto too_far = sender->send_with_deadline(routine, now_ns() + 5'000'000'000ULL,
        flowcore::udp::ExpirationPolicy::Drop, 106);
    check(first.status == flowcore::udp::SendStatus::Sent &&
          first.applied_class == flowcore::udp::MessageClass::Urgent, "urgent datagram did not retain urgency");
    check(second.status == flowcore::udp::SendStatus::BudgetExhaustedDemoted &&
          second.applied_class == flowcore::udp::MessageClass::Background, "urgency budget did not demote excess");
    check(background_result.status == flowcore::udp::SendStatus::Sent &&
          background_result.applied_class == flowcore::udp::MessageClass::Background, "background batch item failed");
    check(third.status == flowcore::udp::SendStatus::Sent, "ordinary send failed");
    check(expired.status == flowcore::udp::SendStatus::Expired, "expired request was sent");
    check(too_far.status == flowcore::udp::SendStatus::InvalidDeadline, "overlong deadline was accepted");
    const auto one = receive_one(receiver, family);
    const auto two = receive_one(receiver, family);
    const auto three = receive_one(receiver, family);
    const auto four = receive_one(receiver, family);
    check(one.payload == "urg" && one.dscp == (46 << 2), "urgent DSCP/class metadata mismatch");
    check(two.payload == "bg!" && two.dscp == (8 << 2), "background demotion metadata mismatch");
    check(three.payload == "urg" && three.dscp == (8 << 2), "background batch class mismatch");
    check(four.payload == "rtn" && four.dscp == 0, "standard traffic was promoted");
    const auto timestamps = sender->drain_tx_timestamps(32);
    if (caps.software_tx_timestamping) {
        for (unsigned id=101; id<=104; ++id) {
            std::uint64_t scheduled=0, transmitted=0;
            for (const auto& timestamp : timestamps) {
                if (timestamp.message_id!=id) continue;
                if (timestamp.stage==flowcore::udp::TimestampStage::Scheduling) scheduled=timestamp.realtime_ns;
                if (timestamp.stage==flowcore::udp::TimestampStage::SoftwareTransmit) transmitted=timestamp.realtime_ns;
            }
            check(scheduled && transmitted && scheduled<=transmitted, "invalid software TX timestamp pair");
        }
    }
    check(sender->drain_tx_timestamps(32).empty(), "timestamp drain repeated a record");
    std::cout << (family == AF_INET ? "ipv4" : "ipv6") << "_tx_timestamps=" << timestamps.size() << '\n';
    close(sender_fd);
    close(receiver);
}
}

int main(int argc, char** argv) {
    if (argc == 2 && std::string(argv[1]) == "--capabilities") {
        const auto caps = flowcore::udp::MessageSender::detect_capabilities();
        std::cout << "sendmmsg=" << caps.sendmmsg
                  << " priority=" << caps.linux_message_priority
                  << " ipv4_tos=" << caps.ipv4_tos_ancillary
                  << " ipv6_tclass=" << caps.ipv6_tclass_ancillary
                  << " txtime=" << caps.txtime_socket_option
                  << " timestamping=" << caps.software_tx_timestamping << '\n';
        return caps.sendmmsg && caps.linux_message_priority ? 0 : 1;
    }
    if (argc != 2) return 2;
    try {
        exercise_family(argv[1], "udp_fixture4", AF_INET);
        exercise_family(argv[1], "udp_fixture6", AF_INET6);
        return 0;
    } catch (const std::system_error& error) {
        std::cerr << error.what() << '\n';
        return 1;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
