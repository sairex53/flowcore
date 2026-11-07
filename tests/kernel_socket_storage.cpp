#include <bpf/bpf.h>
#include <bpf/btf.h>
#include <cerrno>
#include <cstdint>
#include <iostream>
#include <memory>
#include <stdexcept>
#include <sys/socket.h>
#include <unistd.h>

namespace {
void require(bool valid, const char* operation) {
    if (!valid) {
        throw std::runtime_error(operation);
    }
}

class Fd {
public:
    explicit Fd(int descriptor) : descriptor_(descriptor) {
        require(descriptor_ >= 0, "could not acquire descriptor");
    }

    ~Fd() { reset(); }
    Fd(const Fd&) = delete;
    Fd& operator=(const Fd&) = delete;

    int get() const { return descriptor_; }
    void reset() {
        if (descriptor_ >= 0) {
            close(descriptor_);
            descriptor_ = -1;
        }
    }

private:
    int descriptor_;
};

Fd udp() {
    return Fd(socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, 0));
}
}  // namespace

int main() {
    try {
        const std::unique_ptr<btf, decltype(&btf__free)> types(btf__new_empty(), btf__free);
        require(types != nullptr, "could not create BTF");
        const auto key_type = btf__add_int(types.get(), "socket_fd", sizeof(int), BTF_INT_SIGNED);
        const auto value_type = btf__add_int(types.get(), "opaque_hint", sizeof(std::uint64_t), 0);
        require(key_type > 0 && value_type > 0, "could not define map types");
        require(btf__load_into_kernel(types.get()) == 0, "could not load map BTF");

        bpf_map_create_opts options{};
        options.sz = sizeof(options);
        options.btf_fd = btf__fd(types.get());
        options.btf_key_type_id = key_type;
        options.btf_value_type_id = value_type;
        options.map_flags = BPF_F_NO_PREALLOC;
        const Fd map(bpf_map_create(BPF_MAP_TYPE_SK_STORAGE, "fc_intent_probe",
                                    sizeof(int), sizeof(std::uint64_t), 0, &options));

        auto original = udp();
        const auto independent = udp();
        Fd duplicate(dup(original.get()));
        const int first_fd = original.get();
        const int duplicate_fd = duplicate.get();
        const int independent_fd = independent.get();
        const std::uint64_t hint = 870491;
        std::uint64_t observed = 0;

        require(bpf_map_update_elem(map.get(), &first_fd, &hint, BPF_NOEXIST) == 0,
                "socket storage insertion failed");
        require(bpf_map_update_elem(map.get(), &duplicate_fd, &hint, BPF_NOEXIST) == -EEXIST,
                "duplicate descriptor acquired independent storage");
        require(bpf_map_lookup_elem(map.get(), &independent_fd, &observed) == -ENOENT,
                "hint exposed on an unrelated socket");

        original.reset();
        require(bpf_map_lookup_elem(map.get(), &duplicate_fd, &observed) == 0 && observed == hint,
                "hint did not survive through a live duplicate");
        duplicate.reset();

        const auto replacement = udp();
        const int replacement_fd = replacement.get();
        require(replacement_fd == first_fd, "fixture did not reuse the original descriptor number");
        require(bpf_map_lookup_elem(map.get(), &replacement_fd, &observed) == -ENOENT,
                "reused descriptor inherited a stale hint");
        std::cout << "{\"status\":\"pass\",\"live_duplicate\":true,\"socket_isolation\":true,"
                     "\"descriptor_reuse\":true,\"attached_programs\":0,\"pinned_maps\":0}\n";
    } catch (const std::exception& error) {
        std::cerr << "socket storage probe: " << error.what() << '\n';
        return 1;
    }
}
