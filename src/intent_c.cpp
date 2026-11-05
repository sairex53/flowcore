#include "flowcore/intent_c.h"
#include "flowcore/intent_v2.hpp"
#include <exception>
#include <string>
#include <stdexcept>
#include <utility>
using namespace flowcore::v2;
struct fc_v2_service { Service value; };
struct fc_v2_request { Request value; };
namespace {
thread_local std::string error;
void save() noexcept { try { throw; } catch (const std::exception& e) { error=e.what(); } catch (...) { error="unknown SDK error"; } }
}
extern "C" {
fc_v2_service* fc_v2_open(const char* path, const fc_v2_options* o) {
    try {
        if (!path || !o || o->version!=2 || o->size!=sizeof(*o) || !o->service || o->priority>2 ||
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
int fc_v2_finish(fc_v2_request* r,uint32_t outcome) {
    if (!r) return 0;
    bool ok=r->value.complete(outcome<=3?static_cast<Outcome>(outcome):Outcome::Failure);
    delete r;
    return ok;
}
int fc_v2_stats(fc_v2_service* s,fc_v2_statistics* out) {
    if (!s || !out) return 0;
    auto v=s->value.statistics();
    *out={v.queued,v.accepted,v.dropped,v.rejected,v.transport_errors};
    return 1;
}
uint32_t fc_v2_notifications(fc_v2_service* s) { return s ? s->value.notifications() : 0; }
uint64_t fc_v2_associate(fc_v2_service* s,int fd) {
    try { return s ? s->value.associate_socket(fd) : 0; } catch (...) { save();return 0; }
}
const char* fc_v2_error() { return error.c_str(); }
}
