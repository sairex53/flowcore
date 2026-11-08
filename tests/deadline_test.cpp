#include "flowcore/intent_v2.hpp"
#include "flowcore/socket_pacing.hpp"

#include <chrono>
#include <cstdlib>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <utility>

#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

using namespace std::chrono_literals;
namespace fc = flowcore::v2;
namespace d = fc::deadline;
namespace p = fc::pacing;

#define CHECK(expression) do { if (!(expression)) throw std::runtime_error(#expression); } while (false)

namespace {
struct Fd {
    explicit Fd(int value) : fd(value) { CHECK(fd >= 0); }
    Fd(const Fd&) = delete;
    ~Fd() { close(fd); }
    const int fd;
};

void estimator() {
    constexpr auto now = 3'000'000'000ULL;
    d::Work work{now+35'000'000, 4096, 1000, 2000, 1000};
    d::Evidence sample{now-1'000'000, 1'500'000, 500'000, 100, 5000, 6500, 12000, true, true, {}};
    const auto accepted = d::estimate(work, sample, now);
    CHECK(accepted.decision == d::Decision::Accept && accepted.quality == d::Quality::Observed);
    CHECK(accepted.remaining_ns == 35'000'000 && accepted.reserved_us == 4000);
    CHECK(accepted.network_p95_us == 14'692 && accepted.queue_us == 200);
    work.deadline_ns = now+20'700'000;
    CHECK(d::estimate(work, sample, now).decision == d::Decision::Accept);
    work.reliability = d::Reliability::PreferReliable;
    CHECK(d::estimate(work, sample, now).decision == d::Decision::AcceptWithRisk);

    work.deadline_ns = now+35'000'000;
    sample.backlog_bytes = 30'000;
    CHECK(d::estimate(work, sample, now).decision == d::Decision::Defer);
    sample.backlog_bytes = 100;
    sample.capacity_bytes_s = 100'000;
    CHECK(d::estimate(work, sample, now).decision != d::Decision::RejectInfeasible);
    sample.hard_burst_bytes = 0;
    CHECK(d::estimate(work, sample, now).reason == d::Reason::CapacityBound);
    sample.hard_burst_bytes = 4096;
    CHECK(d::estimate(work, sample, now).decision != d::Decision::RejectInfeasible);

    sample.observed_ns = now-2'501'000'000;
    CHECK(d::estimate(work, sample, now).decision == d::Decision::InsufficientData);
    sample.observed_ns = now+1;
    CHECK(d::estimate(work, sample, now).decision == d::Decision::InsufficientData);
    work.deadline_ns = now+1;
    CHECK(d::estimate(work, sample, now).reason == d::Reason::BudgetExhausted);
    CHECK(d::estimate(work, sample, now+1).reason == d::Reason::Expired);
    work.deadline_ns = now+d::maximum_budget_ns+1;
    CHECK(d::estimate(work, sample, now).reason == d::Reason::Invalid);
    work.deadline_ns = now+d::maximum_budget_ns;
    work.egress_bytes = d::maximum_payload_bytes;
    sample = {now, 1, 1, 1ULL<<34, 5, 10, {}, true, true, {}};
    CHECK(d::estimate(work, sample, now).decision == d::Decision::Defer);
    sample.class_scoped = false;
    CHECK(d::estimate(work, sample, now).decision == d::Decision::InsufficientData);
    work.egress_bytes++;
    CHECK(!d::valid(work));
}

void cooperative_lifecycle(fc::Service& service, std::uint64_t cookie) {
    d::Work work{d::monotonic_ns()+100'000'000, 4096, 0, 0, 0};
    auto first = service.begin_deadline_stream(work, 12, 3, cookie);
    auto second = service.begin_deadline_stream(work, 16, 1, cookie);
    CHECK(first.id() != second.id());
    CHECK(first.progress(1024) && !first.progress(1023) && !first.progress(4097));
    auto request = std::move(first);
    CHECK(request.remaining_work().egress_bytes == 3072 && request.remaining().count() > 0);
    CHECK(!first.complete());
    const auto completed = request.complete();
    CHECK(completed && !completed->missed && !request.complete() && !request.progress(2048));
    CHECK(second.complete(fc::Outcome::Failure)->missed);

    work.deadline_ns = d::monotonic_ns()+30'000'000;
    work.expiration = d::Expiration::CancelCooperatively;
    auto cancelled = service.begin_deadline_request(work, cookie);
    std::this_thread::sleep_for(35ms);
    CHECK(cancelled.should_cancel());
    CHECK(cancelled.complete(fc::Outcome::Cancelled)->missed);
    CHECK(!cancelled.should_cancel());
    CHECK(service.admit(work, cookie).reason == d::Reason::Expired);
}

void live(const char* path) {
    fc::Service service(path, {"deadline_demo"});
    CHECK(service.wait_ready(2s));
    Fd listener(socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, IPPROTO_TCP));
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    auto* peer = reinterpret_cast<sockaddr*>(&address);
    socklen_t size = sizeof(address);
    CHECK(bind(listener.fd, peer, size) == 0 && listen(listener.fd, 2) == 0);
    CHECK(getsockname(listener.fd, peer, &size) == 0);
    std::uint64_t original = 0;
    for (int attempt = 0; attempt < 2; ++attempt) {
        Fd sender(socket(AF_INET, SOCK_STREAM | SOCK_CLOEXEC, IPPROTO_TCP));
        CHECK(connect(sender.fd, peer, size) == 0);
        Fd receiver(accept4(listener.fd, nullptr, nullptr, SOCK_CLOEXEC));
        const auto cookie = service.associate_socket(sender.fd);
        CHECK(cookie && cookie != original);
        original = cookie;
        const d::Work work{d::monotonic_ns()+1'000'000'000, 128, {}, {}, {}};
        const auto advice = service.admit(work, cookie);
        CHECK(advice.decision == d::Decision::InsufficientData && advice.reason == d::Reason::MissingEvidence);
        cooperative_lifecycle(service, cookie);
        CHECK(service.flush(1s));
        service.dissociate_socket(cookie);
        CHECK(service.admit(work, cookie).reason == d::Reason::Unauthorized);
    }
    CHECK(service.statistics().accepted >= 2);
}

void pacing() {
    const auto base = p::Clock::time_point(1s);
    p::Controller control(p::Strategy::Deadline, 300'000, 1'500'000, 650'000, 30'000);
    p::Observation o{1'500'000, 700'000, 900'000, 0, 5000, 5000, 64, 8, true};
    o.deadline_pressure = true;
    CHECK(control.evaluate(o, 650'000, base).decision == p::Decision::Hold);
    CHECK(control.evaluate(o, 650'000, base+1s).decision == p::Decision::Hold);
    o.backlog_bytes = 20'000;
    o.class_transition = true;
    CHECK(control.evaluate(o, 650'000, base+2s).decision == p::Decision::Hold);
    o.class_transition = false;
    const auto adjust = control.evaluate(o, 650'000, base+3s);
    CHECK(adjust.decision == p::Decision::Adjust && adjust.rate == 568'750);
    control.confirm(true);
    o.throughput = 100'000;
    CHECK(control.evaluate(o, adjust.rate, base+4s).decision == p::Decision::Rollback);
    control.confirm(true);

    // The one-socket floor bounds even a long episode of sustained pressure.
    p::Controller floor(p::Strategy::Deadline, 600'000, 1'500'000, 650'000, 30'000);
    CHECK(floor.evaluate(o, 650'000, base).decision == p::Decision::Hold);
    CHECK(floor.evaluate(o, 650'000, base+1s).decision == p::Decision::Hold);
}
} // namespace

int main(int argc, char** argv) {
    try {
        if (argc == 2) {
            live(argv[1]);
        } else {
            estimator();
            pacing();
            fc::Service offline("/dev/null/control.sock", {"deadline_demo"});
            CHECK(offline.admit({d::monotonic_ns()+1'000'000'000, 128, {}, {}, {}}).reason == d::Reason::Offline);
            cooperative_lifecycle(offline, 0);
        }
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return EXIT_FAILURE;
    }
}
