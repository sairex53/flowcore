#include "flowcore/intent_c.h"
#include <stdio.h>
int main(int argc,char **argv) {
    if (argc!=2) return 2;
    fc_v2_options options={.version=2,.size=sizeof(options),.service="c_demo",
                          .target_us=30000,.ttl_seconds=60};
    fc_v2_service *service=fc_v2_open(argv[1],&options);
    if (!service || !fc_v2_wait(service,3000)) return 3;
    fc_v2_request *request=fc_v2_begin(service,10000,0);
    if (!request) return 4;
    (void)fc_v2_finish(request,0);
    int flushed=fc_v2_flush(service,3000);
    fc_v2_close(service);
    return flushed?0:5;
}
