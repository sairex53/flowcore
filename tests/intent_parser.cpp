#include "intent_transport.hpp"
#include <limits>
#include <string>
#include <vector>
using namespace flowcore::v2::detail;
int main() {
    if (milliseconds(1)!="0.001" || milliseconds(1000)!="1.000" ||
        milliseconds(1234567)!="1234.567" || milliseconds(600000000)!="600000.000") return 4;
    if (unsigned_field("{\"n\": 0}","\"n\": ")!=0 ||
        unsigned_field("{\"n\": 18446744073709551615}","\"n\": ")!=std::numeric_limits<std::uint64_t>::max()) return 1;
    const std::vector<std::string> invalid={"-1","+1","true","2.0","2e0","18446744073709551616","1x","","\"12\""};
    for (const auto& value:invalid) {
        try { unsigned_field("{\"n\": "+value+"}","\"n\": ");return 2; }
        catch (const RpcError&) {}
    }
    for (unsigned i=0;i<1000;++i) {
        try { unsigned_field("{\"n\": "+std::to_string(i)+"garbage}","\"n\": ");return 3; }
        catch (const RpcError&) {}
    }
}
