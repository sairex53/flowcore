#include "flowcore/intent_v2.hpp"
#include "flowcore/udp_intent.hpp"

#include <cerrno>
#include <chrono>
#include <memory>

namespace {
struct Sender {
    flowcore::v2::Service service;
    std::unique_ptr<flowcore::udp::MessageSender> socket;
    Sender(const char* path, int fd)
        : service(path, [] {
              flowcore::v2::Intent intent;
              intent.service="udp_lab";
              intent.priority=flowcore::v2::Priority::Critical;
              return intent;
          }()) {
        if (!service.wait_ready(std::chrono::seconds(3)))
            throw std::runtime_error("UDP lab registration failed");
        socket=service.udp_message_sender(fd);
    }
};
}

extern "C" void* flowcore_udp_lab_open(const char* path, int fd) noexcept {
    try { return new Sender(path,fd); }
    catch (...) { return nullptr; }
}

extern "C" int flowcore_udp_lab_send(void* handle, const void* data, std::size_t size,
                                      std::uint64_t deadline, std::uint64_t id) noexcept {
    try {
        auto& sender=*static_cast<Sender*>(handle)->socket;
        const auto payload=std::span(static_cast<const std::byte*>(data),size);
        auto result=deadline ? sender.send_urgent(payload,deadline,id) :
                               sender.send_message(payload,{.message_id=id});
        if (result.status!=flowcore::udp::SendStatus::Sent)
            return -(result.error_number ? result.error_number : ETIMEDOUT);
        return static_cast<int>(result.bytes);
    } catch (...) { return -EIO; }
}

extern "C" void flowcore_udp_lab_close(void* handle) noexcept {
    delete static_cast<Sender*>(handle);
}
