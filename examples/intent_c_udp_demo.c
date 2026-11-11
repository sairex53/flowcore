#define _GNU_SOURCE
#include "flowcore/intent_c.h"
#include <arpa/inet.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#ifndef SO_MARK
#define SO_MARK 36
#endif

static uint64_t monotonic_ns(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) return 0;
    return (uint64_t)value.tv_sec * UINT64_C(1000000000) + (uint64_t)value.tv_nsec;
}

int main(int argc, char **argv) {
    if (argc != 6) {
        fprintf(stderr, "usage: intent_c_udp_demo <control.sock> <service> <ipv4> <port> <operator-mark>\n");
        return 2;
    }
    char *end = NULL;
    long port = strtol(argv[4], &end, 10);
    if (!end || *end || port < 1 || port > 65535) return 2;
    long mark = strtol(argv[5], &end, 10);
    if (!end || *end || mark < 1 || mark > 0x7fffffff) return 2;

    int fd = socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, IPPROTO_UDP);
    if (fd < 0) return 1;
    struct sockaddr_in destination = {.sin_family = AF_INET, .sin_port = htons((uint16_t)port)};
    if (inet_pton(AF_INET, argv[3], &destination.sin_addr) != 1 ||
        connect(fd, (struct sockaddr *)&destination, sizeof(destination)) != 0) {
        close(fd);
        return 1;
    }

    /* Ordinary UDP remains usable even if the optional policy path is unavailable. */
    static const char routine[] = "routine telemetry";
    if (send(fd, routine, sizeof(routine) - 1, MSG_DONTWAIT) < 0 && errno != EAGAIN) {
        close(fd);
        return 1;
    }

    if (setsockopt(fd, SOL_SOCKET, SO_MARK, &mark, sizeof(mark)) != 0) {
        fprintf(stderr, "SO_MARK unavailable; ordinary UDP was sent without FLOWCORE urgency\n");
        close(fd);
        return 0;
    }

    fc_v2_options options = {.version = 2, .size = sizeof(options), .service = argv[2],
                             .priority = 1, .target_us = 30000, .deadline_us = 50000,
                             .ttl_seconds = 60};
    fc_v2_service *service = fc_v2_open(argv[1], &options);
    if (!service) {
        fprintf(stderr, "FLOWCORE unavailable: %s; ordinary UDP already sent\n", fc_v2_error());
        close(fd);
        return 0;
    }
    if (!fc_v2_wait(service, 250)) {
        fprintf(stderr, "FLOWCORE offline; ordinary UDP remains available\n");
        fc_v2_close(service);
        close(fd);
        return 0;
    }

    fc_v2_udp_sender *sender = fc_v2_udp_sender_open(service, fd, 0);
    if (!sender) {
        fprintf(stderr, "per-message priority not authorized: %s; ordinary UDP remains available\n",
                fc_v2_error());
        fc_v2_close(service);
        close(fd);
        return 0;
    }

    static const char urgent[] = "urgent sample";
    fc_v2_udp_message message = {
        .version = 1, .size = sizeof(message), .traffic_class = FC_V2_UDP_URGENT,
        .expiration = FC_V2_UDP_DROP_EXPIRED, .payload = urgent,
        .payload_size = sizeof(urgent) - 1, .deadline_monotonic_ns = monotonic_ns() + UINT64_C(50000000),
        .message_id = 1, .destination = NULL, .destination_size = 0
    };
    fc_v2_udp_result result = {.version = 1, .size = sizeof(result)};
    if (!fc_v2_udp_send(sender, &message, &result)) {
        fprintf(stderr, "intent send unavailable: %s; application should use its normal send path\n",
                fc_v2_error());
    } else {
        printf("status=%u class=%u bytes=%llu deadline_missed=%u\n", result.status,
               result.traffic_class, (unsigned long long)result.bytes, result.deadline_missed);
    }

    fc_v2_udp_sender_close(sender);
    fc_v2_close(service);
    close(fd);
    return 0;
}
