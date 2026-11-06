#ifndef FLOWCORE_FLOW_ENGINE_H
#define FLOWCORE_FLOW_ENGINE_H
#define FC_FLOW_RULES 16
#define FC_FLOW_ABI 2
#define FC_FLOW_PAGE 4
struct fc_rule {
    __u64 cgroup, cookie;
    __u32 mark, service, classid;
    __u16 dport;
    __u8 protocol, reserved;
};
struct fc_policy {
    __u64 generation, netns;
    __u32 count, enforce;
    struct fc_rule rules[FC_FLOW_RULES];
};
struct fc_flow_key {
    __u64 cookie;
    __u32 ifindex;
    __u16 sport, dport;
    __u8 family, protocol;
    __u8 vlan_depth, reserved;
    __u16 vlan_id[2], vlan_proto[2];
    __u32 padding;
    __u8 source[16], destination[16];
};
struct fc_flow_value {
    struct bpf_spin_lock lock;
    __u32 padding;
    __u64 first_ns, last_ns, bytes, packets, cgroup, generation;
    __u32 service, classid, flags, reserved;
};
enum fc_stat { FC_PACKETS, FC_BYTES, FC_PARSED, FC_CLASSIFIED, FC_UNSUPPORTED,
    FC_MAP_FULL, FC_CREATED, FC_GSO_SEGMENTS, FC_APPLIED, FC_BAD_L2, FC_BAD_L3, FC_BAD_L4, FC_FRAGMENT, FC_EXTENSION_LIMIT, FC_PROTOCOL, FC_STAT_COUNT };
#endif
