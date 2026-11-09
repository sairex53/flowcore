#include "flowcore/intent_v2.hpp"
#include "flowcore/udp_intent.hpp"

#include <array>
#include <charconv>
#include <chrono>
#include <cstddef>
#include <iostream>
#include <span>
#include <string_view>
#include <sys/socket.h>

int main(int argc, char** argv) {
    if (argc != 3) {
        std::cerr << "usage: flowcore-udp-intent-demo DAEMON_SOCKET PREAUTHORIZED_UDP_FD\n";
        return 2;
    }
    int fd = -1;
    const std::string_view number(argv[2]);
    const auto parsed = std::from_chars(number.data(), number.data()+number.size(), fd);
    if (parsed.ec != std::errc{} || parsed.ptr != number.data()+number.size() || fd < 0) return 2;

    flowcore::v2::Intent intent;
    intent.service = "udp_demo";
    intent.priority = flowcore::v2::Priority::Critical;
    flowcore::v2::Service service(argv[1], intent);
    if (!service.wait_ready(std::chrono::seconds(3))) {
        std::cerr << "FLOWCORE is unavailable; application must use its ordinary UDP fallback\n";
        return 3;
    }
    try {
        auto sender = service.udp_message_sender(fd);
        constexpr std::array payload{std::byte{'p'}, std::byte{'i'}, std::byte{'n'}, std::byte{'g'}};
        const auto deadline = static_cast<std::uint64_t>(std::chrono::duration_cast<std::chrono::nanoseconds>(
            std::chrono::steady_clock::now().time_since_epoch()).count()) + 20'000'000;
        const auto result = sender->send_with_deadline(payload, deadline);
        std::cout << "status=" << static_cast<unsigned>(result.status)
                  << " bytes=" << result.bytes
                  << " class=" << static_cast<unsigned>(result.applied_class)
                  << " deadline_missed=" << result.deadline_missed << '\n';
        return result.status == flowcore::udp::SendStatus::Sent ? 0 : 1;
    } catch (const std::exception& error) {
        std::cerr << "UDP intent unavailable: " << error.what() << '\n';
        return 4;
    }
}
