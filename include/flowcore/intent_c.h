#ifndef FLOWCORE_INTENT_C_H
#define FLOWCORE_INTENT_C_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct fc_v2_service fc_v2_service;
typedef struct fc_v2_request fc_v2_request;
typedef struct {
    uint32_t version, size;
    const char *service;
    uint32_t priority; /* 0 standard, 1 interactive, 2 critical */
    uint32_t target_us, deadline_us, desired_kbit, minimum_kbit, maximum_kbit, ttl_seconds;
    uint32_t reliable, isolated;
} fc_v2_options;
typedef struct { uint64_t queued, accepted, dropped, rejected, transport_errors; } fc_v2_statistics;
fc_v2_service *fc_v2_open(const char *socket_path, const fc_v2_options *);
int fc_v2_wait(fc_v2_service *, uint32_t timeout_ms);
int fc_v2_flush(fc_v2_service *, uint32_t timeout_ms);
void fc_v2_close(fc_v2_service *);
fc_v2_request *fc_v2_begin(fc_v2_service *, uint32_t deadline_us, uint64_t cookie);
int fc_v2_finish(fc_v2_request *, uint32_t outcome); /* 0 ok,1 timeout,2 failure,3 cancelled; consumes request */
int fc_v2_stats(fc_v2_service *, fc_v2_statistics *);
uint32_t fc_v2_notifications(fc_v2_service *);
uint64_t fc_v2_associate(fc_v2_service *, int fd);
int fc_v2_dissociate(fc_v2_service *, uint64_t cookie, uint64_t *abandoned);
/* Additive diagnostics: existing options/statistics layouts are unchanged. */
typedef struct { uint64_t session_connections, session_requests, legacy_requests; } fc_v2_transport_statistics;
int fc_v2_transport_stats(fc_v2_service *, fc_v2_transport_statistics *);
uint32_t fc_v2_last_error(fc_v2_service *); /* C++ Error enum: 0 none,1 transport,2 auth,3 invalid,4 rejected,5 protocol */
uint64_t fc_v2_generation(fc_v2_service *);
uint64_t fc_v2_notifications_time(fc_v2_service *);
uint64_t fc_v2_request_id(const fc_v2_request *);
const char *fc_v2_error(void); /* thread-local, valid until next error on this thread */
#ifdef __cplusplus
}
#endif
#endif
