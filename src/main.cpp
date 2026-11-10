#include "flowcore/policy.hpp"
#include <sys/wait.h>
#include <sys/stat.h>
#include <poll.h>
#include <unistd.h>
#include <filesystem>
#include <cerrno>
#include <chrono>
#include <csignal>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <vector>
namespace {
volatile std::sig_atomic_t stopping = 0;
void stop(int) { stopping = 1; }
void tc_class(const flowcore::Config& c, int id, int percent, int priority) {
    std::vector<std::string> words{c.tc, "class", "change", "dev", c.interface,
        "parent", "1:1", "classid", "1:" + std::to_string(id), "htb",
        "rate", std::to_string(c.capacity_kbit * percent / 100) + "kbit",
        "ceil", std::to_string(c.capacity_kbit) + "kbit", "prio", std::to_string(priority),
        "quantum", "1514", "burst", "16k", "cburst", "16k"};
    std::vector<char*> argv;
    for (auto& word : words) argv.push_back(word.data());
    argv.push_back(nullptr);
    pid_t child = fork();
    if (child < 0) throw std::runtime_error("fork tc failed");
    if (child == 0) { execv(c.tc.c_str(), argv.data()); _exit(127); }
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
        if (errno != EINTR) throw std::runtime_error("wait for tc failed");
    }
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0)
        throw std::runtime_error("tc class update failed");
}
void require_lab_namespace(const char* path) {
    struct stat current{}, initial{}, expected{};
    auto same = [](const struct stat& left, const struct stat& right) {
        return left.st_dev == right.st_dev && left.st_ino == right.st_ino;
    };
    if (stat("/proc/self/ns/net", &current) || stat("/proc/1/ns/net", &initial) ||
        stat(path, &expected) || same(current, initial) || !same(current, expected))
        throw std::runtime_error("legacy controller requires the owned lab namespace");
}
void apply(const flowcore::Config& c, bool protected_mode) {
    int critical = protected_mode ? c.protected_percent : c.balanced_percent;
    // Decrease a reservation before increasing its peer's reservation.
    if (protected_mode) tc_class(c, 20, 100 - critical, 2);
    tc_class(c, 10, critical, protected_mode ? 0 : 2);
    if (!protected_mode) tc_class(c, 20, 100 - critical, 2);
}
void log(const flowcore::Config& c, bool protected_mode, const std::string& event,
         double p99 = 0, int samples = 0) {
    auto stats = flowcore::link_stats(c.interface);
    const auto monotonic_ms = std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch()).count();
    std::cout << "{\"monotonic_ms\":" << monotonic_ms << ",\"event\":\"" << event << "\",\"state\":\""
              << (protected_mode ? "protected" : "balanced") << "\",\"p99_ms\":" << p99
              << ",\"samples\":" << samples << ",\"tx_bytes\":" << stats.tx_bytes
              << ",\"tx_dropped\":" << stats.tx_dropped << "}\n" << std::flush;
    if (!std::cout) throw std::runtime_error("controller telemetry write failed");
}
}
int main(int argc, char** argv) {
    if (argc==2 && std::string(argv[1])=="--version") {
        std::cout << "FLOWCORE " << FLOWCORE_VERSION << '\n';
        return 0;
    }
    std::error_code executable_error;
    const auto executable = std::filesystem::read_symlink("/proc/self/exe", executable_error);
    const bool source_build = !executable_error && executable.parent_path() == FLOWCORE_SOURCE_BINARY_DIR;
    if (argc < 2 || (std::string(argv[1]) != "--link" && std::string(argv[1]) != "--config")) {
        char python[] = "/usr/bin/python3";
        char no_bytecode[] = "-B";
        std::error_code error;
        const auto installed = executable.parent_path()/FLOWCORE_INSTALLED_CLI_REL;
        const bool installed_cli = !executable_error && std::filesystem::is_regular_file(installed, error);
        if (!installed_cli && !source_build) {
            std::cerr << "flowcore: installed management CLI unavailable\n";
            return 3;
        }
        std::string script = installed_cli ? installed.string() : FLOWCORE_CLI_PATH;
        char ignore_environment[] = "-E";
        char no_user_site[] = "-s";
        std::vector<char*> command{python, ignore_environment, no_user_site, no_bytecode, script.data()};
        command.insert(command.end(), argv + 1, argv + argc);
        command.push_back(nullptr);
        execv(python, command.data());
        std::cerr << "flowcore: cannot launch management CLI\n";
        return 3;
    }

    flowcore::Config config;
    bool actuator_started = false;
    try {
        if (argc == 3 && std::string(argv[1]) == "--link") {
            auto s = flowcore::link_stats(argv[2]);
            std::cout << "{\"tx_bytes\":" << s.tx_bytes << ",\"tx_dropped\":" << s.tx_dropped
                      << ",\"rx_bytes\":" << s.rx_bytes << ",\"rx_dropped\":" << s.rx_dropped
                      << ",\"tx_packets\":" << s.tx_packets << ",\"rx_packets\":" << s.rx_packets
                      << ",\"tx_errors\":" << s.tx_errors << ",\"rx_errors\":" << s.rx_errors << "}\n";
            return 0;
        }
        if (argc != 5 || std::string(argv[1]) != "--config" || std::string(argv[3]) != "--lab-netns")
            throw std::runtime_error("usage: flowcore --config FILE --lab-netns PATH | --link INTERFACE");
        if (!source_build) throw std::runtime_error("installed actuation requires the isolated network helper");
        require_lab_namespace(argv[4]);
        config = flowcore::read_config(argv[2]);
        flowcore::Policy policy(config);
        std::signal(SIGTERM, stop);
        std::signal(SIGINT, stop);
        std::signal(SIGPIPE, SIG_IGN);
        actuator_started = true;
        apply(config, false);
        log(config, false, "start");
        auto last_feedback = std::chrono::steady_clock::now();
        std::string pending;
        while (!stopping) {
            pollfd input{STDIN_FILENO, POLLIN, 0};
            int ready = poll(&input, 1, 250);
            if (ready < 0 && errno != EINTR) throw std::runtime_error("feedback poll failed");
            if (ready > 0 && (input.revents & (POLLERR | POLLNVAL)))
                throw std::runtime_error("feedback descriptor failed");
            if (ready > 0 && (input.revents & (POLLIN | POLLHUP))) {
                char buffer[1024];
                ssize_t length = read(STDIN_FILENO, buffer, sizeof(buffer));
                if (length == 0) {
                    if (!pending.empty()) throw std::runtime_error("truncated feedback line");
                    break;
                }
                if (length < 0) {
                    if (errno == EINTR) continue;
                    throw std::runtime_error("feedback read failed");
                }
                pending.append(buffer, length);
                if (pending.size() > 4096) throw std::runtime_error("feedback exceeds limit");
                size_t newline;
                while ((newline = pending.find('\n')) != std::string::npos) {
                    std::istringstream line(pending.substr(0, newline));
                    pending.erase(0, newline + 1);
                    double p99;
                    int samples;
                    std::string extra;
                    if (!(line >> p99 >> samples) || (line >> extra) || samples > 1000000)
                        throw std::runtime_error("invalid feedback line");
                    bool changed = policy.update(p99, samples);
                    if (changed) apply(config, policy.protected_mode());
                    log(config, policy.protected_mode(), changed ? "transition" : "sample", p99, samples);
                    last_feedback = std::chrono::steady_clock::now();
                }
            }
            if (std::chrono::steady_clock::now() - last_feedback > std::chrono::seconds(3)) {
                if (policy.protected_mode()) {
                    apply(config, false);
                    log(config, false, "stale");
                }
                policy.reset();
                last_feedback = std::chrono::steady_clock::now();
            }
        }
        apply(config, false);
        log(config, false, "stop");
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "flowcore: " << error.what() << '\n';
        if (actuator_started) {
            try { apply(config, false); }
            catch (const std::exception& rollback) { std::cerr << "rollback: " << rollback.what() << '\n'; }
        }
        return 1;
    }
}
