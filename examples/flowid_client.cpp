#include "flowcore/intent_v2.hpp"
#include "flowid/session.hpp"

#include <sys/resource.h>

#include <charconv>
#include <chrono>
#include <ctime>
#include <iostream>
#include <map>
#include <optional>
#include <set>
#include <sstream>
#include <string>
#include <string_view>

namespace {
using namespace std::chrono_literals;
using Clock = std::chrono::steady_clock;

int integer(std::string_view value, int low, int high) {
    int result = 0;
    const auto [ptr, error] = std::from_chars(value.data(), value.data()+value.size(), result);
    if (error != std::errc{} || ptr != value.data()+value.size() || result < low || result > high)
        throw std::invalid_argument("number outside permitted range");
    return result;
}

long long micros(Clock::time_point started) {
    return std::chrono::duration_cast<std::chrono::microseconds>(Clock::now()-started).count();
}

void progress(std::string_view event, flowid::FlushResult result, std::uint64_t sequence,
              long long elapsed, std::uint32_t notifications) {
    std::cout << "{\"event\":\"" << event << "\",\"status\":\"" << flowid::name(result.status)
              << "\",\"sequence\":" << sequence << ",\"acknowledged\":" << result.acknowledged
              << ",\"pending\":" << result.pending << ",\"elapsed_us\":" << elapsed
              << ",\"flowcore_notifications\":" << notifications << "}" << std::endl;
}

class Observation {
public:
    explicit Observation(const std::map<std::string, std::string>& args, flowid::Transport transport) {
        if (!args.contains("--flowcore-socket")) return;
        const std::string name = args.contains("--intent-service") ? args.at("--intent-service") : "flowid-demo";
        deadline_ = args.contains("--deadline-us") ? integer(args.at("--deadline-us"), 1, 30'000'000) : 100'000;
        try {
            flowcore::v2::Intent intent;
            intent.service = name;
            intent.deadline_us = deadline_;
            intent.target_latency_us = deadline_;
            intent.prefer_reliable = true;
            service_.emplace(args.at("--flowcore-socket"), std::move(intent));
            if (!service_->wait_ready(600ms)) {
                service_.reset();
                return;
            }
            service_->declare_transport({"flowid-logical", flowcore::v2::TransportKind::Logical, false, true});
            service_->declare_transport({"flowid-underlay", transport == flowid::Transport::tcp
                ? flowcore::v2::TransportKind::Tcp : flowcore::v2::TransportKind::Mptcp});
        } catch (const std::exception&) {
            service_.reset();
        }
    }

    std::optional<flowcore::v2::Request> request() {
        if (service_ && service_->ready()) return service_->begin_request(deadline_);
        return std::nullopt;
    }

    std::uint32_t notifications() const {
        return service_ ? service_->notifications() : 0;
    }

    void path(bool degraded, const char* error) {
        if (!service_) return;
        try {
            service_->report_transport_state("flowid-logical", degraded, error);
        } catch (const std::exception&) {
            // Telemetry failure must not change the logical-session outcome.
        }
    }

    void report() {
        if (!service_) return;
        service_->flush(600ms);
        const auto metrics = service_->statistics();
        std::cout << "{\"event\":\"flowcore\",\"accepted\":" << metrics.accepted
                  << ",\"dropped\":" << metrics.dropped << ",\"notifications\":" << notifications()
                  << "}" << std::endl;
    }
private:
    std::optional<flowcore::v2::Service> service_;
    std::uint32_t deadline_ = 0;
};

void benchmark(flowid::Session& session, int count, int size) {
    const std::string payload(static_cast<std::size_t>(size), 'x');
    const auto begin = Clock::now();
    const auto cpu = std::clock();
    int acknowledged = 0;
    flowid::Status status = flowid::Status::acknowledged;
    for (int i = 0; i < count; ++i) {
        const auto receipt = session.send(payload);
        status = receipt.progress.status;
        if (!receipt.acknowledged()) break;
        ++acknowledged;
    }
    rusage usage{};
    getrusage(RUSAGE_SELF, &usage);
    const auto elapsed = micros(begin);
    std::cout << "{\"event\":\"benchmark\",\"status\":\"" << flowid::name(status)
              << "\",\"messages\":" << acknowledged << ",\"payload_bytes\":" << static_cast<long long>(acknowledged)*size
              << ",\"elapsed_us\":" << elapsed << ",\"cpu_seconds\":" << double(std::clock()-cpu)/CLOCKS_PER_SEC
              << ",\"peak_rss_kib\":" << usage.ru_maxrss << "}" << std::endl;
}
} // namespace

int main(int argc, char** argv) {
    try {
        const std::set<std::string> allowed{"--service", "--directory", "--ca", "--cert", "--key",
            "--transport", "--timeout-ms", "--attempt-ms", "--flowcore-socket", "--intent-service", "--deadline-us"};
        std::map<std::string, std::string> args;
        for (int i = 1; i < argc; i += 2) {
            if (i+1 >= argc || !allowed.contains(argv[i]) || !args.emplace(argv[i], argv[i+1]).second)
                throw std::invalid_argument("expected unique --option value pairs");
        }
        flowid::Options options;
        options.resolve = flowid::file_directory(args.at("--directory"));
        options.ca = args.at("--ca");
        options.certificate = args.at("--cert");
        options.key = args.at("--key");
        const auto transport = args.contains("--transport") ? args.at("--transport") : "tcp";
        if (transport == "mptcp") options.transport = flowid::Transport::mptcp;
        else if (transport != "tcp") throw std::invalid_argument("unsupported transport");
        if (args.contains("--timeout-ms")) options.operation_timeout =
            std::chrono::milliseconds(integer(args.at("--timeout-ms"), 10, 30000));
        if (args.contains("--attempt-ms")) options.attempt_timeout =
            std::chrono::milliseconds(integer(args.at("--attempt-ms"), 10, 5000));
        Observation observation(args, options.transport);
        const auto started = Clock::now();
        auto session = flowid::connect(args.at("--service"), std::move(options));
        std::cout << "{\"event\":\"opened\",\"transport\":\"" << transport << "\",\"elapsed_us\":"
                  << micros(started) << "}" << std::endl;
        session.on_reconnect([&](const flowid::ReconnectEvent& event) {
            std::cout << "{\"event\":\"reconnected\",\"transport\":\"" << flowid::name(event.endpoint.transport)
                      << "\",\"address\":\"" << event.endpoint.address << "\",\"port\":" << event.endpoint.port
                      << ",\"committed\":" << event.committed << ",\"association\":" << event.association
                      << ",\"elapsed_us\":" << event.elapsed.count() << "}" << std::endl;
            observation.path(false, "none");
        });
        std::string line;
        while (std::getline(std::cin, line)) {
            const auto begin = Clock::now();
            try {
                if (line.starts_with("send ")) {
                    auto measure = observation.request();
                    const auto result = session.send(std::string_view(line).substr(5));
                    const auto elapsed = micros(begin);
                    if (measure) measure->complete(result.acknowledged() ? flowcore::v2::Outcome::Ok
                        : (result.progress.status == flowid::Status::timeout ? flowcore::v2::Outcome::Timeout
                                                                             : flowcore::v2::Outcome::Failure));
                    if (!result.acknowledged()) observation.path(true,
                        result.progress.status == flowid::Status::timeout ? "timeout" : "reset");
                    progress("send", result.progress, result.sequence, elapsed, observation.notifications());
                } else if (line.starts_with("queue ")) {
                    const auto sequence = session.enqueue(std::string_view(line).substr(6));
                    const auto current = session.state();
                    progress("queue", {flowid::Status::pending, current.acknowledged, current.pending},
                             sequence, micros(begin), observation.notifications());
                } else if (line == "flush" || line == "recover") {
                    const auto result = line == "flush" ? session.flush() : session.recover();
                    progress(line, result, 0, micros(begin), observation.notifications());
                } else if (line == "state") {
                    const auto state = session.state();
                    std::cout << "{\"event\":\"state\",\"authenticated\":" << (state.authenticated ? "true" : "false")
                              << ",\"acknowledged\":" << state.acknowledged << ",\"association\":" << state.association
                              << ",\"reconnects\":" << state.reconnects << ",\"pending\":" << state.pending
                              << ",\"pending_bytes\":" << state.pending_bytes << "}" << std::endl;
                } else if (line == "close") {
                    const auto status = session.close();
                    std::cout << "{\"event\":\"close\",\"status\":\"" << flowid::name(status) << "\"}" << std::endl;
                    if (status == flowid::Status::acknowledged) break;
                } else if (line.starts_with("bench ")) {
                    std::istringstream command(line.substr(6));
                    std::string count, size, extra;
                    if (!(command >> count >> size) || command >> extra)
                        throw std::invalid_argument("expected bench count bytes");
                    benchmark(session, integer(count, 1, 5000), integer(size, 1, 16384));
                } else {
                    throw std::invalid_argument("unknown demo operation");
                }
            } catch (const flowid::Error& error) {
                std::cout << "{\"event\":\"error\",\"status\":\"" << flowid::name(error.status()) << "\"}" << std::endl;
            } catch (const std::invalid_argument&) {
                std::cout << "{\"event\":\"error\",\"status\":\"invalid_configuration\"}" << std::endl;
            }
        }
        observation.report();
    } catch (const flowid::Error& error) {
        std::cout << "{\"event\":\"open_failed\",\"status\":\"" << flowid::name(error.status()) << "\"}" << std::endl;
        return 2;
    } catch (const std::exception&) {
        std::cerr << "usage: flowid-client --service NAME --directory PATH --ca PEM --cert PEM --key PEM"
                     " [--transport tcp|mptcp] [--timeout-ms N] [--attempt-ms N] [--flowcore-socket PATH]\n";
        return 1;
    }
    return 0;
}
