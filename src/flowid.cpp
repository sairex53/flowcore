#include "flowid/session.hpp"

#include <openssl/err.h>
#include <openssl/pem.h>
#include <openssl/ssl.h>
#include <openssl/x509v3.h>

#include <arpa/inet.h>
#include <fcntl.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cerrno>
#include <charconv>
#include <climits>
#include <cstring>
#include <deque>
#include <mutex>
#include <set>
#include <span>
#include <sstream>
#include <thread>
#include <tuple>
#include <utility>

namespace flowid {
namespace {
using Clock = std::chrono::steady_clock;
using Time = Clock::time_point;
using Bytes = std::vector<unsigned char>;
using View = std::span<const unsigned char>;
using namespace std::chrono_literals;
constexpr unsigned version = 1;
constexpr unsigned capabilities = 7;
constexpr unsigned char alpn[] = {8, 'f', 'l', 'o', 'w', 'i', 'd', '/', '1'};
constexpr std::size_t token_size = 32;
constexpr std::size_t max_frame = 4 + 8 + max_message_bytes;
constexpr std::uint64_t max_sequence = INT64_MAX;
constexpr int mptcp_protocol = 262;
constexpr int tcp_is_mptcp = 43;
enum class Op : unsigned char { open = 1, opened, resume, resumed, data, ack, end, ended, error };

[[noreturn]] void fail(Status status, const char* message) { throw Error(status, message); }

class Fd {
public:
    explicit Fd(int fd = -1) : fd_(fd) {}
    Fd(Fd&& other) noexcept : fd_(std::exchange(other.fd_, -1)) {}
    Fd& operator=(Fd&& other) noexcept {
        if (this != &other) {
            if (fd_ >= 0) ::close(fd_);
            fd_ = std::exchange(other.fd_, -1);
        }
        return *this;
    }
    Fd(const Fd&) = delete;
    ~Fd() { if (fd_ >= 0) ::close(fd_); }
    int get() const { return fd_; }
private:
    int fd_;
};

// OpenSSL's socket BIO may issue a write during reads and handshakes.
class SigpipeScope {
public:
    SigpipeScope() {
        sigemptyset(&mask_);
        sigaddset(&mask_, SIGPIPE);
        if (pthread_sigmask(SIG_BLOCK, &mask_, &old_) != 0)
            fail(Status::unavailable, "cannot protect TLS I/O from SIGPIPE");
        sigset_t pending;
        sigpending(&pending);
        consume_ = !sigismember(&old_, SIGPIPE) && !sigismember(&pending, SIGPIPE);
    }
    SigpipeScope(const SigpipeScope&) = delete;
    ~SigpipeScope() {
        if (consume_) {
            const timespec zero{};
            while (sigtimedwait(&mask_, nullptr, &zero) < 0 && errno == EINTR) {}
        }
        pthread_sigmask(SIG_SETMASK, &old_, nullptr);
    }
private:
    sigset_t mask_{}, old_{};
    bool consume_ = false;
};

bool valid_service(std::string_view value) {
    if (value.empty() || value.size() > 127) return false;
    std::size_t length = 0;
    char previous = '\0';
    for (char c : value) {
        if (c == '.') {
            if (length == 0 || previous == '-') return false;
            length = 0;
        } else {
            const bool alphanumeric = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9');
            if (!alphanumeric && (c != '-' || length == 0)) return false;
            if (++length > 63) return false;
        }
        previous = c;
    }
    return length != 0 && previous != '-';
}

template<class T>
T number(std::string_view input, T low, T high) {
    T out{};
    const auto [end, error] = std::from_chars(input.data(), input.data() + input.size(), out);
    if (error != std::errc{} || end != input.data() + input.size() || out < low || out > high)
        fail(Status::invalid_configuration, "invalid endpoint directory number");
    return out;
}

struct Address {
    sockaddr_storage storage{};
    socklen_t length = 0;
    int family = AF_UNSPEC;
};

Address address(const Endpoint& endpoint) {
    if (!endpoint.port || endpoint.address.empty() || endpoint.address.size() > INET6_ADDRSTRLEN ||
        endpoint.address.find('\0') != std::string::npos)
        fail(Status::invalid_configuration, "invalid endpoint address or port");
    Address out;
    sockaddr_in ipv4{};
    sockaddr_in6 ipv6{};
    if (inet_pton(AF_INET, endpoint.address.c_str(), &ipv4.sin_addr) == 1) {
        ipv4.sin_family = AF_INET;
        ipv4.sin_port = htons(endpoint.port);
        out.family = AF_INET;
        out.length = sizeof(ipv4);
        std::memcpy(&out.storage, &ipv4, sizeof(ipv4));
    } else if (inet_pton(AF_INET6, endpoint.address.c_str(), &ipv6.sin6_addr) == 1) {
        ipv6.sin6_family = AF_INET6;
        ipv6.sin6_port = htons(endpoint.port);
        out.family = AF_INET6;
        out.length = sizeof(ipv6);
        std::memcpy(&out.storage, &ipv6, sizeof(ipv6));
    } else {
        fail(Status::invalid_configuration, "endpoint must use a numeric IPv4 or IPv6 address");
    }
    return out;
}

struct Directory {
    explicit Directory(std::filesystem::path path) : path(std::move(path)) {}
    std::filesystem::path path;
    std::mutex mutex;
    std::uint64_t generation = 0;
    std::string previous;

    std::vector<Endpoint> read(std::string_view service) {
        const std::lock_guard guard(mutex);
        Fd file(::open(path.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
        if (file.get() < 0) {
            if (errno == ENOENT) fail(Status::unavailable, "endpoint directory unavailable");
            fail(Status::invalid_configuration, "cannot open trusted endpoint directory");
        }
        struct stat info{};
        if (fstat(file.get(), &info) < 0 || !S_ISREG(info.st_mode) || info.st_nlink != 1 ||
            (info.st_uid != 0 && info.st_uid != geteuid()) || (info.st_mode & 0022) ||
            info.st_size <= 0 || info.st_size > 8192)
            fail(Status::invalid_configuration, "endpoint directory must be a bounded, owned regular file");
        std::array<char, 8193> raw{};
        std::size_t size = 0;
        while (size < raw.size()) {
            const auto count = ::read(file.get(), raw.data() + size, raw.size() - size);
            if (count < 0 && errno == EINTR) continue;
            if (count < 0) fail(Status::unavailable, "cannot read endpoint directory");
            if (count == 0) break;
            size += static_cast<std::size_t>(count);
        }
        if (size != static_cast<std::size_t>(info.st_size))
            fail(Status::unavailable, "endpoint directory changed while reading");
        const std::string content(raw.data(), size);
        std::istringstream stream(content);
        std::string line, magic, logical, revision, extra;
        if (!std::getline(stream, line)) fail(Status::invalid_configuration, "missing endpoint directory header");
        std::istringstream header(line);
        if (!(header >> magic >> logical >> revision) || header >> extra ||
            magic != "FLOWID-DIRECTORY/1" || logical != service || !valid_service(logical))
            fail(Status::invalid_configuration, "invalid endpoint directory header");
        const auto next = number<std::uint64_t>(revision, 1, max_sequence);
        if (next < generation || (next == generation && previous != content))
            fail(Status::invalid_configuration, "endpoint directory generation conflict");
        std::vector<Endpoint> endpoints;
        std::set<std::tuple<Transport, std::string, std::uint16_t>> seen;
        while (std::getline(stream, line)) {
            if (line.empty()) continue;
            std::istringstream row(line);
            std::string kind, ip, port, expires;
            if (!(row >> kind >> ip >> port >> expires) || row >> extra ||
                (kind != "tcp" && kind != "mptcp") || endpoints.size() == 16)
                fail(Status::invalid_configuration, "invalid endpoint row");
            Endpoint endpoint{ip, number<std::uint16_t>(port, 1, 65535),
                kind == "tcp" ? Transport::tcp : Transport::mptcp,
                std::chrono::system_clock::time_point{
                    std::chrono::seconds(number<std::int64_t>(expires, 1, 4'102'444'800))}};
            address(endpoint);
            if (!seen.emplace(endpoint.transport, endpoint.address, endpoint.port).second)
                fail(Status::invalid_configuration, "duplicate endpoint");
            endpoints.push_back(std::move(endpoint));
        }
        generation = next;
        previous = content;
        return endpoints;
    }
};

void wait_fd(int fd, short events, Time deadline) {
    for (;;) {
        const auto remaining = std::chrono::ceil<std::chrono::milliseconds>(deadline - Clock::now());
        if (remaining <= 0ms) fail(Status::timeout, "transport deadline expired");
        pollfd descriptor{fd, events, 0};
        const auto result = ::poll(&descriptor, 1, static_cast<int>(remaining.count()));
        if (result < 0 && errno == EINTR) continue;
        if (result == 0) fail(Status::timeout, "transport deadline expired");
        if (result < 0 || (descriptor.revents & POLLNVAL))
            fail(Status::unavailable, "transport poll failed");
        return;
    }
}

using Context = std::unique_ptr<SSL_CTX, decltype(&SSL_CTX_free)>;
using Tls = std::unique_ptr<SSL, decltype(&SSL_free)>;

Context context(const Options& options) {
    Fd key_file(::open(options.key.c_str(), O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC));
    struct stat key{};
    if (key_file.get() < 0 || fstat(key_file.get(), &key) < 0 || !S_ISREG(key.st_mode) ||
        key.st_nlink != 1 || key.st_size <= 0 || key.st_size > 65536 || (key.st_uid != 0 && key.st_uid != geteuid()) || (key.st_mode & 0077))
        fail(Status::invalid_configuration, "TLS private key must be an owned private file");
    const auto no_password = [](char*, int, int, void*) { return 0; };
    const std::unique_ptr<BIO, decltype(&BIO_free)> input(BIO_new_fd(key_file.get(), BIO_NOCLOSE), BIO_free);
    const std::unique_ptr<EVP_PKEY, decltype(&EVP_PKEY_free)> private_key(
        input ? PEM_read_bio_PrivateKey(input.get(), nullptr, no_password, nullptr) : nullptr, EVP_PKEY_free);
    Context ctx(SSL_CTX_new(TLS_client_method()), SSL_CTX_free);
    if (!ctx) fail(Status::invalid_configuration, "cannot allocate client TLS context");
    if (!private_key || SSL_CTX_set_min_proto_version(ctx.get(), TLS1_3_VERSION) != 1 ||
        SSL_CTX_load_verify_locations(ctx.get(), options.ca.c_str(), nullptr) != 1 ||
        SSL_CTX_use_certificate_chain_file(ctx.get(), options.certificate.c_str()) != 1 ||
        SSL_CTX_use_PrivateKey(ctx.get(), private_key.get()) != 1 ||
        SSL_CTX_check_private_key(ctx.get()) != 1)
        fail(Status::invalid_configuration, "cannot configure client TLS");
    SSL_CTX_set_verify(ctx.get(), SSL_VERIFY_PEER, nullptr);
    if (SSL_CTX_set_alpn_protos(ctx.get(), alpn, sizeof(alpn)) != 0)
        fail(Status::invalid_configuration, "cannot configure FLOWID ALPN");
    SSL_CTX_set_options(ctx.get(), SSL_OP_NO_TICKET | SSL_OP_IGNORE_UNEXPECTED_EOF);
    return ctx;
}

// The frame/session layer requires an authenticated ordered byte stream.
struct Channel {
    explicit Channel(Endpoint endpoint) : endpoint(std::move(endpoint)) {}
    virtual ~Channel() = default;
    virtual void write(View data, Time deadline) = 0;
    virtual void read(std::span<unsigned char> data, Time deadline) = 0;
    const Endpoint endpoint;
};

struct TlsChannel final : Channel {
    Fd fd;
    Tls ssl{nullptr, SSL_free};

    void check(int result, Time deadline, bool handshake = false) {
        const int error = SSL_get_error(ssl.get(), result);
        if (error == SSL_ERROR_WANT_READ || error == SSL_ERROR_WANT_WRITE) {
            wait_fd(fd.get(), error == SSL_ERROR_WANT_READ ? POLLIN : POLLOUT, deadline);
            return;
        }
        if (SSL_get_verify_result(ssl.get()) != X509_V_OK || (handshake && error == SSL_ERROR_SSL))
            fail(Status::authentication_failed, "TLS peer identity or handshake rejected");
        if (error == SSL_ERROR_SSL)
            fail(Status::protocol_error, "TLS record rejected");
        fail(Status::unavailable, "transport disconnected");
    }

    TlsChannel(SSL_CTX* context, std::string_view service, Endpoint endpoint, Time deadline)
        : Channel(std::move(endpoint)) {
        const auto peer = address(this->endpoint);
        const auto protocol = this->endpoint.transport == Transport::mptcp ? mptcp_protocol : IPPROTO_TCP;
        fd = Fd(::socket(peer.family, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, protocol));
        if (fd.get() < 0) {
            if (protocol == mptcp_protocol && (errno == EPROTONOSUPPORT || errno == EINVAL ||
                                              errno == ENOPROTOOPT || errno == EAFNOSUPPORT))
                fail(Status::unsupported_transport, "MPTCP socket unsupported");
            fail(Status::unavailable, "cannot create transport socket");
        }
        const int nodelay = 1;
        setsockopt(fd.get(), IPPROTO_TCP, TCP_NODELAY, &nodelay, sizeof(nodelay));
        if (::connect(fd.get(), reinterpret_cast<const sockaddr*>(&peer.storage), peer.length) < 0) {
            if (errno != EINPROGRESS && errno != EINTR)
                fail(Status::unavailable, "endpoint unreachable");
            wait_fd(fd.get(), POLLOUT, deadline);
            int error = 0;
            socklen_t size = sizeof(error);
            if (getsockopt(fd.get(), SOL_SOCKET, SO_ERROR, &error, &size) < 0 || error)
                fail(Status::unavailable, "endpoint connection failed");
        }
        if (protocol == mptcp_protocol) {
            int negotiated = 0;
            socklen_t size = sizeof(negotiated);
            if (getsockopt(fd.get(), IPPROTO_TCP, tcp_is_mptcp, &negotiated, &size) < 0 || negotiated != 1)
                fail(Status::unsupported_transport, "MPTCP was not negotiated");
        }
        const std::string name(service);
        ssl.reset(SSL_new(context));
        if (!ssl || SSL_set_fd(ssl.get(), fd.get()) != 1 || X509_VERIFY_PARAM_set1_host(SSL_get0_param(ssl.get()), name.c_str(), 0) != 1 ||
            SSL_set_tlsext_host_name(ssl.get(), name.c_str()) != 1)
            fail(Status::invalid_configuration, "cannot configure TLS service identity");
        SSL_set_hostflags(ssl.get(), X509_CHECK_FLAG_NO_WILDCARDS | X509_CHECK_FLAG_NEVER_CHECK_SUBJECT);
        SigpipeScope guard;
        for (;;) {
            if (Clock::now() >= deadline) fail(Status::timeout, "TLS handshake deadline");
            ERR_clear_error();
            const int result = SSL_connect(ssl.get());
            if (result == 1) break;
            check(result, deadline, true);
        }
        if (SSL_get_verify_result(ssl.get()) != X509_V_OK)
            fail(Status::authentication_failed, "TLS service identity rejected");
        const unsigned char* selected = nullptr;
        unsigned int length = 0;
        SSL_get0_alpn_selected(ssl.get(), &selected, &length);
        if (length != alpn[0] || !std::equal(selected, selected + length, alpn + 1))
            fail(Status::protocol_error, "peer did not negotiate FLOWID ALPN");
    }

    void write(View data, Time deadline) override {
        SigpipeScope guard;
        while (!data.empty()) {
            if (Clock::now() >= deadline) fail(Status::timeout, "TLS write deadline");
            std::size_t amount = 0;
            ERR_clear_error();
            const int result = SSL_write_ex(ssl.get(), data.data(), data.size(), &amount);
            if (result == 1) data = data.subspan(amount);
            else check(result, deadline);
        }
    }

    void read(std::span<unsigned char> data, Time deadline) override {
        SigpipeScope guard;
        while (!data.empty()) {
            if (Clock::now() >= deadline) fail(Status::timeout, "TLS read deadline");
            std::size_t amount = 0;
            ERR_clear_error();
            const int result = SSL_read_ex(ssl.get(), data.data(), data.size(), &amount);
            if (result == 1) data = data.subspan(amount);
            else check(result, deadline);
        }
    }
};

void append(Bytes& bytes, std::uint64_t number, unsigned width) {
    for (unsigned index = width; index > 0; --index)
        bytes.push_back(static_cast<unsigned char>(number >> ((index - 1) * 8)));
}

std::uint64_t decode(View bytes, std::size_t offset, unsigned width) {
    if (offset > bytes.size() || width > bytes.size() - offset)
        fail(Status::protocol_error, "short FLOWID frame");
    std::uint64_t value = 0;
    for (const auto byte : bytes.subspan(offset, width)) value = (value << 8) | byte;
    return value;
}

void send_frame(Channel& channel, Op op, View body, Time deadline) {
    Bytes frame;
    frame.reserve(8 + body.size());
    append(frame, 4 + body.size(), 4);
    frame.insert(frame.end(), {version, static_cast<unsigned char>(op), 0, 0});
    frame.insert(frame.end(), body.begin(), body.end());
    channel.write(frame, deadline);
}

Status wire_error(unsigned code) {
    switch (code) {
        case 1: return Status::protocol_error;
        case 2: return Status::authentication_failed;
        case 3: return Status::expired;
        case 4: return Status::sequence_conflict;
        case 5: return Status::quota;
        case 6: return Status::closed;
        case 7: return Status::unavailable; // a newer transport fenced this association
        case 8: return Status::unavailable; // state store temporarily unavailable
        default: return Status::protocol_error;
    }
}

Bytes receive_frame(Channel& channel, Op expected, Time deadline) {
    std::array<unsigned char, 4> prefix{};
    channel.read(prefix, deadline);
    const auto size = decode(prefix, 0, 4);
    if (size < 4 || size > max_frame) fail(Status::protocol_error, "invalid FLOWID frame size");
    Bytes packet(static_cast<std::size_t>(size));
    channel.read(packet, deadline);
    if (packet[0] != version || packet[2] || packet[3])
        fail(Status::protocol_error, "unsupported FLOWID version or flags");
    const auto opcode = static_cast<Op>(packet[1]);
    if (opcode == Op::error && packet.size() == 5)
        fail(wire_error(packet[4]), "FLOWID peer rejected the operation");
    if (opcode != expected) fail(Status::protocol_error, "unexpected FLOWID operation");
    return Bytes(packet.begin() + 4, packet.end());
}

bool retryable(Status status) {
    return status == Status::unavailable || status == Status::timeout ||
           status == Status::unsupported_transport;
}

} // namespace

struct Session::Impl {
    struct Message { std::uint64_t sequence; Bytes bytes; };

    Impl(std::string_view service, Options options)
        : service(service), options(std::move(options)), ctx(nullptr, SSL_CTX_free) {
        if (!valid_service(service) || !this->options.resolve ||
            this->options.operation_timeout < 10ms || this->options.operation_timeout > 30s ||
            this->options.attempt_timeout < 10ms || this->options.attempt_timeout > 5s ||
            (this->options.transport != Transport::tcp && this->options.transport != Transport::mptcp))
            fail(Status::invalid_configuration, "invalid FLOWID session options");
        ctx = context(this->options);
    }
    ~Impl() { OPENSSL_cleanse(token.data(), token.size()); }

    std::string service;
    Options options;
    Context ctx;
    std::unique_ptr<Channel> channel;
    std::array<unsigned char, token_size> token{};
    std::deque<Message> queue;
    std::size_t pending_bytes = 0;
    std::size_t endpoint_cursor = 0;
    std::uint64_t last_ack = 0, issued = 0, sent = 0, association = 0, reconnects = 0;
    bool established = false, closed = false, closing = false, end_attempted = false;
    Time expiry{};
    std::optional<Time> disconnected;
    std::optional<Status> terminal;
    std::function<void(const ReconnectEvent&)> callback;

    Time attempt(Time deadline) const { return std::min(deadline, Clock::now() + options.attempt_timeout); }

    void acknowledge(std::uint64_t committed) {
        if (committed < last_ack || committed > sent)
            fail(Status::protocol_error, "peer cursor contradicts sent sequence");
        last_ack = committed;
        while (!queue.empty() && queue.front().sequence <= last_ack) {
            pending_bytes -= queue.front().bytes.size();
            queue.pop_front();
        }
    }

    void disconnect() {
        channel.reset();
        if (established && !disconnected) disconnected = Clock::now();
    }

    void bind(Endpoint endpoint, Time deadline) {
        auto candidate = std::make_unique<TlsChannel>(ctx.get(), service, std::move(endpoint), deadline);
        Bytes body;
        append(body, capabilities, 2);
        append(body, service.size(), 1);
        body.insert(body.end(), service.begin(), service.end());
        const bool resumed = established;
        if (resumed) {
            body.insert(body.end(), token.begin(), token.end());
            append(body, last_ack, 8);
        }
        send_frame(*candidate, resumed ? Op::resume : Op::open, body, deadline);
        const auto response = receive_frame(*candidate, resumed ? Op::resumed : Op::opened, deadline);
        const std::size_t cursor = resumed ? 2 : 2 + token_size;
        if (response.size() != cursor + 20 || decode(response, 0, 2) != capabilities)
            fail(Status::protocol_error, "invalid negotiated FLOWID parameters");
        const auto committed = decode(response, cursor, 8);
        const auto epoch = decode(response, cursor + 8, 8);
        const auto ttl = decode(response, cursor + 16, 4);
        if (!epoch || epoch > max_sequence || epoch <= association || !ttl || ttl > 3600 ||
            (!resumed && (epoch != 1 || committed != 0)))
            fail(Status::protocol_error, "invalid FLOWID association or expiry");
        acknowledge(committed);
        const auto expires = Clock::now() + std::chrono::seconds(ttl);
        if (!resumed) {
            std::copy_n(response.begin() + 2, token.size(), token.begin());
            expiry = expires;
            established = true;
        } else {
            expiry = std::min(expiry, expires);
            ++reconnects;
        }
        association = epoch;
        channel = std::move(candidate);
    }

    void attach(Time deadline) {
        if (established && Clock::now() >= expiry)
            fail(Status::expired, "FLOWID session expired");
        auto endpoints = options.resolve(service);
        if (endpoints.size() > 16) fail(Status::invalid_configuration, "too many resolved endpoints");
        const auto current = std::chrono::system_clock::now();
        std::erase_if(endpoints, [&](const Endpoint& endpoint) {
            return endpoint.transport != options.transport || endpoint.expires <= current;
        });
        if (endpoints.empty()) fail(Status::unavailable, "no unexpired endpoint for selected transport");
        const auto next = endpoint_cursor++ % endpoints.size();
        bind(std::move(endpoints[next]), attempt(deadline));
    }

    template<class Operation>
    Status run(Operation operation) {
        if (terminal) return *terminal;
        if (closed) return Status::closed;
        const auto deadline = Clock::now() + options.operation_timeout;
        Status latest = Status::unavailable;
        auto pause = 25ms;
        while (Clock::now() < deadline) {
            bool notify = false;
            try {
                if (established && Clock::now() >= expiry)
                    fail(Status::expired, "FLOWID session expired");
                if (!channel) {
                    notify = established;
                    attach(deadline);
                }
            } catch (const Error& error) {
                latest = error.status();
                disconnect();
                if (!retryable(latest)) {
                    terminal = latest;
                    return latest;
                }
            }
            if (channel) {
                if (notify) {
                    const auto began = std::exchange(disconnected, std::nullopt);
                    if (callback) callback(ReconnectEvent{channel->endpoint, last_ack, association,
                        std::chrono::duration_cast<std::chrono::microseconds>(Clock::now() -
                            began.value_or(Clock::now()))});
                }
                try {
                    operation(deadline);
                    return Status::acknowledged;
                } catch (const Error& error) {
                    latest = error.status();
                    disconnect();
                    if (!retryable(latest)) {
                        terminal = latest;
                        return latest;
                    }
                }
            }
            std::this_thread::sleep_until(std::min(deadline, Clock::now() + pause));
            pause = std::min(250ms, pause * 2);
        }
        return latest;
    }

    FlushResult flush(bool check = false) {
        if (closing || closed) return {Status::closed, last_ack, queue.size()};
        if (queue.empty() && !check) return {terminal.value_or(Status::acknowledged), last_ack, 0};
        const auto status = run([&](Time deadline) {
            while (!queue.empty()) {
                const auto& message = queue.front();
                const auto sequence = message.sequence;
                Bytes body;
                body.reserve(8 + message.bytes.size());
                append(body, sequence, 8);
                body.insert(body.end(), message.bytes.begin(), message.bytes.end());
                sent = std::max(sent, sequence);
                const auto limit = attempt(deadline);
                send_frame(*channel, Op::data, body, limit);
                const auto reply = receive_frame(*channel, Op::ack, limit);
                if (reply.size() != 8 || decode(reply, 0, 8) < sequence)
                    fail(Status::protocol_error, "ACK did not cover the in-flight message");
                acknowledge(decode(reply, 0, 8));
            }
        });
        return {status, last_ack, queue.size()};
    }
};

Error::Error(Status status, const char* description) : std::runtime_error(description), status_(status) {}

std::string_view name(Status status) noexcept {
    switch (status) {
        case Status::acknowledged: return "acknowledged";
        case Status::unavailable: return "unavailable";
        case Status::timeout: return "timeout";
        case Status::authentication_failed: return "authentication_failed";
        case Status::expired: return "expired";
        case Status::closed: return "closed";
        case Status::sequence_conflict: return "sequence_conflict";
        case Status::protocol_error: return "protocol_error";
        case Status::unsupported_transport: return "unsupported_transport";
        case Status::quota: return "quota";
        case Status::invalid_configuration: return "invalid_configuration";
        case Status::pending: return "pending";
    }
    return "unknown";
}

std::string_view name(Transport transport) noexcept { return transport == Transport::tcp ? "tcp" : "mptcp"; }

Resolver file_directory(std::filesystem::path path) {
    auto directory = std::make_shared<Directory>(std::move(path));
    return [directory = std::move(directory)](std::string_view service) { return directory->read(service); };
}

Session::Session(std::unique_ptr<Impl> impl) : impl_(std::move(impl)) {}
Session::Session(Session&&) noexcept = default;
Session& Session::operator=(Session&&) noexcept = default;
Session::~Session() = default;

Session connect(std::string_view service, Options options) {
    auto impl = std::make_unique<Session::Impl>(service, std::move(options));
    const auto status = impl->run([](Time) {});
    if (status != Status::acknowledged) fail(status, "FLOWID session establishment failed");
    return Session(std::move(impl));
}

std::uint64_t Session::enqueue(std::string_view message) {
    auto& session = *impl_;
    if (session.terminal) fail(*session.terminal, "FLOWID session cannot accept messages");
    if (session.closing || session.closed) fail(Status::closed, "FLOWID session is closing");
    if (Clock::now() >= session.expiry) fail(Status::expired, "FLOWID session expired");
    if (message.empty() || message.size() > max_message_bytes ||
        session.queue.size() == max_pending_messages || message.size() > max_pending_bytes - session.pending_bytes ||
        session.issued == max_sequence)
        fail(Status::quota, "FLOWID outbox or message quota exceeded");
    const auto sequence = session.issued + 1;
    session.queue.push_back({sequence, Bytes(message.begin(), message.end())});
    session.issued = sequence;
    session.pending_bytes += message.size();
    return sequence;
}

Receipt Session::send(std::string_view message) {
    const auto sequence = enqueue(message);
    return {sequence, flush()};
}

FlushResult Session::flush() { return impl_->flush(); }

FlushResult Session::recover() {
    impl_->disconnect();
    return impl_->flush(true);
}

Status Session::close() {
    auto& session = *impl_;
    if (session.closed) return Status::acknowledged;
    if (!session.queue.empty()) return Status::pending;
    session.closing = true;
    auto status = session.run([&](Time deadline) {
        Bytes body;
        append(body, session.last_ack, 8);
        const auto limit = session.attempt(deadline);
        session.end_attempted = true;
        send_frame(*session.channel, Op::end, body, limit);
        const auto reply = receive_frame(*session.channel, Op::ended, limit);
        if (reply.size() != 8 || decode(reply, 0, 8) != session.last_ack)
            fail(Status::protocol_error, "invalid FLOWID termination cursor");
    });
    if (status == Status::acknowledged || (status == Status::closed && session.end_attempted)) {
        session.closed = true;
        session.disconnect();
        status = Status::acknowledged;
    }
    return status;
}

void Session::on_reconnect(std::function<void(const ReconnectEvent&)> callback) {
    impl_->callback = std::move(callback);
}

State Session::state() const {
    const auto& s = *impl_;
    return {static_cast<bool>(s.channel), s.closed, s.channel ? std::optional{s.channel->endpoint} : std::nullopt,
            s.last_ack, s.association, s.reconnects, s.queue.size(), s.pending_bytes};
}

} // namespace flowid
