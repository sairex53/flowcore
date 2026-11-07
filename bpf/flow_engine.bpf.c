// SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-only
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_endian.h>
#include "flow_engine.h"
#include "flow_parse.h"
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
} flows_v2 SEC(".maps");
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
    __u8 flags=0;
    int result=fc_parse(skb,&key,&flags);
    if (result) { add(FC_UNSUPPORTED,1);add(result,1);return -1; }
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
    struct fc_flow_value *value=bpf_map_lookup_elem(&flows_v2,&key);
    if (!value) {
        struct fc_flow_value initial={.first_ns=now,.last_ns=now,.cgroup=cgroup,
            .generation=policy->generation,.service=service,.classid=classid};
        long result=bpf_map_update_elem(&flows_v2,&key,&initial,BPF_NOEXIST);
        if (!result) add(FC_CREATED,1);
        value=bpf_map_lookup_elem(&flows_v2,&key);
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
}
char LICENSE[] SEC("license")="GPL";
