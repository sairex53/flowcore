#include "flowcore/policy.hpp"
#include <linux/if_link.h>
#include <linux/rtnetlink.h>
#include <net/if.h>
#include <sys/socket.h>
#include <unistd.h>
#include <array>
#include <cstring>
#include <cstddef>
#include <stdexcept>
namespace flowcore {
LinkStats link_stats(const std::string& interface) {
    unsigned index = if_nametoindex(interface.c_str());
    if (!index) throw std::runtime_error("interface not found");
    struct Socket { int fd; ~Socket() { if (fd >= 0) close(fd); } } socket_fd{socket(AF_NETLINK, SOCK_RAW | SOCK_CLOEXEC, NETLINK_ROUTE)};
    if (socket_fd.fd < 0) throw std::runtime_error("rtnetlink socket failed");
    timeval timeout{1, 0};
    if (setsockopt(socket_fd.fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)))
        throw std::runtime_error("rtnetlink timeout setup failed");
    struct { nlmsghdr header; ifinfomsg info; } request{};
    request.header.nlmsg_len = NLMSG_LENGTH(sizeof(ifinfomsg));
    request.header.nlmsg_type = RTM_GETLINK;
    request.header.nlmsg_flags = NLM_F_REQUEST;
    request.header.nlmsg_seq = 1;
    request.info.ifi_family = AF_UNSPEC;
    request.info.ifi_index = static_cast<int>(index);
    sockaddr_nl kernel{};
    kernel.nl_family = AF_NETLINK;
    if (sendto(socket_fd.fd, &request, request.header.nlmsg_len, 0,
               reinterpret_cast<sockaddr*>(&kernel), sizeof(kernel)) < 0)
        throw std::runtime_error("RTM_GETLINK send failed");
    alignas(nlmsghdr) std::array<char, 65536> buffer{};
    sockaddr_nl sender{};
    iovec payload{buffer.data(), buffer.size()};
    msghdr message{};
    message.msg_name = &sender;
    message.msg_namelen = sizeof(sender);
    message.msg_iov = &payload;
    message.msg_iovlen = 1;
    int remaining = recvmsg(socket_fd.fd, &message, 0);
    if (remaining <= 0 || (message.msg_flags & MSG_TRUNC) ||
        message.msg_namelen < sizeof(sender) || sender.nl_family != AF_NETLINK || sender.nl_pid != 0)
        throw std::runtime_error("invalid RTM_GETLINK reply");
    constexpr size_t required_stats = offsetof(rtnl_link_stats64, rx_dropped) +
                                      sizeof(rtnl_link_stats64::rx_dropped);
    for (auto* header = reinterpret_cast<nlmsghdr*>(buffer.data()); NLMSG_OK(header, remaining); header = NLMSG_NEXT(header, remaining)) {
        if (header->nlmsg_seq != 1) continue;
        if (header->nlmsg_type == NLMSG_ERROR) throw std::runtime_error("RTM_GETLINK kernel error");
        if (header->nlmsg_type != RTM_NEWLINK || header->nlmsg_len < NLMSG_LENGTH(sizeof(ifinfomsg))) continue;
        auto* info = static_cast<ifinfomsg*>(NLMSG_DATA(header));
        if (info->ifi_index != static_cast<int>(index)) continue;
        int size = IFLA_PAYLOAD(header);
        for (auto* attr = IFLA_RTA(info); RTA_OK(attr, size); attr = RTA_NEXT(attr, size)) {
            if (attr->rta_type == IFLA_STATS64 && RTA_PAYLOAD(attr) >= required_stats) {
                rtnl_link_stats64 stats{};
                std::memcpy(&stats, RTA_DATA(attr), required_stats);
                return {stats.tx_bytes, stats.tx_dropped, stats.rx_bytes, stats.rx_dropped,
                        stats.tx_packets, stats.rx_packets, stats.tx_errors, stats.rx_errors};
            }
        }
    }
    throw std::runtime_error("interface has no IFLA_STATS64 response");
}
}
