#pragma once
#include <atomic>
#include <string>
#include <string_view>
#include <stdexcept>
#include "flowcore/intent_v2.hpp"
namespace flowcore::v2::detail {
std::string milliseconds(std::uint32_t microseconds);
std::uint64_t unsigned_field(std::string_view text,std::string_view key);
struct RpcError:std::runtime_error {
    Error code;
    bool retry_registration;
    RpcError(Error value,const char* message,bool retry=false)
        :std::runtime_error(message),code(value),retry_registration(retry) {}
};
class Transport {
    std::string path_,endpoint_;
    int fd_=-1;
public:
    std::atomic<std::uint64_t> connections{0},requests{0},legacy{0};
    explicit Transport(std::string path);
    ~Transport();
    Transport(const Transport&)=delete;
    Transport& operator=(const Transport&)=delete;
    void close() noexcept;
    std::string exchange(std::string data, int passed_fd = -1);
};
}
