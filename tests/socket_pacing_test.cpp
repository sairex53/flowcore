#include "flowcore/intent_v2.hpp"
#include "flowcore/socket_pacing.hpp"

#include <fcntl.h>
#include <poll.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <unistd.h>

#include <array>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <thread>

using namespace std::chrono_literals;
namespace p = flowcore::v2::pacing;

#define CHECK(expression) do { if (!(expression)) throw std::runtime_error(#expression); } while (false)

struct Fd {
    explicit Fd(int value) : fd(value) { CHECK(fd >= 0); }
    ~Fd() { close(fd); }
    Fd(const Fd&) = delete;
    const int fd;
};

struct Pair {
    Fd listener{socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, IPPROTO_TCP)};
    Fd sender{socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, IPPROTO_TCP)};
    Fd peer;
    explicit Pair(int receive_bytes = 0) : peer(connect(receive_bytes)) {}
    int connect(int receive_bytes) {
        if (receive_bytes)
            CHECK(!setsockopt(listener.fd, SOL_SOCKET, SO_RCVBUF, &receive_bytes, sizeof(receive_bytes)));
        sockaddr_in address{};
        address.sin_family = AF_INET;
        address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
        auto* sa = reinterpret_cast<sockaddr*>(&address);
        socklen_t size = sizeof(address);
        CHECK(!bind(listener.fd, sa, size));
        CHECK(!listen(listener.fd, 1));
        CHECK(!getsockname(listener.fd, sa, &size));
        CHECK(!::connect(sender.fd, sa, size));
        return accept4(listener.fd, nullptr, nullptr, SOCK_CLOEXEC);
    }
};

p::Policy settings() {
    p::Policy config{};
    config.service = "pacing_demo";
    config.minimum_cap = 20'000;
    config.maximum_cap = 800'000;
    config.initial = {400'000, 4096, 4};
    config.lifetime = 1s;
    config.latency_target = 20ms;
    config.request_deadline = 40ms;
    return config;
}

void lifecycle() {
    auto options = p::probe();
    CHECK(options);
    CHECK(!options->maximum_rate && !options->priority && !options->notsent_lowat && !options->tcp_info
          && !options->congestion && !options->cookie && !options->mark_read);
    Pair sockets;
    const auto original = p::inspect(sockets.sender.fd);
    CHECK(original && original->cookie && original->rtt_us && original->cwnd_segments);
    const auto config = settings();
    auto policy = p::SocketPolicy::attach(sockets.sender.fd, config);
    CHECK(policy && policy->cookie() == original->cookie);
    const auto state = policy->snapshot();
    CHECK(state && state->maximum_rate == 400'000 && state->notsent_lowat == 4096 && state->priority == 4);
    auto conflict = p::SocketPolicy::attach(sockets.sender.fd, config);
    CHECK(!conflict && conflict.error() == std::errc::device_or_resource_busy);
    CHECK(policy->set_rate(900'000) == std::errc::invalid_argument);
    CHECK(!policy->set_rate(200'000));
    const auto after_update = policy->snapshot();
    CHECK(after_update && after_update->maximum_rate == 200'000);
    const pid_t child = fork();
    CHECK(child >= 0);
    if (!child) _exit(policy->set_rate(300'000) == std::errc::state_not_recoverable ? 0 : 1);
    int status = 0;
    CHECK(waitpid(child, &status, 0) == child && WIFEXITED(status) && WEXITSTATUS(status) == 0);
    CHECK(policy->tick(p::Clock::now()+2s) == std::errc::timed_out);
    const auto restored = p::inspect(sockets.sender.fd);
    CHECK(restored && restored->maximum_rate == original->maximum_rate &&
          restored->notsent_lowat == original->notsent_lowat && restored->priority == original->priority);
    CHECK(!p::select_congestion(sockets.sender.fd, "reno"));
    CHECK(p::inspect(sockets.sender.fd)->congestion == "reno");
    CHECK(p::select_congestion(sockets.sender.fd, "invalid-name") == std::errc::invalid_argument);
}

void descriptor_reuse_and_drift() {
    Pair first, second;
    auto handle = p::SocketPolicy::attach(first.sender.fd, settings());
    CHECK(handle);
    CHECK(dup3(second.sender.fd, first.sender.fd, O_CLOEXEC) == first.sender.fd);
    const auto second_before = p::inspect(second.sender.fd);
    CHECK(second_before && second_before->cookie != handle->cookie());
    CHECK(handle->set_rate(500'000) == std::errc::state_not_recoverable);
    CHECK(handle->remove() == std::errc::state_not_recoverable);
    CHECK(p::inspect(second.sender.fd)->maximum_rate == second_before->maximum_rate);

    auto own = p::SocketPolicy::attach(second.sender.fd, settings());
    CHECK(own);
    const std::uint64_t external = 380'000;
    CHECK(!setsockopt(second.sender.fd, SOL_SOCKET, SO_MAX_PACING_RATE, &external, sizeof(external)));
    CHECK(own->set_rate(350'000) == std::errc::device_or_resource_busy);
    CHECK(own->remove() == std::errc::device_or_resource_busy);
    const auto after = p::inspect(second.sender.fd);
    CHECK(after && after->maximum_rate == external && after->notsent_lowat == second_before->notsent_lowat);

    Fd datagram(socket(AF_INET, SOCK_DGRAM | SOCK_CLOEXEC, IPPROTO_UDP));
    auto rejected = p::SocketPolicy::attach(datagram.fd, settings());
    CHECK(!rejected && rejected.error() == std::errc::protocol_not_supported);
}

void readiness() {
    Pair sockets(4096);
    const int buffer = 262144;
    CHECK(!setsockopt(sockets.sender.fd, SOL_SOCKET, SO_SNDBUF, &buffer, sizeof(buffer)));
    std::array<char, 32768> payload{};
    CHECK(send(sockets.sender.fd, payload.data(), payload.size(), MSG_NOSIGNAL) ==
          static_cast<ssize_t>(payload.size()));
    auto metrics = p::inspect(sockets.sender.fd);
    CHECK(metrics && metrics->notsent_bytes && *metrics->notsent_bytes > 4096);
    pollfd descriptor{sockets.sender.fd, POLLOUT, 0};
    CHECK(poll(&descriptor, 1, 0) == 1 && (descriptor.revents & POLLOUT));

    auto policy = settings();
    policy.initial = {0, 4096, std::nullopt};
    auto attached = p::SocketPolicy::attach(sockets.sender.fd, policy);
    CHECK(attached);
    CHECK(poll(&descriptor, 1, 0) == 0);
    CHECK(!attached->remove());
    CHECK(poll(&descriptor, 1, 0) == 1 && (descriptor.revents & POLLOUT));
    std::cout << "readiness changed with " << *metrics->notsent_bytes << " TCP unsent bytes\n";
}

void decision_logic() {
    p::Controller controller(p::Strategy::Hybrid, 50'000, 500'000, 400'000);
    p::Observation observation{1'500'000, 1'200'000, 1'400'000, 20'000,
                               20'000, 55'000, 100, 8, true, false, false};
    const auto t = p::Clock::now();
    CHECK(controller.evaluate(observation, 400'000, t).decision == p::Decision::Hold);
    CHECK(controller.evaluate(observation, 400'000, t+10ms).decision == p::Decision::Hold);
    auto adjustment = controller.evaluate(observation, 400'000, t+1s);
    CHECK(adjustment.decision == p::Decision::Adjust && adjustment.rate == 300'000);
    controller.confirm(true);
    CHECK(controller.evaluate(observation, 300'000, t+1500ms).decision == p::Decision::Hold);
    observation.throughput = 350'000;
    auto rollback = controller.evaluate(observation, 300'000, t+2s);
    CHECK(rollback.decision == p::Decision::Rollback && rollback.rate == 400'000);
    controller.confirm(true);
    observation.class_controller_active = true;
    CHECK(controller.evaluate(observation, 400'000, t+3s).decision == p::Decision::ClassOnly);
    observation.class_controller_active = false;
    observation.class_transition = true;
    CHECK(controller.evaluate(observation, 300'000, t+4s).decision == p::Decision::Hold);
    observation.class_transition = false;
    observation.valid = false;
    CHECK(controller.evaluate(observation, 300'000, t+5s).decision == p::Decision::Fallback);
    controller.confirm(true);
    p::Controller slo(p::Strategy::Slo, 50'000, 500'000, 400'000);
    observation.valid = true;
    observation.backlog_bytes = 0;
    CHECK(slo.evaluate(observation, 400'000, t).decision == p::Decision::Hold);
    CHECK(slo.evaluate(observation, 400'000, t+1s).decision == p::Decision::Hold);
    p::Controller ecn(p::Strategy::Ecn, 50'000, 500'000, 400'000);
    observation.backlog_bytes = 3500; // Enough for the ECN estimator, below the reactive threshold.
    observation.request_p99_us = 10'000;
    observation.rtt_us = 8000;
    observation.ecn_pre_loss = true;
    CHECK(ecn.evaluate(observation, 400'000, t).decision == p::Decision::Hold); // new cohort
    auto early = ecn.evaluate(observation, 400'000, t+1s);
    CHECK(early.decision == p::Decision::Adjust && early.rate == 350'000);
    ecn.confirm(true);
    observation.class_transition = true;
    CHECK(ecn.evaluate(observation, 350'000, t+2s).decision == p::Decision::Hold);
}

void intent_attestation(const char* path) {
    flowcore::v2::Intent intent;
    intent.service = "pacing_demo";
    flowcore::v2::Service service(path, std::move(intent));
    CHECK(service.wait_ready(2s));
    Pair sockets;
    auto config = settings();
    config.lifetime = 60s;
    auto handle = p::SocketPolicy::attach(sockets.sender.fd, config);
    CHECK(handle && handle->attest(service) == handle->cookie());
    const auto initial_generation = service.registration_generation();
    std::cout << "ready" << std::endl;
    std::string line;
    CHECK(std::getline(std::cin, line) && line == "resume");
    const auto snapshot = handle->snapshot();
    CHECK(snapshot && snapshot->maximum_rate == config.initial.maximum_rate);
    for (unsigned n = 0; n < 25 && service.registration_generation() == initial_generation; ++n) {
        auto request = service.begin_request();
        request.complete();
        service.flush(150ms);
        std::this_thread::sleep_for(50ms);
    }
    CHECK(service.registration_generation() > initial_generation);
    CHECK(handle->attest(service) == handle->cookie());
    const auto accepted = service.statistics().accepted;
    for (unsigned n = 0; n < 10 && service.statistics().accepted < accepted+2; ++n) {
        auto request = service.begin_request(40'000, handle->cookie());
        request.complete();
        service.flush(150ms);
    }
    CHECK(service.statistics().accepted >= accepted+2);
    service.dissociate_socket(handle->cookie());
    std::cout << "reattested" << std::endl;
}

int main(int argc, char** argv) {
    try {
        if (argc == 2) {
            intent_attestation(argv[1]);
        } else {
            lifecycle();
            descriptor_reuse_and_drift();
            decision_logic();
            readiness();
            const auto options = p::probe();
            std::cout << "SO_MARK write errno=" << options->mark_write.value() << '\n';
        }
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
