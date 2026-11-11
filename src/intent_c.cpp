#include "flowcore/intent_c.h"
#include "flowcore/intent_v2.hpp"
#include "flowcore/udp_intent.hpp"
#include <algorithm>
#include <array>
#include <exception>
#include <string>
#include <span>
#include <limits>
#include <stdexcept>
#include <utility>
using namespace flowcore::v2;
struct fc_v2_service { Service value; };
struct fc_v2_request { Request value; };
struct fc_v2_udp_sender { std::unique_ptr<flowcore::udp::MessageSender> value; };
namespace {
thread_local std::string error;
void save() noexcept { try { throw; } catch (const std::exception& e) { error=e.what(); } catch (...) { error="unknown SDK error"; } }
}
extern "C" {
fc_v2_service* fc_v2_open(const char* path, const fc_v2_options* o) {
    try {
        if (!path || !o || o->version!=2 || o->size<sizeof(*o) || !o->service || o->priority>2 ||
            o->reliable>1 || o->isolated>1) throw std::invalid_argument("invalid C ABI options");
        Intent intent;
        intent.service=o->service;
        intent.priority=static_cast<Priority>(o->priority);
        intent.target_latency_us=o->target_us;
        intent.deadline_us=o->deadline_us;
        intent.desired_kbit=o->desired_kbit;
        intent.minimum_kbit=o->minimum_kbit;
        intent.maximum_kbit=o->maximum_kbit;
        intent.lifetime_seconds=o->ttl_seconds;
        intent.prefer_reliable=o->reliable;
        intent.prefer_isolation=o->isolated;
        return new fc_v2_service{Service(path,std::move(intent))};
    } catch (...) { save(); return nullptr; }
}
int fc_v2_wait(fc_v2_service* s,uint32_t ms) { try { return s && s->value.wait_ready(std::chrono::milliseconds(ms)); } catch (...) { save();return 0; } }
int fc_v2_flush(fc_v2_service* s,uint32_t ms) { try { return s && s->value.flush(std::chrono::milliseconds(ms)); } catch (...) { save();return 0; } }
void fc_v2_close(fc_v2_service* s) { delete s; }
fc_v2_request* fc_v2_begin(fc_v2_service* s,uint32_t us,uint64_t cookie) {
    try { return s ? new fc_v2_request{s->value.begin_request(us,cookie)} : nullptr; } catch (...) { save();return nullptr; }
}
fc_v2_request* fc_v2_begin_stream(fc_v2_service* s,uint64_t stream,uint32_t priority,uint32_t deadline,uint64_t cookie) {
    try { return s ? new fc_v2_request{s->value.begin_stream_request(stream,priority,deadline,cookie)} : nullptr; }
    catch (...) { save();return nullptr; }
}
int fc_v2_finish(fc_v2_request* r,uint32_t outcome) {
    if (!r) return 0;
    if (outcome>3) { delete r;error="invalid request outcome";return 0; }
    bool ok=r->value.complete(static_cast<Outcome>(outcome));
    delete r;
    return ok;
}
int fc_v2_cancel(fc_v2_request* request) { return fc_v2_finish(request,3); }
int fc_v2_stats(fc_v2_service* s,fc_v2_statistics* out) {
    if (!s || !out) return 0;
    auto v=s->value.statistics();
    *out={v.queued,v.accepted,v.dropped,v.rejected,v.transport_errors};
    return 1;
}
uint32_t fc_v2_notifications(fc_v2_service* s) { return s ? s->value.notifications() : 0; }
int fc_v2_get_capabilities(fc_v2_service* s,fc_v2_capability_snapshot* out) {
    if (!s || !out || out->version!=1 || out->size<sizeof(*out)) {
        error="invalid capability snapshot ABI version or size";return 0;
    }
    const auto value=s->value.capabilities();
    *out={1,static_cast<uint32_t>(sizeof(*out)),value.protocol_version,0,value.features,value.udp_message_authorized?1U:0U,0};
    return 1;
}
int fc_v2_inspect_policy(fc_v2_service* s,fc_v2_policy_snapshot* out) {
    try {
        if (!s || !out || out->version!=1 || out->size<sizeof(*out))
            throw std::invalid_argument("invalid policy snapshot ABI version or size");
        auto value=s->value.effective_policy();
        if (!value) throw std::runtime_error(value.error().message);
        *out={1,static_cast<uint32_t>(sizeof(*out)),static_cast<uint32_t>(value->state),value->kernel_verified?1U:0U,
              value->generation,static_cast<uint32_t>(value->accepted_intent.priority),
              value->accepted_intent.target_latency_us,value->accepted_intent.deadline_us,
              value->accepted_intent.desired_kbit,value->accepted_intent.minimum_kbit,
              value->accepted_intent.maximum_kbit,value->accepted_intent.lifetime_seconds,
              value->accepted_intent.prefer_reliable?1U:0U,
              value->class_rate_kbit,value->class_ceil_kbit};
        return 1;
    } catch (...) { save();return 0; }
}
int fc_v2_get_notification_snapshot(fc_v2_service* s,fc_v2_notification_snapshot* out) {
    if (!s || !out || out->version!=1 || out->size<sizeof(*out)) {
        error="invalid notification snapshot ABI version or size";return 0;
    }
    const auto value=s->value.notification_snapshot();
    *out={1,static_cast<uint32_t>(sizeof(*out)),value.flags,value.network_observed?1U:0U,value.observed_ns,value.application_samples};
    return 1;
}
int fc_v2_subscribe_notifications(fc_v2_service* s,uint32_t enabled) {
    if (!s || enabled>1) { error="invalid notification subscription";return 0; }
    s->value.subscribe_notifications(enabled!=0);
    return 1;
}
fc_v2_udp_sender* fc_v2_udp_sender_open(fc_v2_service* s,int fd,uint32_t timestamps) {
    try {
        if (!s || timestamps>1) throw std::invalid_argument("invalid UDP sender arguments");
        return new fc_v2_udp_sender{s->value.udp_message_sender(fd,timestamps!=0)};
    } catch (...) { save();return nullptr; }
}
void fc_v2_udp_sender_close(fc_v2_udp_sender* sender) { delete sender; }
uint64_t fc_v2_udp_quota_rejections(fc_v2_udp_sender* sender) {
    return sender && sender->value ? sender->value->urgency_quota_rejections() : 0;
}
namespace {
bool convert_udp_message(const fc_v2_udp_message& input,flowcore::udp::Message& output) {
    if (input.version!=1 || input.size<sizeof(input) || input.traffic_class>FC_V2_UDP_BACKGROUND ||
        input.expiration>FC_V2_UDP_DEMOTE_EXPIRED || input.payload_size>65507 ||
        input.payload_size>std::numeric_limits<size_t>::max() ||
        (!input.payload && input.payload_size) ||
        input.destination_size>std::numeric_limits<socklen_t>::max() ||
        (!input.destination && input.destination_size))
        return false;
    output.payload={static_cast<const std::byte*>(input.payload),static_cast<size_t>(input.payload_size)};
    output.intent={static_cast<flowcore::udp::MessageClass>(input.traffic_class),
                   input.deadline_monotonic_ns,
                   static_cast<flowcore::udp::ExpirationPolicy>(input.expiration),input.message_id};
    output.destination=input.destination;
    output.destination_length=static_cast<socklen_t>(input.destination_size);
    return true;
}
void write_udp_result(const flowcore::udp::SendResult& value,fc_v2_udp_result& output) {
    output={1,static_cast<uint32_t>(sizeof(output)),static_cast<uint32_t>(value.status),
            static_cast<uint32_t>(value.applied_class),value.bytes,value.message_id,
            value.submitted_monotonic_ns,value.error_number,value.deadline_missed?1U:0U};
}
}
int fc_v2_udp_send(fc_v2_udp_sender* sender,const fc_v2_udp_message* input,fc_v2_udp_result* output) {
    try {
        if (!sender || !sender->value || !input || !output || output->version!=1 ||
            output->size<sizeof(*output)) throw std::invalid_argument("invalid UDP send ABI arguments");
        flowcore::udp::Message message;
        if (!convert_udp_message(*input,message)) throw std::invalid_argument("invalid UDP message structure");
        write_udp_result(sender->value->send_message(message.payload,message.intent,message.destination,
            message.destination_length),*output);
        return 1;
    } catch (...) { save();return 0; }
}
int fc_v2_udp_send_batch(fc_v2_udp_sender* sender,const fc_v2_udp_message* input,uint32_t count,
                         fc_v2_udp_result* output) {
    try {
        if (!sender || !sender->value || (!input && count) || (!output && count) || count>4096)
            throw std::invalid_argument("invalid or oversized UDP batch");
        // Validate the complete caller buffer before sending any datagram, so a malformed
        // later element cannot turn the operation into a surprising partial submission.
        for (uint32_t i=0;i<count;++i) {
            flowcore::udp::Message ignored;
            if (output[i].version!=1 || output[i].size<sizeof(output[i]) ||
                !convert_udp_message(input[i],ignored))
                throw std::invalid_argument("invalid UDP batch item");
        }
        constexpr std::size_t block_size=64;
        std::array<flowcore::udp::Message,block_size> messages{};
        std::array<flowcore::udp::SendResult,block_size> results{};
        for (uint32_t base=0;base<count;) {
            const auto block=std::min<std::size_t>(block_size,count-base);
            for (std::size_t i=0;i<block;++i)
                if (!convert_udp_message(input[base+i],messages[i]))
                    throw std::invalid_argument("invalid UDP batch item");
            const auto message_span=std::span<const flowcore::udp::Message>(messages.data(),block);
            const auto result_span=std::span<flowcore::udp::SendResult>(results.data(),block);
            if (!sender->value->send_batch_into(message_span,result_span))
                throw std::invalid_argument("UDP batch output mismatch");
            for (std::size_t i=0;i<block;++i) write_udp_result(results[i],output[base+i]);
            base+=static_cast<uint32_t>(block);
        }
        return 1;
    } catch (...) { save();return 0; }
}
uint64_t fc_v2_associate(fc_v2_service* s,int fd) {
    try { return s ? s->value.associate_socket(fd) : 0; } catch (...) { save();return 0; }
}
int fc_v2_dissociate(fc_v2_service* s,uint64_t cookie,uint64_t* abandoned) {
    try {
        if (!s) return 0;
        auto count=s->value.dissociate_socket(cookie);
        if (abandoned) *abandoned=count;
        return 1;
    } catch (...) { save();return 0; }
}
int fc_v2_transport_stats(fc_v2_service* s,fc_v2_transport_statistics* out) {
    if (!s || !out) return 0;
    auto stats=s->value.transport_statistics();
    *out={stats.session_connections,stats.session_requests,stats.legacy_requests};
    return 1;
}
uint32_t fc_v2_last_error(fc_v2_service* s) { return s?static_cast<uint32_t>(s->value.last_error()):1; }
uint64_t fc_v2_generation(fc_v2_service* s) { return s?s->value.registration_generation():0; }
uint64_t fc_v2_notifications_time(fc_v2_service* s) { return s?s->value.notifications_time_ns():0; }
uint64_t fc_v2_request_id(const fc_v2_request* r) { return r?r->value.id():0; }
int fc_v2_declare_transport(fc_v2_service* s,const char* session,uint32_t kind,
                            uint32_t multiplexed,uint32_t mobility,uint64_t cookie) {
    try {
        if (!s || !session || kind>4 || multiplexed>1 || mobility>1)
            throw std::invalid_argument("invalid transport declaration");
        s->value.declare_transport({session,static_cast<TransportKind>(kind),
                                   bool(multiplexed),bool(mobility),cookie});
        return 1;
    } catch (...) { save();return 0; }
}
int fc_v2_transport_state(fc_v2_service* s,const char* session,uint32_t degraded,const char* reason) {
    try {
        if (!s || !session || !reason || degraded>1) throw std::invalid_argument("invalid transport state");
        s->value.report_transport_state(session,bool(degraded),reason);
        return 1;
    } catch (...) { save();return 0; }
}
const char* fc_v2_error() { return error.c_str(); }
}
