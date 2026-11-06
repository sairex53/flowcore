#include <linux/bpf.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <random>
#include <stdexcept>
#include <string>
#include <vector>
#include "flow_engine.h"
namespace {
void check(bool success,const char* why) { if (!success) throw std::runtime_error(why); }
using Bytes=std::vector<unsigned char>;
void word(Bytes& bytes,unsigned offset,unsigned value) { bytes.at(offset)=value>>8;bytes.at(offset+1)=value; }
Bytes frame(bool ipv6,bool tcp,unsigned tags=0,unsigned extensions=0) {
    unsigned l2=14+4*tags,l3=ipv6?40:20,transport=tcp?20:8;
    Bytes bytes(l2+l3+8*extensions+transport,0);
    std::fill_n(bytes.begin(),12,0x42);
    word(bytes,12,tags?0x88a8:(ipv6?0x86dd:0x0800));
    for (unsigned i=0;i<tags;++i) {
        word(bytes,14+4*i,100+i);
        word(bytes,16+4*i,i+1<tags?0x8100:(ipv6?0x86dd:0x0800));
    }
    if (ipv6) {
        bytes[l2]=0x60;word(bytes,l2+4,8*extensions+transport);
        bytes[l2+6]=extensions?60:(tcp?6:17);bytes[l2+7]=64;
        bytes[l2+8]=0x20;bytes[l2+9]=1;bytes[l2+23]=1;
        bytes[l2+24]=0x20;bytes[l2+25]=1;bytes[l2+39]=2;
        for (unsigned i=0;i<extensions;++i) bytes[l2+40+8*i]=i+1<extensions?60:(tcp?6:17);
    } else {
        bytes[l2]=0x45;word(bytes,l2+2,l3+transport);
        bytes[l2+8]=64;bytes[l2+9]=tcp?6:17;
        bytes[l2+12]=10;bytes[l2+15]=1;bytes[l2+16]=10;bytes[l2+19]=2;
    }
    unsigned off=l2+l3+8*extensions;
    word(bytes,off,12345);word(bytes,off+2,9000);
    if (tcp) { bytes[off+12]=0x50;bytes[off+13]=2; }
    else word(bytes,off+4,8);
    return bytes;
}
}
int main(int argc,char** argv) {
    try {
        check(argc==2,"usage: flow_parser_kernel OBJECT (test-only: no attachment)");
        using Object=std::unique_ptr<bpf_object,decltype(&bpf_object__close)>;
        Object object(bpf_object__open_file(argv[1],nullptr),&bpf_object__close);
        check(object && !bpf_object__load(object.get()),"test object/verifier load failed");
        int policy=bpf_object__find_map_fd_by_name(object.get(),"policies");
        int flows=bpf_object__find_map_fd_by_name(object.get(),"flows_v2");
        auto* program=bpf_object__find_program_by_name(object.get(),"flow_egress");
        check(policy>=0 && flows>=0 && program,"fixture maps/program missing");
        fc_policy configuration{};configuration.generation=1;configuration.count=2;configuration.enforce=1;
        for (unsigned i=0;i<2;++i) {
            configuration.rules[i].protocol=i?17:6;
            configuration.rules[i].dport=9000;configuration.rules[i].service=1;
            configuration.rules[i].classid=0x10010;
        }
        __u32 zero=0;check(!bpf_map_update_elem(policy,&zero,&configuration,BPF_ANY),"fixture policy failed");
        unsigned cases=0,fuzz=0;
        auto run=[&](Bytes bytes,bool expected,unsigned tags=0) {
            fc_flow_key key{};
            while (!bpf_map_get_next_key(flows,nullptr,&key)) check(!bpf_map_delete_elem(flows,&key),"fixture clear failed");
            __sk_buff input{},output{};input.priority=0x4321;
            bpf_test_run_opts options{};
            options.sz=sizeof(options);options.data_in=bytes.data();options.data_size_in=bytes.size();
            options.ctx_in=&input;options.ctx_size_in=sizeof(input);
            options.ctx_out=&output;options.ctx_size_out=sizeof(output);options.repeat=1;
            check(!bpf_prog_test_run_opts(bpf_program__fd(program),&options),"kernel test-run failed");
            check(options.retval==0xffffffffU,"classifier did not return TCX_NEXT");
            check(output.priority==(expected?0x10010U:0x4321U),"incorrect class/default priority");
            int result=bpf_map_get_next_key(flows,nullptr,&key);
            check(expected?!result:result<0,"unexpected flow retention");
            if (expected) {
                fc_flow_value value{};
                check(!bpf_map_lookup_elem_flags(flows,&key,&value,BPF_F_LOCK),"fixture read failed");
                check(key.dport==9000 && key.sport==12345 && key.vlan_depth==tags &&
                    value.classid==0x10010 && value.service==1 && value.packets==1,"incorrect flow fields");
                for (unsigned i=0;i<tags;++i) check(key.vlan_id[i]==100+i,"incorrect VLAN identity");
            }
            ++cases;
        };
        for (bool ipv6:{false,true}) for (bool tcp:{false,true})
            for (unsigned tags=0;tags<=2;++tags) run(frame(ipv6,tcp,tags),true,tags);
        for (unsigned count=1;count<=6;++count) run(frame(true,false,0,count),true);
        for (unsigned next:{0U,43U}) {
            auto extension=frame(true,false,0,1);extension[20]=next;run(extension,true);
        }
        auto authentication=frame(true,false);
        authentication.insert(authentication.begin()+54,16,0);
        authentication[20]=51;authentication[54]=17;authentication[55]=2;
        word(authentication,18,24);run(authentication,true);
        authentication[55]=0;run(authentication,false);
        run(frame(true,false,0,7),false);
        run(frame(false,false,3),false);
        auto bytes=frame(false,true);
        word(bytes,16,28); // Real IPv4 datagram ends before the padded TCP header.
        bytes.resize(100,0);run(bytes,false);
        bytes=frame(false,false);word(bytes,20,0x2000);run(bytes,false);
        bytes=frame(true,false);bytes[20]=44;run(bytes,false);
        bytes=frame(false,false);word(bytes,38,100);run(bytes,false);
        bytes=frame(true,false,0,1);bytes[55]=40;run(bytes,false);
        bytes=frame(true,false,0,1);bytes[55]=255;run(bytes,false);
        // Every truncation is malformed; Ethernet padding must not supply L4 bytes.
        for (bool ipv6:{false,true}) {
            auto full=frame(ipv6,true);
            for (unsigned length=14;length<full.size();++length) {
                // The kernel test harness itself requires a minimally sized IP header.
                if (length<14+(ipv6?40:20)) continue;
                auto short_packet=full;short_packet.resize(length);run(short_packet,false);
            }
        }
        std::mt19937 random(771);
        for (unsigned i=0;i<1000;++i) {
            auto packet=frame(false,false);
            word(packet,16,static_cast<unsigned>(random()%20)); // total length < IPv4 header
            run(packet,false);++fuzz;
        }
        std::printf("{\"kernel_fixture_cases\":%u,\"malformed_fixtures\":%u,\"wire_packets\":0}\n",cases,fuzz);
        return 0;
    } catch (const std::exception& error) { std::fprintf(stderr,"%s (errno=%d)\n",error.what(),errno);return 1; }
}
