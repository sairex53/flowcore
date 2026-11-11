#ifndef FLOWCORE_INTENT_C_H
#define FLOWCORE_INTENT_C_H
#include <stdint.h>
#include <sys/socket.h>
#ifdef __cplusplus
extern "C" {
#endif
typedef struct fc_v2_service fc_v2_service;
typedef struct fc_v2_request fc_v2_request;
typedef struct fc_v2_udp_sender fc_v2_udp_sender;
typedef struct {
    uint32_t version, size;
    const char *service;
    uint32_t priority; /* 0 standard, 1 interactive, 2 critical */
    uint32_t target_us, deadline_us, desired_kbit, minimum_kbit, maximum_kbit, ttl_seconds;
    uint32_t reliable, isolated;
} fc_v2_options;
typedef struct { uint64_t queued, accepted, dropped, rejected, transport_errors; } fc_v2_statistics;
/* UDP classes: 0 standard, 1 urgent, 2 background; expiration: 0 drop, 1 demote.
 * deadline_monotonic_ns is a CLOCK_MONOTONIC-domain advisory deadline. */
typedef struct {
    uint32_t version, size, traffic_class, expiration;
    const void *payload;
    uint64_t payload_size, deadline_monotonic_ns, message_id;
    const struct sockaddr *destination;
    uint32_t destination_size;
} fc_v2_udp_message;
/* Send status values follow flowcore::udp::SendStatus (0..6). */
typedef struct {
    uint32_t version, size, status, traffic_class;
    uint64_t bytes, message_id, submitted_monotonic_ns;
    int32_t error_number;
    uint32_t deadline_missed;
} fc_v2_udp_result;
#define FC_V2_UDP_STANDARD 0U
#define FC_V2_UDP_URGENT 1U
#define FC_V2_UDP_BACKGROUND 2U
#define FC_V2_UDP_DROP_EXPIRED 0U
#define FC_V2_UDP_DEMOTE_EXPIRED 1U

/* Additive version-1 result structures; initialize version=1 and size=sizeof(struct). */
#define FC_V2_CAP_LEASE_RENEWAL          (UINT64_C(1) << 0)
#define FC_V2_CAP_SOCKET_ASSOCIATION     (UINT64_C(1) << 1)
#define FC_V2_CAP_SOCKET_DISSOCIATION    (UINT64_C(1) << 2)
#define FC_V2_CAP_NOTIFICATION_QUALITY   (UINT64_C(1) << 3)
#define FC_V2_CAP_TRANSPORT_SESSIONS     (UINT64_C(1) << 4)
#define FC_V2_CAP_DEADLINE_ADMISSION     (UINT64_C(1) << 5)
#define FC_V2_CAP_UDP_PER_MESSAGE        (UINT64_C(1) << 6)
#define FC_V2_POLICY_UNKNOWN             0U
#define FC_V2_POLICY_REGISTERED_ONLY     1U
#define FC_V2_POLICY_KERNEL_VERIFIED     2U
typedef struct {
    uint32_t version, size, protocol_version, reserved;
    uint64_t features;
    uint32_t udp_message_authorized, reserved2;
} fc_v2_capability_snapshot;
typedef struct {
    uint32_t version, size, state, kernel_verified;
    uint64_t policy_generation;
    uint32_t priority, target_latency_us, deadline_us, desired_kbit;
    uint32_t minimum_kbit, maximum_kbit, lifetime_seconds, prefer_reliable;
    uint32_t class_rate_kbit, class_ceil_kbit;
} fc_v2_policy_snapshot;
typedef struct {
    uint32_t version, size, flags, network_observed;
    uint64_t observed_ns, application_samples;
} fc_v2_notification_snapshot;
fc_v2_service *fc_v2_open(const char *socket_path, const fc_v2_options *);
int fc_v2_wait(fc_v2_service *, uint32_t timeout_ms);
int fc_v2_flush(fc_v2_service *, uint32_t timeout_ms);
void fc_v2_close(fc_v2_service *);
fc_v2_request *fc_v2_begin(fc_v2_service *, uint32_t deadline_us, uint64_t cookie);
fc_v2_request *fc_v2_begin_stream(fc_v2_service *, uint64_t stream_id, uint32_t priority, uint32_t deadline_us, uint64_t cookie);
int fc_v2_finish(fc_v2_request *, uint32_t outcome); /* 0 ok,1 timeout,2 failure,3 cancelled; consumes request */
int fc_v2_cancel(fc_v2_request *); /* convenience for finish(...,3); consumes request */
int fc_v2_stats(fc_v2_service *, fc_v2_statistics *);
uint32_t fc_v2_notifications(fc_v2_service *);
int fc_v2_get_capabilities(fc_v2_service *, fc_v2_capability_snapshot *);
int fc_v2_inspect_policy(fc_v2_service *, fc_v2_policy_snapshot *); /* bounded read-only IPC */
int fc_v2_get_notification_snapshot(fc_v2_service *, fc_v2_notification_snapshot *);
int fc_v2_subscribe_notifications(fc_v2_service *, uint32_t enabled); /* enable periodic bounded polling */
fc_v2_udp_sender *fc_v2_udp_sender_open(fc_v2_service *, int fd, uint32_t enable_timestamps);
void fc_v2_udp_sender_close(fc_v2_udp_sender *);
int fc_v2_udp_send(fc_v2_udp_sender *, const fc_v2_udp_message *, fc_v2_udp_result *);
int fc_v2_udp_send_batch(fc_v2_udp_sender *, const fc_v2_udp_message *, uint32_t count,
                         fc_v2_udp_result *); /* bounded to 4096 messages */
uint64_t fc_v2_associate(fc_v2_service *, int fd);
int fc_v2_dissociate(fc_v2_service *, uint64_t cookie, uint64_t *abandoned);
/* Additive diagnostics: existing options/statistics layouts are unchanged. */
typedef struct { uint64_t session_connections, session_requests, legacy_requests; } fc_v2_transport_statistics;
int fc_v2_transport_stats(fc_v2_service *, fc_v2_transport_statistics *);
uint32_t fc_v2_last_error(fc_v2_service *); /* C++ Error enum: 0 none,1 transport,2 auth,3 invalid,4 rejected,5 protocol */
uint64_t fc_v2_generation(fc_v2_service *);
uint64_t fc_v2_notifications_time(fc_v2_service *);
uint64_t fc_v2_request_id(const fc_v2_request *);
/* Additive transport declarations: kind 0 TCP,1 MPTCP,2 UDP,3 QUIC,4 logical. */
int fc_v2_declare_transport(fc_v2_service *, const char *session, uint32_t kind,
                            uint32_t multiplexed, uint32_t mobility, uint64_t cookie);
int fc_v2_transport_state(fc_v2_service *, const char *session, uint32_t degraded, const char *error);
const char *fc_v2_error(void); /* thread-local, valid until next error on this thread */
#ifdef __cplusplus
}
#endif
#endif
