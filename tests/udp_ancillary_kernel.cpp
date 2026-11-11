#include <arpa/inet.h>
#include <algorithm>
#include <array>
#include <cstddef>
#include <cstring>
#include <iostream>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>
#include <netinet/in.h>

namespace {
void require(bool value,const char* message) {
    if (!value) throw std::runtime_error(message);
}
struct Received { char payload=0; int traffic_class=-1; };

void run_family(int family) {
    const int receiver=::socket(family,SOCK_DGRAM|SOCK_CLOEXEC,IPPROTO_UDP);
    require(receiver>=0,"receiver socket failed");
    const int one=1;
    timeval timeout{2,0};
    require(setsockopt(receiver,SOL_SOCKET,SO_RCVTIMEO,&timeout,sizeof(timeout))==0,"receive timeout failed");
    sockaddr_storage address{};
    socklen_t address_length=0;
    int recv_level=0,recv_option=0,tx_level=0,tx_option=0;
    if (family==AF_INET) {
        auto* ipv4=reinterpret_cast<sockaddr_in*>(&address);
        ipv4->sin_family=AF_INET;
        ipv4->sin_addr.s_addr=htonl(INADDR_LOOPBACK);
        address_length=sizeof(*ipv4);
        recv_level=IPPROTO_IP;recv_option=IP_RECVTOS;tx_level=IPPROTO_IP;tx_option=IP_TOS;
    } else {
        auto* ipv6=reinterpret_cast<sockaddr_in6*>(&address);
        ipv6->sin6_family=AF_INET6;
        ipv6->sin6_addr=in6addr_loopback;
        address_length=sizeof(*ipv6);
        recv_level=IPPROTO_IPV6;recv_option=IPV6_RECVTCLASS;tx_level=IPPROTO_IPV6;tx_option=IPV6_TCLASS;
    }
    require(setsockopt(receiver,recv_level,recv_option,&one,sizeof(one))==0,"receive traffic-class option failed");
    require(bind(receiver,reinterpret_cast<sockaddr*>(&address),address_length)==0,"receiver bind failed");
    require(getsockname(receiver,reinterpret_cast<sockaddr*>(&address),&address_length)==0,"receiver address failed");
    const int sender=::socket(family,SOCK_DGRAM|SOCK_CLOEXEC,IPPROTO_UDP);
    require(sender>=0,"sender socket failed");
    std::array<mmsghdr,3> packets{};
    std::array<iovec,3> vectors{};
    std::array<char,3> payloads{'s','u','b'};
    std::array<std::array<std::byte,128>,3> controls{};
    const std::array<int,3> classes{0,46<<2,8<<2};
    const std::array<int,3> priorities{0,6,1};
    for (std::size_t i=0;i<packets.size();++i) {
        auto& header=packets[i].msg_hdr;
        vectors[i]={&payloads[i],1};
        header.msg_name=&address;
        header.msg_namelen=address_length;
        header.msg_iov=&vectors[i];
        header.msg_iovlen=1;
        header.msg_control=controls[i].data();
        header.msg_controllen=CMSG_SPACE(sizeof(int))*2;
        auto* priority=CMSG_FIRSTHDR(&header);
        priority->cmsg_level=SOL_SOCKET;
        priority->cmsg_type=SO_PRIORITY;
        priority->cmsg_len=CMSG_LEN(sizeof(int));
        std::memcpy(CMSG_DATA(priority),&priorities[i],sizeof(int));
        auto* marking=CMSG_NXTHDR(&header,priority);
        marking->cmsg_level=tx_level;
        marking->cmsg_type=tx_option;
        marking->cmsg_len=CMSG_LEN(sizeof(int));
        std::memcpy(CMSG_DATA(marking),&classes[i],sizeof(int));
    }
    const int sent=sendmmsg(sender,packets.data(),packets.size(),MSG_DONTWAIT);
    require(sent==3,"sendmmsg did not submit every marked datagram");
    std::array<Received,3> received{};
    for (auto& row:received) {
        std::array<char,16> data{};
        alignas(cmsghdr) std::array<std::byte,128> control{};
        iovec vector{data.data(),data.size()};
        msghdr header{};
        header.msg_iov=&vector;header.msg_iovlen=1;
        header.msg_control=control.data();header.msg_controllen=control.size();
        const auto size=recvmsg(receiver,&header,0);
        require(size==1,"failed to receive one loopback datagram");
        row.payload=data[0];
        for (auto* cmsg=CMSG_FIRSTHDR(&header);cmsg;cmsg=CMSG_NXTHDR(&header,cmsg)) {
            if (family==AF_INET && cmsg->cmsg_level==IPPROTO_IP && cmsg->cmsg_type==IP_TOS &&
                cmsg->cmsg_len>=CMSG_LEN(sizeof(unsigned char)))
                row.traffic_class=*reinterpret_cast<unsigned char*>(CMSG_DATA(cmsg));
            if (family==AF_INET6 && cmsg->cmsg_level==IPPROTO_IPV6 && cmsg->cmsg_type==IPV6_TCLASS &&
                cmsg->cmsg_len>=CMSG_LEN(sizeof(int)))
                std::memcpy(&row.traffic_class,CMSG_DATA(cmsg),sizeof(int));
        }
    }
    const std::array<std::pair<char,int>,3> expected{{{'s',0},{'u',46<<2},{'b',8<<2}}};
    for (const auto& [payload,traffic_class]:expected) {
        const auto found=std::find_if(received.begin(),received.end(),[&](const auto& row){return row.payload==payload;});
        require(found!=received.end() && found->traffic_class==traffic_class,
                "per-message DSCP did not survive mixed sendmmsg on loopback");
    }
    int priority=0,traffic_class=0;
    socklen_t option_length=sizeof(int);
    require(getsockopt(sender,SOL_SOCKET,SO_PRIORITY,&priority,&option_length)==0 && priority==0,
            "per-message SO_PRIORITY unexpectedly changed socket default");
    option_length=sizeof(int);
    require(getsockopt(sender,tx_level,tx_option,&traffic_class,&option_length)==0 && traffic_class==0,
            "per-message DSCP unexpectedly changed socket default");
    ::close(sender);::close(receiver);
    std::cout<<(family==AF_INET?"ipv4":"ipv6")<<" mixed per-message markings verified\n";
}
}

int main() {
    try { run_family(AF_INET);run_family(AF_INET6);return 0; }
    catch (const std::exception& error) { std::cerr<<error.what()<<'\n';return 1; }
}
