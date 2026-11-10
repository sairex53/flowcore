#include <linux/types.h>
#include <linux/nsfs.h>
#include <linux/capability.h>
#include <sys/syscall.h>
#include <sys/resource.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <arpa/inet.h>
#include <net/if.h>
#include <sched.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/prctl.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <unistd.h>
#include <array>
#include <algorithm>
#include <charconv>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <memory>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>
#include "flow_engine.h"
namespace {
static_assert(FC_FLOW_ABI==2 && sizeof(fc_flow_key)==64 && sizeof(fc_flow_value)==72,
              "flow map ABI changed: version the map and exporter together");
volatile sig_atomic_t stopped=0;
void stop(int) { stopped=1; }
void require(bool yes,const char* why) { if (!yes) throw std::runtime_error(why); }
void validate(bool yes,const char* why) { if (!yes) throw std::invalid_argument(why); }
unsigned number(const char* text) {
    unsigned result=0; std::string_view input(text);
    auto parsed=std::from_chars(input.begin(),input.end(),result);
    require(parsed.ec==std::errc{} && parsed.ptr==input.end(),"invalid integer");
    return result;
}
struct Observation { fc_flow_key key;fc_flow_value value; };
struct Engine {
    int policy_fd,flow_fd,total_fd;
    unsigned capacity,cpus;
    __u64 generation=0,netns,expired=0,output_lost=0;
    bool observe;
    std::vector<__u64> cpu_values;
    __u64 session=std::chrono::duration_cast<std::chrono::nanoseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
    std::vector<Observation> frozen{};
    __u64 snapshot_id=0,captured_ns=0;
    std::chrono::steady_clock::time_point expires{};
    std::string flow_json(const Observation& observation) {
        const auto& key=observation.key;const auto& value=observation.value;
        char source[INET6_ADDRSTRLEN]{},destination[INET6_ADDRSTRLEN]{};
        require(inet_ntop(key.family,key.source,source,sizeof(source)) &&
                inet_ntop(key.family,key.destination,destination,sizeof(destination)),"invalid flow family");
        std::ostringstream out;
        out<<"{\"type\":\"flow\",\"session_ns\":"<<session<<",\"netns\":"<<netns<<",\"ifindex\":"<<key.ifindex
           <<",\"cookie\":\""<<key.cookie<<"\",\"family\":"<<unsigned(key.family)
           <<",\"protocol\":"<<unsigned(key.protocol)<<",\"source\":\""<<source
           <<"\",\"destination\":\""<<destination<<"\",\"sport\":"<<key.sport<<",\"dport\":"<<key.dport
           <<",\"first_ns\":"<<value.first_ns<<",\"last_ns\":"<<value.last_ns
           <<",\"bytes\":"<<value.bytes<<",\"packets\":"<<value.packets<<",\"cgroup\":"<<value.cgroup
           <<",\"generation\":"<<value.generation<<",\"service\":"<<value.service
           <<",\"classid\":"<<value.classid<<",\"tcp_wire_flags\":"<<value.flags
           <<",\"vlan_depth\":"<<unsigned(key.vlan_depth)
           <<",\"vlan_id\":["<<key.vlan_id[0]<<','<<key.vlan_id[1]
           <<"],\"vlan_proto\":["<<key.vlan_proto[0]<<','<<key.vlan_proto[1]<<"]}";
        return out.str();
    }
    void page(__u64 id,unsigned cursor) {
        validate(id && id==snapshot_id && std::chrono::steady_clock::now()<expires &&
                 cursor<=frozen.size() && cursor%FC_FLOW_PAGE==0,"snapshot/cursor unavailable");
        unsigned next=std::min<std::size_t>(cursor+FC_FLOW_PAGE,frozen.size());
        std::ostringstream out;
        out<<"{\"type\":\"page\",\"protocol\":2,\"snapshot\":"<<snapshot_id
           <<",\"session_ns\":"<<session<<",\"captured_ns\":"<<captured_ns
           <<",\"cursor\":"<<cursor<<",\"total\":"<<frozen.size()<<",\"next\":";
        if (next<frozen.size()) out<<next;else out<<"null";
        out<<",\"flows\":[";
        for (unsigned i=cursor;i<next;++i) { if (i!=cursor) out<<',';out<<flow_json(frozen[i]); }
        out<<"]}\n";
        output(out.str());
    }
    void page_command(const std::string& command) {
        validate(command.find_first_not_of("page 0123456789")==std::string::npos,"invalid page characters");
        std::istringstream input(command);
        std::string name,extra;__u64 id;unsigned cursor;
        validate(bool(input>>name>>id>>cursor) && name=="page" && !(input>>extra),"invalid page command");
        page(id,cursor);
    }
    void output(const std::string& text) {
        require(text.size()<4096,"output bound exceeded");
        ssize_t size;
        do { size=write(STDOUT_FILENO,text.data(),text.size()); } while (size<0 && errno==EINTR);
        if (size!=static_cast<ssize_t>(text.size())) ++output_lost;
    }
    void policy(const std::string& line) {
        validate(line.find_first_not_of("set 0123456789")==std::string::npos,"invalid command characters");
        std::istringstream input(line);
        std::string command,extra;
        fc_policy next{};
        next.netns=netns; next.enforce=!observe;
        validate(bool(input>>command>>next.generation>>next.count) && command=="set" &&
            next.generation>generation && next.count<=FC_FLOW_RULES,"invalid policy generation/count");
        for (unsigned i=0;i<next.count;++i) {
            unsigned proto,port; auto& rule=next.rules[i];
            validate(bool(input>>proto>>port>>rule.service>>rule.classid>>rule.mark>>rule.cgroup>>rule.cookie) &&
                (proto==6 || proto==17) && port<=65535 && rule.service && rule.service<=16 &&
                (rule.classid==0x10010 || rule.classid==0x10011 ||
                 rule.classid==0x10012 || rule.classid==0x10020),"invalid policy rule");
            rule.protocol=proto; rule.dport=port;
        }
        validate(!(input>>extra),"unexpected policy data");
        __u32 zero=0;
        require(!bpf_map_update_elem(policy_fd,&zero,&next,BPF_ANY),"atomic policy update failed");
        fc_policy actual{};
        require(!bpf_map_lookup_elem(policy_fd,&zero,&actual) &&
                !std::memcmp(&next,&actual,sizeof(next)),"policy verification failed");
        generation=next.generation;
        output("{\"type\":\"policy\",\"generation\":"+std::to_string(generation)+"}\n");
    }
    void snapshot(bool dump) {
        std::array<__u64,FC_STAT_COUNT> stats{};
        for (__u32 i=0;i<FC_STAT_COUNT;++i) {
            require(!bpf_map_lookup_elem(total_fd,&i,cpu_values.data()),"counter read failed");
            for (unsigned c=0;c<cpus;++c) stats[i]+=cpu_values[c];
        }
        timespec ts{}; clock_gettime(CLOCK_MONOTONIC,&ts);
        __u64 now=static_cast<__u64>(ts.tv_sec)*1000000000ULL+ts.tv_nsec;
        if (dump) { frozen.clear();frozen.reserve(capacity); }
        fc_flow_key key{},next{};
        std::vector<fc_flow_key> keys;
        keys.reserve(capacity);
        while (keys.size()<capacity && !bpf_map_get_next_key(flow_fd,keys.empty()?nullptr:&key,&next)) {
            key=next; keys.push_back(key);
        }
        unsigned entries=0;
        for (const auto& key:keys) {
            fc_flow_value value{};
            if (bpf_map_lookup_elem_flags(flow_fd,&key,&value,BPF_F_LOCK)) continue;
            if (now>value.last_ns && now-value.last_ns>((value.flags&3)?2ULL:15ULL)*1000000000ULL) {
                if (!bpf_map_delete_elem(flow_fd,&key)) ++expired;
                continue;
            }
            ++entries;
            if (dump) frozen.push_back({key,value});
        }
        std::ostringstream out;
        out<<"{\"type\":\"stats\",\"dump\":"<<(dump?"true":"false")<<",\"session_ns\":"<<session<<",\"time_ns\":"<<now<<",\"generation\":"<<generation
           <<",\"entries_observed\":"<<entries<<",\"capacity\":"<<capacity<<",\"expired\":"<<expired
           <<",\"output_lost\":"<<output_lost<<",\"packets\":"<<stats[FC_PACKETS]
           <<",\"bytes\":"<<stats[FC_BYTES]<<",\"parsed\":"<<stats[FC_PARSED]
           <<",\"classified\":"<<stats[FC_CLASSIFIED]<<",\"applied\":"<<stats[FC_APPLIED]<<",\"unsupported\":"<<stats[FC_UNSUPPORTED]
           <<",\"map_full_packets\":"<<stats[FC_MAP_FULL]<<",\"created\":"<<stats[FC_CREATED]
           <<",\"gso_segments\":"<<stats[FC_GSO_SEGMENTS]
           <<",\"bad_l2\":"<<stats[FC_BAD_L2]<<",\"bad_l3\":"<<stats[FC_BAD_L3]
           <<",\"bad_l4\":"<<stats[FC_BAD_L4]<<",\"fragments\":"<<stats[FC_FRAGMENT]
           <<",\"extension_limit\":"<<stats[FC_EXTENSION_LIMIT]<<",\"other_protocol\":"<<stats[FC_PROTOCOL]<<"}\n";
        output(out.str());
        if (dump) {
            require(++snapshot_id!=0,"snapshot identifier exhausted");
            timespec completed{};require(!clock_gettime(CLOCK_MONOTONIC,&completed),"snapshot clock failed");
            captured_ns=static_cast<__u64>(completed.tv_sec)*1000000000ULL+completed.tv_nsec;
            expires=std::chrono::steady_clock::now()+std::chrono::seconds(10);
            page(snapshot_id,0);
        }
    }
};
}
int main(int argc,char** argv) {
    try {
        require(argc==5 || argc==6,"usage: flowcore-flow-engine OBJECT NSFD fc0 CAPACITY [observe]");
        require(argc==5 || std::string_view(argv[5])=="observe","unsupported mode");
        require(std::string_view(argv[3])=="fc0","only owned fc0 supported");
        const unsigned namespace_fd=number(argv[2]),capacity=number(argv[4]);
        require(capacity>=16 && capacity<=4096,"capacity must be 16..4096");
        struct stat before{},target{},host{};
        require(!stat("/proc/self/ns/net",&before) && !stat("/proc/1/ns/net",&host) &&
            !fstat(namespace_fd,&target) && ioctl(namespace_fd,NS_GET_NSTYPE)==CLONE_NEWNET &&
            target.st_ino!=before.st_ino && target.st_ino!=host.st_ino,"refusing host/current namespace");
        const pid_t parent=getppid();
        require(parent!=1 && !prctl(PR_SET_PDEATHSIG,SIGTERM) && getppid()==parent,"parent unavailable");
        require(!setns(namespace_fd,CLONE_NEWNET),"namespace entry failed");
        using Object=std::unique_ptr<bpf_object,decltype(&bpf_object__close)>;
        Object object(bpf_object__open_file(argv[1],nullptr),&bpf_object__close);
        require(bool(object),"BPF open failed");
        for (unsigned capability=0;capability<64;++capability) {
            if (capability==CAP_NET_ADMIN || capability==CAP_BPF) continue;
            require(!prctl(PR_CAPBSET_DROP,capability,0,0,0) || errno==EINVAL,"capability bounding failed");
        }
        __user_cap_header_struct header{_LINUX_CAPABILITY_VERSION_3,0};
        std::array<__user_cap_data_struct,2> caps{};
        for (unsigned capability:{CAP_NET_ADMIN,CAP_BPF}) {
            caps[capability/32].effective|=1U<<(capability%32);
            caps[capability/32].permitted|=1U<<(capability%32);
        }
        require(!syscall(SYS_capset,&header,caps.data()) &&
                !prctl(PR_SET_NO_NEW_PRIVS,1,0,0,0),"capability reduction failed");
        rlimit files{128,128};
        require(!setrlimit(RLIMIT_NOFILE,&files),"FD resource bound failed");
        auto* interfaces=if_nameindex();
        require(interfaces,"interface enumeration failed");
        unsigned iface=0,count=0;
        for (auto* it=interfaces;it->if_index;++it) {
            ++count;
            if (!std::strcmp(it->if_name,"fc0")) iface=it->if_index;
        }
        if_freenameindex(interfaces);
        require(iface && count==2 && if_nametoindex("lo"),"namespace must contain only lo/fc0");
        auto* flows=bpf_object__find_map_by_name(object.get(),"flows_v2");
        require(flows && bpf_map__key_size(flows)==sizeof(fc_flow_key) &&
            bpf_map__value_size(flows)==sizeof(fc_flow_value) &&
            !bpf_map__set_max_entries(flows,capacity),"flow ABI/capacity setup failed");
        require(!bpf_object__load(object.get()),"BPF load/verifier failed; use userspace fallback");
        int policy=bpf_object__find_map_fd_by_name(object.get(),"policies");
        int totals=bpf_object__find_map_fd_by_name(object.get(),"totals");
        int cpus=libbpf_num_possible_cpus();
        require(policy>=0 && totals>=0 && cpus>0 && cpus<=4096,"map setup failed");
        Engine engine{policy,bpf_map__fd(flows),totals,capacity,static_cast<unsigned>(cpus),
                      0,target.st_ino,0,0,argc==6,std::vector<__u64>(cpus)};
        // Initial disabled policy: unknown traffic remains unchanged.
        engine.policy("set 1 0");
        struct bpf_prog_query_opts query{}; query.sz=sizeof(query);
        require(!bpf_prog_query_opts(iface,BPF_TCX_EGRESS,&query) && !query.prog_cnt,"TCX conflict/query failed");
        bpf_tcx_opts options{}; options.sz=sizeof(options); options.expected_revision=query.revision;
        using Link=std::unique_ptr<bpf_link,decltype(&bpf_link__destroy)>;
        auto* program=bpf_object__find_program_by_name(object.get(),"flow_egress");
        require(program,"flow program missing");
        auto* attached=bpf_program__attach_tcx(program,iface,&options);
        require(attached && !libbpf_get_error(attached),"TCX attach failed");
        Link link(attached,&bpf_link__destroy);
        struct bpf_prog_query_opts owned{}; owned.sz=sizeof(owned);
        require(!bpf_prog_query_opts(iface,BPF_TCX_EGRESS,&owned) && owned.prog_cnt==1,"attachment verification failed");
        const __u64 revision=owned.revision;
        const auto check_scope = [&] {
            struct bpf_prog_query_opts current{}; current.sz=sizeof(current);
            require(if_nametoindex("fc0")==iface &&
                !bpf_prog_query_opts(iface,BPF_TCX_EGRESS,&current) &&
                current.prog_cnt==1 && current.revision==revision,"interface/TCX ownership changed");
        };
        struct stat out{}; require(!fstat(STDOUT_FILENO,&out) && S_ISFIFO(out.st_mode) && fpathconf(STDOUT_FILENO,_PC_PIPE_BUF)>=4096,"output must be an atomic pipe");
        int flags=fcntl(STDOUT_FILENO,F_GETFL);
        require(flags>=0 && !fcntl(STDOUT_FILENO,F_SETFL,flags|O_NONBLOCK),"output setup failed");
        int input_flags=fcntl(STDIN_FILENO,F_GETFL);
        require(input_flags>=0 && !fcntl(STDIN_FILENO,F_SETFL,input_flags|O_NONBLOCK),"input setup failed");
        signal(SIGINT,stop); signal(SIGTERM,stop); signal(SIGPIPE,SIG_IGN);
        engine.output("{\"type\":\"ready\",\"protocol\":2,\"netns\":"+std::to_string(target.st_ino)+",\"ifindex\":"+std::to_string(iface)+"}\n");
        auto next=std::chrono::steady_clock::now();
        std::string pending;
        while (!stopped) {
            pollfd input{STDIN_FILENO,POLLIN,0};
            int ready=poll(&input,1,100);
            if (ready>0 && (input.revents&(POLLIN|POLLHUP))) {
                char data[1024]; auto size=read(STDIN_FILENO,data,sizeof(data));
                if (size==0) break;
                if (size>0) pending.append(data,size);
                require(pending.size()<=4096,"command exceeds 4096 bytes");
                while (true) {
                    auto end=pending.find('\n');
                    if (end==std::string::npos) break;
                    auto command=pending.substr(0,end); pending.erase(0,end+1);
                    try {
                        if (command=="dump") engine.snapshot(true);
                        else if (command.starts_with("page ")) engine.page_command(command);
                        else {
                            check_scope();
                            engine.policy(command);
                        }
                    } catch (const std::invalid_argument&) { engine.output("{\"type\":\"error\",\"error\":\"operation rejected\"}\n"); }
                }
            }
            if (std::chrono::steady_clock::now()>=next) {
                check_scope();
                if (std::chrono::steady_clock::now()>=engine.expires) engine.frozen.clear();
                engine.snapshot(false); next=std::chrono::steady_clock::now()+std::chrono::seconds(1);
            }
        }
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr,"flowcore-flow-engine: %s\n",error.what());
        return 1;
    }
}
