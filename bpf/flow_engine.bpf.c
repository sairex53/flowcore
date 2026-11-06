#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>
#include "flow_engine.h"
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, struct fc_policy);
} policies SEC(".maps");
struct {
    __uint(type, BPF_MAP_TYPE_HASH);
    __uint(max_entries, 4096);
    __type(key, struct fc_flow_key);
    __type(value, struct fc_flow_value);
} flows SEC(".maps");
struct {
    __uint(type, BPF_MAP_TYPE_PERCPU_ARRAY);
    __uint(max_entries, FC_STAT_COUNT);
    __type(key, __u32);
    __type(value, __u64);
} totals SEC(".maps");
static __always_inline void add(__u32 index, __u64 value) {
    __u64 *counter=bpf_map_lookup_elem(&totals,&index);
    if (counter) *counter+=value;
}
SEC("tcx/egress")
int flow_egress(struct __sk_buff *skb) {
    __u32 zero=0;
    struct fc_policy *policy=bpf_map_lookup_elem(&policies,&zero);
    if (!policy) return -1; /* TCX_NEXT */
    add(FC_PACKETS,1);
    add(FC_BYTES,skb->len);
    if (bpf_core_field_exists(skb->gso_segs)) add(FC_GSO_SEGMENTS,skb->gso_segs);
    struct fc_flow_key key={};
    __u16 ethernet=0;
    __u32 offset=14;
    __u8 flags=0;
    if (bpf_skb_load_bytes(skb,12,&ethernet,2)) goto unsupported;
    if (ethernet==bpf_htons(0x0800)) {
        struct iphdr ip;
        if (bpf_skb_load_bytes(skb,offset,&ip,sizeof(ip)) || ip.version!=4 || ip.ihl<5 ||
            (ip.frag_off & bpf_htons(0x3fff))) goto unsupported;
        if (bpf_ntohs(ip.tot_len)<ip.ihl*4+8 || 14+bpf_ntohs(ip.tot_len)>skb->len) goto unsupported;
        key.family=2;
        key.protocol=ip.protocol;
        __builtin_memcpy(key.source,&ip.saddr,4);
        __builtin_memcpy(key.destination,&ip.daddr,4);
        offset+=ip.ihl*4;
    } else if (ethernet==bpf_htons(0x86dd)) {
        struct ipv6hdr ip;
        if (bpf_skb_load_bytes(skb,offset,&ip,sizeof(ip)) || ip.version!=6) goto unsupported;
        key.family=10;
        key.protocol=ip.nexthdr;
        __builtin_memcpy(key.source,&ip.saddr,16);
        __builtin_memcpy(key.destination,&ip.daddr,16);
        offset+=40;
        /* Extension headers deliberately stay unknown; no unsafe partial parsing. */
    } else goto unsupported;
    if (key.protocol==6) {
        struct tcphdr tcp;
        if (bpf_skb_load_bytes(skb,offset,&tcp,sizeof(tcp)) || tcp.doff<5 || offset+tcp.doff*4>skb->len) goto unsupported;
        key.sport=bpf_ntohs(tcp.source); key.dport=bpf_ntohs(tcp.dest);
        flags=(tcp.fin?1:0)|(tcp.rst?2:0)|(tcp.syn?4:0)|((tcp.syn && !tcp.ack)?8:0);
    } else if (key.protocol==17) {
        struct udphdr udp;
        if (bpf_skb_load_bytes(skb,offset,&udp,sizeof(udp)) || bpf_ntohs(udp.len)<8) goto unsupported;
        key.sport=bpf_ntohs(udp.source); key.dport=bpf_ntohs(udp.dest);
    } else goto unsupported;
    key.cookie=bpf_get_socket_cookie(skb);
    key.ifindex=skb->ifindex;
    __u64 cgroup=bpf_skb_cgroup_id(skb);
    __u32 service=0,classid=0;
    #pragma unroll
    for (int i=0;i<FC_FLOW_RULES;++i) {
        if (i>=policy->count) break;
        struct fc_rule *rule=&policy->rules[i];
        if (rule->protocol!=key.protocol || (rule->dport && rule->dport!=key.dport) ||
            (rule->mark && rule->mark!=skb->mark) || (rule->cgroup && rule->cgroup!=cgroup) || (rule->cookie && rule->cookie!=key.cookie)) continue;
        service=rule->service; classid=rule->classid;
        break;
    }
    add(FC_PARSED,1);
    if (classid) {
        add(FC_CLASSIFIED,1);
        if (policy->enforce) { skb->priority=classid; add(FC_APPLIED,1); }
    }
    __u64 now=bpf_ktime_get_ns();
    struct fc_flow_value *value=bpf_map_lookup_elem(&flows,&key);
    if (!value) {
        struct fc_flow_value initial={.first_ns=now,.last_ns=now,.cgroup=cgroup,
            .generation=policy->generation,.service=service,.classid=classid};
        long result=bpf_map_update_elem(&flows,&key,&initial,BPF_NOEXIST);
        if (!result) add(FC_CREATED,1);
        value=bpf_map_lookup_elem(&flows,&key);
        if (!value) { add(FC_MAP_FULL,1); return -1; }
    }
    __u64 generation=policy->generation;
    __u32 length=skb->len;
    bpf_spin_lock(&value->lock);
    if (!key.cookie && (flags&8) && (value->flags&3) && now>=value->last_ns) {
        value->first_ns=now; value->bytes=0; value->packets=0; value->flags=0;
    }
    value->bytes+=length;
    ++value->packets;
    if (now>=value->last_ns) {
        value->last_ns=now;
        value->generation=generation;
        value->service=service; value->classid=classid;
    }
    value->flags|=flags;
    bpf_spin_unlock(&value->lock);
    return -1;
unsupported:
    add(FC_UNSUPPORTED,1);
    return -1;
}
char LICENSE[] SEC("license")="GPL";
