#ifndef FLOWCORE_OBSERVE_EVENT_H
#define FLOWCORE_OBSERVE_EVENT_H
struct flow_event {
    __u64 time_ns;
    __u64 cookie;
    __u64 lifetime_ns;
    __u64 flow_id;
    __u32 netns;
    __u16 sport;
    __u16 dport;
    __u16 family;
    __u8 kind;
    __u8 old_state;
    __u8 new_state;
    __u8 origin_state;
};
#endif
