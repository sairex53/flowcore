#include <linux/types.h>
#include <bpf/bpf.h>
#include <bpf/libbpf.h>
#include <fcntl.h>
#include <signal.h>
#include <sys/prctl.h>
#include <unistd.h>
#include <array>
#include <charconv>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <thread>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include "observe_event.h"

namespace {
volatile sig_atomic_t stopped = 0;
void stop(int) { stopped = 1; }
struct Output {
    unsigned long long drops = 0;
    std::array<char, 4096> pending{};
    size_t size = 0;
    unsigned records = 0;

    void flush() {
        if (size && write(STDOUT_FILENO, pending.data(), size) != static_cast<ssize_t>(size))
            drops += records;
        size = records = 0;
    }

    void append(const char* data, int length, bool event = false) {
        if (length < 0 || length >= 512) {
            if (event) ++drops;
            return;
        }
        if (size + length > pending.size()) flush();
        std::memcpy(pending.data() + size, data, length);
        size += length;
        if (event) ++records;
    }
};
int consume(void* context, void* data, size_t size) {
    if (size != sizeof(flow_event)) return 0;
    auto& out = *static_cast<Output*>(context);
    const auto& e = *static_cast<const flow_event*>(data);
    char line[512];
    int length = std::snprintf(line, sizeof(line),
        "{\"type\":\"tcp\",\"time_ns\":%llu,\"cookie\":\"%016llx\",\"lifetime_ns\":%llu,"
        "\"flow_id\":%llu,\"origin_state\":%u,\"netns\":%u,\"sport\":%u,\"dport\":%u,\"family\":%u,\"kind\":%u,\"old\":%u,\"new\":%u}\n",
        e.time_ns, e.cookie, e.lifetime_ns, e.flow_id, static_cast<unsigned>(e.origin_state), e.netns, static_cast<unsigned>(e.sport),
        static_cast<unsigned>(e.dport), static_cast<unsigned>(e.family), static_cast<unsigned>(e.kind),
        static_cast<unsigned>(e.old_state), static_cast<unsigned>(e.new_state));
    out.append(line, length, true);
    return 0;
}
}

int main(int argc, char** argv) {
    try {
        const pid_t parent = getppid();
        if (prctl(PR_SET_PDEATHSIG, SIGTERM) || parent == 1 || parent != getppid())
            throw std::runtime_error("observer parent unavailable");
        if (argc != 4) throw std::runtime_error("usage: flowcore-bpf OBJECT NETNS_A NETNS_B");
        std::array<__u32, 2> namespaces{};
        for (size_t i = 0; i < namespaces.size(); ++i) {
            std::string_view value(argv[i + 2]);
            auto parsed = std::from_chars(value.begin(), value.end(), namespaces[i]);
            if (parsed.ec != std::errc{} || parsed.ptr != value.end() || !namespaces[i])
                throw std::runtime_error("invalid namespace inode");
        }
        using Object = std::unique_ptr<bpf_object, decltype(&bpf_object__close)>;
        Object object(bpf_object__open_file(argv[1], nullptr), &bpf_object__close);
        if (!object || bpf_object__load(object.get())) throw std::runtime_error("BPF load failed");
        const __u32 zero = 0;
        int scope_fd = bpf_object__find_map_fd_by_name(object.get(), "scope");
        int event_fd = bpf_object__find_map_fd_by_name(object.get(), "events");
        int lost_fd = bpf_object__find_map_fd_by_name(object.get(), "lost");
        if (scope_fd < 0 || event_fd < 0 || lost_fd < 0 ||
            bpf_map_update_elem(scope_fd, &zero, namespaces.data(), BPF_ANY))
            throw std::runtime_error("BPF scope setup failed");
        using Link = std::unique_ptr<bpf_link, decltype(&bpf_link__destroy)>;
        std::array<Link, 3> links{Link(nullptr, &bpf_link__destroy), Link(nullptr, &bpf_link__destroy),
                                  Link(nullptr, &bpf_link__destroy)};
        const std::array<const char*, 3> names{"tcp_state", "tcp_retransmit", "tcp_reset"};
        std::string hooks;
        for (size_t i = 0; i < names.size(); ++i) {
            auto* program = bpf_object__find_program_by_name(object.get(), names[i]);
            if (!program) continue;
            auto* link = bpf_program__attach(program);
            if (!link || libbpf_get_error(link)) continue;
            links[i].reset(link);
            if (!hooks.empty()) hooks += ',';
            hooks += '"' + std::string(names[i]) + '"';
        }
        if (hooks.empty()) throw std::runtime_error("no TCP raw tracepoint could attach");
        Output out;
        std::unique_ptr<ring_buffer, decltype(&ring_buffer__free)> ring(
            ring_buffer__new(event_fd, consume, &out, nullptr), &ring_buffer__free);
        if (!ring) throw std::runtime_error("BPF ring initialization failed");
        const int flags = fcntl(STDOUT_FILENO, F_GETFL);
        if (flags < 0 || fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK) < 0)
            throw std::runtime_error("cannot make output nonblocking");
        signal(SIGINT, stop);
        signal(SIGTERM, stop);
        signal(SIGPIPE, SIG_IGN);
        auto ready = "{\"type\":\"ready\",\"hooks\":[" + hooks + "]}\n";
        out.append(ready.data(), static_cast<int>(ready.size()));
        out.flush();
        auto next = std::chrono::steady_clock::now();
        while (!stopped) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            if (ring_buffer__consume(ring.get()) < 0)
                throw std::runtime_error("BPF ring consume failed");
            out.flush();
            if (std::chrono::steady_clock::now() < next) continue;
            next = std::chrono::steady_clock::now() + std::chrono::seconds(1);
            __u64 lost_events = 0;
            if (bpf_map_lookup_elem(lost_fd, &zero, &lost_events))
                throw std::runtime_error("BPF loss lookup failed");
            char line[512];
            int length = std::snprintf(line, sizeof(line),
                "{\"type\":\"loss\",\"kernel\":%llu,\"transport\":%llu}\n", lost_events, out.drops);
            out.append(line, length);
            out.flush();
        }
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "flowcore-bpf: %s\n", error.what());
        return 1;
    }
}
