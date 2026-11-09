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
int fc_pacing_rate_timed(void* handle, std::uint64_t rate, std::uint64_t* out) noexcept {
    p::RateTiming timing;
    const auto result = static_cast<p::SocketPolicy*>(handle)->set_rate(rate, &timing);
    out[0] = timing.pin_ns;
    out[1] = timing.precheck_ns;
    out[2] = timing.write_ns;
    out[3] = timing.readback_ns;
    out[4] = timing.write_attempted;
    return result.value();
}
int fc_pacing_close(void* handle) noexcept {
    std::unique_ptr<p::SocketPolicy> managed(static_cast<p::SocketPolicy*>(handle));
    return managed->remove().value();
}
int fc_pacing_cc(int fd, const char* name) noexcept {
    try { return p::select_congestion(fd, name).value(); }
    catch (...) { return EIO; }
}
static int inspect_fields(int fd, std::uint64_t* out, bool extended) noexcept {
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
        if (extended) {
            out[12] = v.ecn_negotiated ? static_cast<unsigned>(*v.ecn_negotiated) : UINT32_MAX;
            out[13] = v.delivered_segments.value_or(UINT32_MAX);
            out[14] = v.delivered_ce_segments.value_or(UINT32_MAX);
            out[15] = v.ecn_mode.value_or(UINT32_MAX);
        }
        return 0;
    } catch (...) { return EIO; }
}
int fc_pacing_inspect(int fd, std::uint64_t* out) noexcept { return inspect_fields(fd, out, false); }
int fc_pacing_inspect_v2(int fd, std::uint64_t* out) noexcept { return inspect_fields(fd, out, true); }
void* fc_pacing_controller(unsigned strategy, std::uint64_t floor, std::uint64_t ceiling,
                           std::uint64_t fallback, std::uint32_t target_us) noexcept {
    try { return new p::Controller(static_cast<p::Strategy>(strategy), floor, ceiling, fallback, target_us); }
    catch (...) { return nullptr; }
}
void fc_pacing_delete_controller(void* handle) noexcept { delete static_cast<p::Controller*>(handle); }
static unsigned evaluate_fields(void* handle, const std::uint64_t* fields, std::uint64_t current,
                                std::uint64_t* proposed, unsigned count, bool shadow = false, unsigned* reason = nullptr) noexcept {
    const p::Observation o{fields[0], fields[1], fields[2], fields[3], static_cast<std::uint32_t>(fields[4]),
                           static_cast<std::uint32_t>(fields[5]), static_cast<std::uint32_t>(fields[6]),
                           static_cast<unsigned>(fields[7]), fields[8]!=0, fields[9]!=0, fields[10]!=0,
                           count >= 12 && fields[11]!=0, count >= 13 && fields[12]!=0,
                           count >= 14 ? fields[13] : 0,
                           count >= 19 ? fields[14] : 0, count >= 19 ? fields[15] : 0,
                           p::Clock::time_point{std::chrono::nanoseconds(count >= 19 ? fields[16] : 0)},
                           p::Clock::time_point{std::chrono::nanoseconds(count >= 19 ? fields[17] : 0)},
                           p::Clock::time_point{std::chrono::nanoseconds(count >= 20 ? fields[19] : 0)},
                           count >= 19 ? fields[18] : 0};
    auto& controller = *static_cast<p::Controller*>(handle);
    const auto now = p::Clock::now();
    const auto result = shadow ? controller.preview(o, current, now) : controller.evaluate(o, current, now);
    *proposed = result.rate;
    if (reason) *reason = static_cast<unsigned>(result.reason);
    return static_cast<unsigned>(result.decision);
}
unsigned fc_pacing_evaluate(void* handle, const std::uint64_t* fields, std::uint64_t current,
                            std::uint64_t* proposed) noexcept {
    return evaluate_fields(handle, fields, current, proposed, 11);
}
unsigned fc_pacing_evaluate_v2(void* handle, const std::uint64_t* fields, std::uint64_t current,
                               std::uint64_t* proposed) noexcept {
    return evaluate_fields(handle, fields, current, proposed, 12);
}
unsigned fc_pacing_evaluate_v3(void* handle, const std::uint64_t* fields, std::uint64_t current,
                               std::uint64_t* proposed) noexcept {
    return evaluate_fields(handle, fields, current, proposed, 13);
}
unsigned fc_pacing_evaluate_v4(void* handle, const std::uint64_t* fields, std::uint64_t current,
                               std::uint64_t* proposed) noexcept {
    return evaluate_fields(handle, fields, current, proposed, 14);
}
unsigned fc_pacing_evaluate_v5(void* handle, const std::uint64_t* fields, std::uint64_t current,
                               std::uint64_t* proposed, unsigned* reason) noexcept {
    return evaluate_fields(handle, fields, current, proposed, 20, false, reason);
}
unsigned fc_pacing_shadow_v1(void* handle, const std::uint64_t* fields, std::uint64_t current,
                             std::uint64_t* proposed) noexcept {
    return evaluate_fields(handle, fields, current, proposed, 13, true);
}
void fc_pacing_confirm(void* handle, bool success) noexcept {
    static_cast<p::Controller*>(handle)->confirm(success);
}
}
