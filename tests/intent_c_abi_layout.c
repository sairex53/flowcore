#include <stddef.h>
#include <stdint.h>
#include "flowcore/intent_c.h"

_Static_assert(sizeof(void *) == 8, "FLOWCORE C ABI v2 currently supports 64-bit Linux targets");
_Static_assert(sizeof(fc_v2_options) == 56, "fc_v2_options layout changed");
_Static_assert(offsetof(fc_v2_options, service) == 8, "fc_v2_options.service offset changed");
_Static_assert(offsetof(fc_v2_options, priority) == 16, "fc_v2_options.priority offset changed");
_Static_assert(sizeof(fc_v2_statistics) == 40, "fc_v2_statistics layout changed");
_Static_assert(sizeof(fc_v2_udp_message) == 64, "fc_v2_udp_message layout changed");
_Static_assert(offsetof(fc_v2_udp_message, payload) == 16, "fc_v2_udp_message.payload offset changed");
_Static_assert(offsetof(fc_v2_udp_message, destination) == 48, "fc_v2_udp_message.destination offset changed");
_Static_assert(sizeof(fc_v2_udp_result) == 48, "fc_v2_udp_result layout changed");
_Static_assert(sizeof(fc_v2_capability_snapshot) == 32, "fc_v2_capability_snapshot layout changed");
_Static_assert(sizeof(fc_v2_policy_snapshot) == 64, "fc_v2_policy_snapshot layout changed");
_Static_assert(sizeof(fc_v2_notification_snapshot) == 32, "fc_v2_notification_snapshot layout changed");
_Static_assert(sizeof(fc_v2_transport_statistics) == 24, "fc_v2_transport_statistics layout changed");

int main(void) { return 0; }
