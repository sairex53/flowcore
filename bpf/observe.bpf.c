// SPDX-License-Identifier: Apache-2.0 OR GPL-2.0-only
#include "vmlinux.h"
#include <bpf/bpf_core_read.h>
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_endian.h>
#include "observe_event.h"

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u32[2]);
} scope SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_RINGBUF);
    __uint(max_entries, 1 << 18);
} events SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} lost SEC(".maps");

struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u64);
} sequence SEC(".maps");

struct flow_start {
    __u64 time_ns;
    __u64 id;
    __u8 state;
};

/* The pointer is private to the map, never emitted as an identity. */
struct {
    __uint(type, BPF_MAP_TYPE_LRU_HASH);
    __uint(max_entries, 4096);
    __type(key, __u64);
    __type(value, struct flow_start);
} starts SEC(".maps");

static __always_inline int record(struct sock *sk, __u8 kind, __u8 old, __u8 next)
{
    if (!sk)
        return 0;
    __u32 zero = 0;
    __u32 (*filter)[2] = bpf_map_lookup_elem(&scope, &zero);
    __u32 ns = BPF_CORE_READ(sk, __sk_common.skc_net.net, ns.inum);
    if (!filter || !ns || (ns != (*filter)[0] && ns != (*filter)[1]))
        return 0;
    __u16 family = BPF_CORE_READ(sk, __sk_common.skc_family);
    if (family != 2 && family != 10)
        return 0;
    __u64 now = bpf_ktime_get_ns();
    __u64 key = (__u64)sk;
    __u64 duration = 0;
    struct flow_start *start = bpf_map_lookup_elem(&starts, &key);
    if (kind == 1 && (next == 2 || next == 3 || (next == 1 && !start))) {
        __u64 *seq = bpf_map_lookup_elem(&sequence, &zero);
        if (seq) {
            struct flow_start created = { .time_ns = now, .id = __sync_fetch_and_add(seq, 1) + 1, .state = next };
            bpf_map_update_elem(&starts, &key, &created, BPF_ANY);
            start = bpf_map_lookup_elem(&starts, &key);
        }
    }
    __u64 flow_id = start ? start->id : 0;
    __u8 origin = start ? start->state : 0;
    if (kind == 1 && (next == 7 || next == 6)) {
        if (start && now >= start->time_ns)
            duration = now - start->time_ns;
        bpf_map_delete_elem(&starts, &key);
    }
    struct flow_event *event = bpf_ringbuf_reserve(&events, sizeof(*event), 0);
    if (!event) {
        __u64 *counter = bpf_map_lookup_elem(&lost, &zero);
        if (counter)
            __sync_fetch_and_add(counter, 1);
        return 0;
    }
    __builtin_memset(event, 0, sizeof(*event));
    event->time_ns = now;
    event->cookie = BPF_CORE_READ(sk, __sk_common.skc_cookie.counter);
    event->lifetime_ns = duration;
    event->flow_id = flow_id;
    event->origin_state = origin;
    event->netns = ns;
    event->sport = BPF_CORE_READ(sk, __sk_common.skc_num);
    event->dport = bpf_ntohs(BPF_CORE_READ(sk, __sk_common.skc_dport));
    event->family = family;
    if (family == 2) {
        bpf_core_read(event->saddr, 4, &sk->__sk_common.skc_rcv_saddr);
        bpf_core_read(event->daddr, 4, &sk->__sk_common.skc_daddr);
    } else {
        bpf_core_read(event->saddr, sizeof(event->saddr), &sk->__sk_common.skc_v6_rcv_saddr);
        bpf_core_read(event->daddr, sizeof(event->daddr), &sk->__sk_common.skc_v6_daddr);
    }
    event->kind = kind;
    event->old_state = old;
    event->new_state = next;
    bpf_ringbuf_submit(event, BPF_RB_NO_WAKEUP);
    return 0;
}

SEC("raw_tracepoint/inet_sock_set_state")
int tcp_state(struct bpf_raw_tracepoint_args *ctx)
{
    struct sock *sk = (void *)ctx->args[0];
    if (!sk || BPF_CORE_READ_BITFIELD_PROBED(sk, sk_protocol) != 6)
        return 0;
    return record(sk, 1, ctx->args[1], ctx->args[2]);
}

SEC("raw_tracepoint/tcp_retransmit_skb")
int tcp_retransmit(struct bpf_raw_tracepoint_args *ctx)
{
    return record((void *)ctx->args[0], 2, 0, 0);
}

SEC("raw_tracepoint/tcp_receive_reset")
int tcp_reset(struct bpf_raw_tracepoint_args *ctx)
{
    return record((void *)ctx->args[0], 3, 0, 0);
}

char LICENSE[] SEC("license") = "GPL";
