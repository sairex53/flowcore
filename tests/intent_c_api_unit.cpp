#include "flowcore/intent_c.h"

#include <cstdint>

int main() {
    if (fc_v2_open(nullptr, nullptr) != nullptr) return 1;
    const char service_name[]="c_abi_offline";
    fc_v2_options options{};
    options.version=2;
    options.size=sizeof(options)+16; // Future extension tail is accepted by the v2 ABI.
    options.service=service_name;
    options.target_us=30000;
    options.ttl_seconds=60;
    auto* service=fc_v2_open("/nonexistent-flowcore-sdk-test.sock",&options);
    if (!service) return 9;
    if (fc_v2_wait(service,5)) return 10;
    fc_v2_capability_snapshot online_caps{};
    online_caps.version=1;
    online_caps.size=sizeof(online_caps);
    if (!fc_v2_get_capabilities(service,&online_caps) || online_caps.protocol_version!=0) return 11;
    auto* request=fc_v2_begin(service,1000,0);
    if (!request || fc_v2_cancel(request)) return 12; // Offline cancellation is local; telemetry enqueue reports false.
    fc_v2_close(service);
    if (fc_v2_udp_sender_open(nullptr, -1, 0) != nullptr) return 2;
    if (!fc_v2_error() || !*fc_v2_error()) return 3;
    fc_v2_udp_message message{};
    fc_v2_udp_result result{};
    result.version=1;
    result.size=sizeof(result);
    if (fc_v2_udp_send(nullptr, &message, &result) != 0) return 4;
    if (fc_v2_udp_send_batch(nullptr, nullptr, 0, nullptr) != 0) return 5;
    fc_v2_capability_snapshot capabilities{};
    capabilities.version=1;
    capabilities.size=sizeof(capabilities);
    if (fc_v2_get_capabilities(nullptr, &capabilities) != 0) return 6;
    fc_v2_policy_snapshot policy{};
    policy.version=1;
    policy.size=sizeof(policy);
    if (fc_v2_inspect_policy(nullptr, &policy) != 0) return 7;
    fc_v2_notification_snapshot notifications{};
    notifications.version=1;
    notifications.size=sizeof(notifications);
    if (fc_v2_get_notification_snapshot(nullptr, &notifications) != 0) return 8;
    return 0;
}
