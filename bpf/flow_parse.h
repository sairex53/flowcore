#ifndef FLOWCORE_FLOW_PARSE_H
#define FLOWCORE_FLOW_PARSE_H
/* Header-only decoding: no payload inspection, reassembly, checksum validation or TCP rewrite. */
static __always_inline int fc_parse(struct __sk_buff *skb,struct fc_flow_key *key,__u8 *flags) {
    __u16 ethernet=0;
    __u32 offset=14,end=0;
    if (bpf_skb_load_bytes(skb,12,&ethernet,2)) return FC_BAD_L2;
    if (skb->vlan_present) {
        __u16 protocol=bpf_ntohs((__u16)skb->vlan_proto);
        if (protocol!=0x8100 && protocol!=0x88a8) return FC_BAD_L2;
        key->vlan_proto[0]=protocol;key->vlan_id[0]=skb->vlan_tci&0xfff;key->vlan_depth=1;
    }
    #pragma unroll
    for (int i=0;i<2;++i) {
        if (ethernet!=bpf_htons(0x8100) && ethernet!=bpf_htons(0x88a8)) break;
        if (key->vlan_depth>=2) return FC_BAD_L2;
        struct { __be16 tci,protocol; } vlan;
        if (bpf_skb_load_bytes(skb,offset,&vlan,sizeof(vlan))) return FC_BAD_L2;
        if (!key->vlan_depth) {
            key->vlan_proto[0]=bpf_ntohs(ethernet);key->vlan_id[0]=bpf_ntohs(vlan.tci)&0xfff;
        } else {
            key->vlan_proto[1]=bpf_ntohs(ethernet);key->vlan_id[1]=bpf_ntohs(vlan.tci)&0xfff;
        }
        ++key->vlan_depth;ethernet=vlan.protocol;offset+=4;
    }
    if (ethernet==bpf_htons(0x0800)) {
        struct iphdr ip;
        if (bpf_skb_load_bytes(skb,offset,&ip,sizeof(ip)) || ip.version!=4 || ip.ihl<5) return FC_BAD_L3;
        if (ip.frag_off & bpf_htons(0x3fff)) return FC_FRAGMENT;
        __u32 size=bpf_ntohs(ip.tot_len),header=ip.ihl*4;
        if (size<header || offset+size>skb->len) return FC_BAD_L3;
        end=offset+size;
        key->family=2;key->protocol=ip.protocol;
        __builtin_memcpy(key->source,&ip.saddr,4);
        __builtin_memcpy(key->destination,&ip.daddr,4);
        offset+=header;
    } else if (ethernet==bpf_htons(0x86dd)) {
        struct ipv6hdr ip;
        if (bpf_skb_load_bytes(skb,offset,&ip,sizeof(ip)) || ip.version!=6 ||
            !ip.payload_len || offset+40+bpf_ntohs(ip.payload_len)>skb->len) return FC_BAD_L3;
        end=offset+40+bpf_ntohs(ip.payload_len);
        key->family=10;key->protocol=ip.nexthdr;
        __builtin_memcpy(key->source,&ip.saddr,16);
        __builtin_memcpy(key->destination,&ip.daddr,16);
        offset+=40;
        __u32 extension_bytes=0;
        #pragma unroll
        for (int i=0;i<6;++i) {
            if (key->protocol!=0 && key->protocol!=43 && key->protocol!=60 && key->protocol!=51) break;
            struct { __u8 next,length; } extension;
            if (offset+2>end || bpf_skb_load_bytes(skb,offset,&extension,2)) return FC_BAD_L3;
            int authentication=key->protocol==51;
            __u32 length=authentication?(extension.length+2)*4:(extension.length+1)*8;
            if (authentication && length<12) return FC_BAD_L3;
            extension_bytes+=length;
            if (length<8 || extension_bytes>256) return FC_EXTENSION_LIMIT;
            if (offset+length>end) return FC_BAD_L3;
            key->protocol=extension.next;offset+=length;
        }
        if (key->protocol==44) return FC_FRAGMENT;
        if (key->protocol==0 || key->protocol==43 || key->protocol==60 || key->protocol==51)
            return FC_EXTENSION_LIMIT;
    } else return FC_PROTOCOL;
    if (key->protocol==6) {
        struct tcphdr tcp;
        if (offset+sizeof(tcp)>end || bpf_skb_load_bytes(skb,offset,&tcp,sizeof(tcp)) ||
            tcp.doff<5 || offset+tcp.doff*4>end) return FC_BAD_L4;
        key->sport=bpf_ntohs(tcp.source);key->dport=bpf_ntohs(tcp.dest);
        *flags=(tcp.fin?1:0)|(tcp.rst?2:0)|(tcp.syn?4:0)|((tcp.syn && !tcp.ack)?8:0);
    } else if (key->protocol==17) {
        struct udphdr udp;
        if (offset+sizeof(udp)>end || bpf_skb_load_bytes(skb,offset,&udp,sizeof(udp)) ||
            bpf_ntohs(udp.len)<8 || offset+bpf_ntohs(udp.len)>end) return FC_BAD_L4;
        key->sport=bpf_ntohs(udp.source);key->dport=bpf_ntohs(udp.dest);
    } else return FC_PROTOCOL; /* ESP/No-next/ICMP/etc. never imply a transport tuple. */
    return 0;
}
#endif
