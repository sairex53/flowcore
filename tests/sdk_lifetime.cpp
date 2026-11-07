#include <flowcore/intent_v2.hpp>
#include <array>
#include <chrono>
#include <memory>
#include <thread>
#include <vector>
int main() {
    using namespace flowcore::v2;
    std::array<std::unique_ptr<Service>,8> services;
    for (std::size_t i=0;i<services.size();++i) {
        Intent intent;intent.service="lifetime_"+std::to_string(i);
        services[i]=std::make_unique<Service>("/nonexistent-flowcore-validation.sock",intent);
    }
    {
        std::vector<std::jthread> workers;
        for (auto& service:services) workers.emplace_back([handle=service.get()] {
            for (int i=0;i<2000;++i) {
                auto request=handle->begin_stream_request(i,4,50000);
                if (i%2) request.complete(Outcome::Cancelled);
            }
        });
    }
    for (const auto& service:services) {
        if (service->statistics().dropped<2000 || service->ready()) return 1;
    }
    return 0;
}
