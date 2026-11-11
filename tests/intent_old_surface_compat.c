/* Deliberately use only the original v2 C symbols and data layouts. */
#include "flowcore/intent_c.h"
#include <stdint.h>
#include <string.h>

int main(int argc,char **argv) {
    if (argc!=2) return 2;
    const char service_name[]="old_surface_client";
    fc_v2_options options;
    memset(&options,0,sizeof(options));
    options.version=2;
    options.size=(uint32_t)sizeof(options);
    options.service=service_name;
    options.target_us=30000;
    options.ttl_seconds=60;
    fc_v2_service *service=fc_v2_open(argv[1],&options);
    if (!service) return 3;
    if (!fc_v2_wait(service,2000)) { fc_v2_close(service); return 4; }
    fc_v2_request *request=fc_v2_begin(service,10000,0);
    if (!request || !fc_v2_finish(request,0) || !fc_v2_flush(service,2000)) {
        fc_v2_close(service);
        return 5;
    }
    fc_v2_statistics stats;
    if (!fc_v2_stats(service,&stats) || stats.accepted<2 || stats.transport_errors) {
        fc_v2_close(service);
        return 6;
    }
    fc_v2_close(service);
    return 0;
}
