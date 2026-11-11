#include "flowcore/intent_c.h"
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>
int main(int argc,char **argv) {
    if (argc!=2) return 2;
    struct ExtendedOptions {
        fc_v2_options known;
        uint64_t future_reserved[2];
    } options;
    memset(&options,0,sizeof(options));
    options.known.version=2;
    options.known.size=sizeof(options); /* Future tail is ignored by this v2 library. */
    options.known.service="c_demo";
    options.known.target_us=30000;
    options.known.ttl_seconds=60;
    fc_v2_service *service=fc_v2_open(argv[1],&options.known);
    if (!service) return 3;
    int online=fc_v2_wait(service,250);
    if (online) {
        fc_v2_capability_snapshot capabilities={.version=1,.size=sizeof(capabilities)};
        if (!fc_v2_get_capabilities(service,&capabilities) || capabilities.protocol_version!=2) return 6;
        fc_v2_policy_snapshot policy={.version=1,.size=sizeof(policy)};
        if (!fc_v2_inspect_policy(service,&policy) || policy.state==FC_V2_POLICY_UNKNOWN) return 7;
        fc_v2_notification_snapshot notifications={.version=1,.size=sizeof(notifications)};
        if (!fc_v2_get_notification_snapshot(service,&notifications)) return 8;
    }
    int udp_fd=socket(AF_INET,SOCK_DGRAM|SOCK_CLOEXEC,0);
    if (udp_fd<0) return 9;
    fc_v2_udp_sender *unauthorized=fc_v2_udp_sender_open(service,udp_fd,0);
    if (unauthorized) { fc_v2_udp_sender_close(unauthorized); close(udp_fd); return 10; }
    close(udp_fd);
    fc_v2_request *request=fc_v2_begin(service,10000,0);
    if (!request) return 4;
    (void)fc_v2_finish(request,0);
    int flushed=online?fc_v2_flush(service,3000):1;
    fc_v2_close(service);
    return flushed?0:5;
}
