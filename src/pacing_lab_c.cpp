// Private native bridge for the Research Lab workload, not a public ABI.
#include "flowcore/socket_pacing.hpp"

#include <cerrno>
#include <memory>
#include <new>

namespace p = flowcore::v2::pacing;
extern "C" {

int fc_pacing_capabilities(int* out) noexcept {
    auto result = p::probe();
    if (!result) return result.error().value();
    const auto& c = *result;
    out[0] = c.maximum_rate.value();
    out[1] = c.priority.value();
    out[2] = c.mark_read.value();
    out[3] = c.mark_write.value();
    out[4] = c.notsent_lowat.value();
    out[5] = c.congestion.value();
    out[6] = c.tcp_info.value();
    out[7] = c.cookie.value();
    return 0;
}
void* fc_pacing_open(int fd, std::uint64_t rate, std::uint64_t floor, std::uint64_t ceiling,
                     std::uint32_t lowat, int* failure) noexcept {
    try {
        p::Policy policy{};
        policy.service = "research-background";
        policy.minimum_cap = floor;
        policy.maximum_cap = ceiling;
        policy.initial.maximum_rate = rate;
        if (lowat) policy.initial.notsent_lowat = lowat;
        auto attached = p::SocketPolicy::attach(fd, policy);
        if (!attached) {
            *failure = attached.error().value();
            return nullptr;
        }
        return new p::SocketPolicy(std::move(*attached));
    } catch (...) {
        *failure = ENOMEM;
        return nullptr;
    }
}
int fc_pacing_rate(void* handle, std::uint64_t rate) noexcept {
    return static_cast<p::SocketPolicy*>(handle)->set_rate(rate).value();
}
int fc_pacing_close(void* handle) noexcept {
    std::unique_ptr<p::SocketPolicy> managed(static_cast<p::SocketPolicy*>(handle));
    return managed->remove().value();
}
int fc_pacing_cc(int fd, const char* name) noexcept {
    try { return p::select_congestion(fd, name).value(); }
    catch (...) { return EIO; }
}
int fc_pacing_inspect(int fd, std::uint64_t* out) noexcept {
    try {
        auto result = p::inspect(fd);
        if (!result) return result.error().value();
        const auto& v = *result;
        out[0] = v.cookie;
        out[1] = v.maximum_rate;
        out[2] = v.pacing_rate.value_or(UINT64_MAX);
        out[3] = v.delivery_rate.value_or(UINT64_MAX);
        out[4] = v.bytes_acked.value_or(UINT64_MAX);
        out[5] = v.notsent_lowat;
        out[6] = v.notsent_bytes.value_or(UINT32_MAX);
        out[7] = v.rtt_us.value_or(UINT32_MAX);
        out[8] = v.cwnd_segments.value_or(UINT32_MAX);
        out[9] = v.total_retransmits.value_or(UINT32_MAX);
        out[10] = v.output_queue_bytes.value_or(UINT32_MAX);
        out[11] = v.send_buffer_bytes.value_or(UINT32_MAX);
        return 0;
    } catch (...) { return EIO; }
}
void* fc_pacing_controller(unsigned strategy, std::uint64_t floor, std::uint64_t ceiling,
                           std::uint64_t fallback, std::uint32_t target_us) noexcept {
    try { return new p::Controller(static_cast<p::Strategy>(strategy), floor, ceiling, fallback, target_us); }
    catch (...) { return nullptr; }
}
void fc_pacing_delete_controller(void* handle) noexcept { delete static_cast<p::Controller*>(handle); }
unsigned fc_pacing_evaluate(void* handle, const std::uint64_t* fields, std::uint64_t current,
                            std::uint64_t* proposed) noexcept {
    const p::Observation o{fields[0], fields[1], fields[2], fields[3], static_cast<std::uint32_t>(fields[4]),
                           static_cast<std::uint32_t>(fields[5]), static_cast<std::uint32_t>(fields[6]),
                           static_cast<unsigned>(fields[7]), fields[8]!=0, fields[9]!=0, fields[10]!=0};
    const auto result = static_cast<p::Controller*>(handle)->evaluate(o, current, p::Clock::now());
    *proposed = result.rate;
    return static_cast<unsigned>(result.decision);
}
void fc_pacing_confirm(void* handle, bool success) noexcept {
    static_cast<p::Controller*>(handle)->confirm(success);
}
}
