#include "flowcore/intent_v2.hpp"

#include <chrono>
#include <thread>
#include <string>

using namespace flowcore::v2;

int main(int argc,char** argv) {
    if (argc!=3) return 2;
    Intent intent;
    intent.service="protocol_probe";
    Service service(argv[1],intent);
    const std::string_view mode=argv[2];
    const auto limit=std::chrono::steady_clock::now()+std::chrono::seconds(3);
    if (mode=="mismatch") {
        while (std::chrono::steady_clock::now()<limit) {
            if (service.ready()) return 3;
            if (service.last_error()==Error::ProtocolError) return 0;
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
        return 4;
    }
    if (mode!="legacy-v2" || !service.wait_ready(std::chrono::seconds(3))) return 5;
    const auto caps=service.capabilities();
    if (caps.protocol_version!=2 || caps.features!=0 || caps.udp_message_authorized) return 6;
    auto request=service.begin_request(10'000);
    if (!request.complete() || !service.flush(std::chrono::seconds(2))) return 7;
    const auto stats=service.statistics();
    if (stats.accepted<2 || stats.transport_errors!=0) return 8;
    return 0;
}
