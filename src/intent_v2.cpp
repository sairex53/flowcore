#include "flowcore/intent_v2.hpp"
#include "intent_transport.hpp"
#include "flowcore/udp_intent.hpp"
#include <array>
#include <atomic>
#include <condition_variable>
#include <mutex>
#include <stdexcept>
#include <string_view>
#include <thread>
#include <utility>
#include <unistd.h>
#include <sys/socket.h>
#include <system_error>
#include <cerrno>

namespace flowcore::detail {
std::string exchange(const std::string&, std::string, int);
}
namespace flowcore::v2 {
namespace {
std::string field(std::string_view text, std::string_view key) {
    auto pos=text.find(key);
    if (pos==std::string_view::npos) throw std::runtime_error("missing protocol field");
    pos+=key.size();
    auto end=text.find('"',pos);
    if (end==std::string_view::npos) throw std::runtime_error("invalid protocol field");
    return std::string(text.substr(pos,end-pos));
}
std::uint64_t integer(std::string_view text, std::string_view key) {
    return detail::unsigned_field(text,key);
}
bool boolean(std::string_view text, std::string_view key) noexcept {
    const auto position=text.find(key);
    if (position==std::string_view::npos) return false;
    auto value=position+key.size();
    while (value<text.size() && (text[value]==' ' || text[value]=='\t')) ++value;
    return text.substr(value,4)=="true";
}
bool protocol_boolean(std::string_view text, std::string_view key) {
    const auto position=text.find(key);
    if (position==std::string_view::npos)
        throw detail::RpcError(Error::ProtocolError,"missing boolean field");
    auto value=position+key.size();
    while (value<text.size() && (text[value]==' ' || text[value]=='\t')) ++value;
    const bool result=text.substr(value,4)=="true";
    if (!result && text.substr(value,5)!="false")
        throw detail::RpcError(Error::ProtocolError,"invalid boolean field");
    value+=result?4:5;
    while (value<text.size() && (text[value]==' ' || text[value]=='\t')) ++value;
    if (value>=text.size() || (text[value]!=',' && text[value]!='}' && text[value]!=']'))
        throw detail::RpcError(Error::ProtocolError,"invalid boolean delimiter");
    return result;
}
std::uint64_t feature_mask(std::string_view response) noexcept {
    constexpr std::pair<std::string_view,Capability> features[] = {
        {"lease_renewal",Capability::LeaseRenewal},
        {"udp_socket_association",Capability::SocketAssociation},
        {"socket_dissociation",Capability::SocketDissociation},
        {"notification_quality",Capability::NotificationQuality},
        {"transport_sessions_v1",Capability::TransportSessions},
        {"deadline_admission_v1",Capability::DeadlineAdmission},
        {"udp_per_message_intents_v1",Capability::UdpPerMessageIntents}
    };
    std::uint64_t result=0;
    for (const auto& [name,capability] : features)
        if (response.find(name)!=std::string_view::npos)
            result|=static_cast<std::uint64_t>(capability);
    return result;
}

std::uint32_t microseconds(std::chrono::microseconds value) {
    if (value.count()<0 || value.count()>600'000'000)
        throw std::invalid_argument("request deadline outside supported range");
    return static_cast<std::uint32_t>(value.count());
}

bool label(std::string_view text) {
    return !text.empty() && text.size()<=32 && text[0]>='a' && text[0]<='z' &&
           text.find_first_not_of("abcdefghijklmnopqrstuvwxyz0123456789_-")==std::string_view::npos;
}
}
namespace detail {
struct Event {
    std::uint64_t id, start, end, generation, cookie;
    std::uint32_t deadline;
    unsigned phase;
    std::uint64_t stream=0;
    unsigned priority=0;
    bool logical=false;
    std::uint64_t absolute_ns=0;
    v2::TrafficAccounting traffic{};
};
struct Channel {
    const pid_t owner=getpid();
    const std::string path, registration;
    const Intent requested;
    const std::uint32_t default_deadline,lifetime;
    Transport transport;
    std::mutex mutex;
    std::condition_variable changed;
    std::array<Event,512> queue{};
    std::size_t head=0, count=0;
    bool stopping=false, busy=false;
    std::string lease;
    std::atomic<bool> connected{false}, absolute_deadlines{false}, udp_authorized{false},
                      notifications_enabled{false};
    std::atomic<std::uint32_t> protocol_version{0};
    std::atomic<std::uint64_t> negotiated_features{0}, application_samples{0};
    std::atomic<bool> network_observed{false};
    std::uint32_t udp_socket_mark=0, udp_rate_pps=0, udp_burst=0, udp_max_deadline_ms=0;
    std::uint64_t udp_expires_ns=0;
    std::shared_ptr<flowcore::udp::UrgencyBudget> udp_budget;
    std::atomic<std::uint64_t> sequence{0}, generation{0}, queued{0}, accepted{0}, dropped{0}, rejected{0}, errors{0};
    std::atomic<std::uint32_t> notifications{0};
    std::atomic<Error> last_error{Error::None};
    std::atomic<std::uint64_t> notification_time{0};
    std::uint64_t renew_after=0;
    bool first=true,can_renew=false;
    std::thread worker;

    Channel(std::string socket, const Intent& intent)
        : path(std::move(socket)), registration(make_registration(intent)), requested(intent),
          default_deadline(intent.deadline_us),lifetime(intent.lifetime_seconds),transport(path),
          worker([this]{ run(); }) {}
    static std::string make_registration(const Intent& i) {
        if ((i.priority!=Priority::Standard && i.priority!=Priority::Interactive && i.priority!=Priority::Critical) || !label(i.service) || i.deadline_us>600000000 || !i.lifetime_seconds || i.lifetime_seconds>3600 ||
            !i.target_latency_us || i.target_latency_us>600000000 ||
            i.minimum_kbit>1000000 || i.maximum_kbit>1000000 || i.desired_kbit>1000000 ||
            (i.maximum_kbit && (i.minimum_kbit>i.maximum_kbit || i.desired_kbit>i.maximum_kbit)))
            throw std::invalid_argument("invalid FLOWCORE intent");
        const char* priority=i.priority==Priority::Critical ? "critical" :
                             i.priority==Priority::Interactive ? "interactive" : "standard";
        return "{\"op\":\"intent_v2\",\"version\":2,\"action\":\"register\",\"isolation\":\""+
            std::string(i.prefer_isolation?"dedicated_preference":"shared")+"\",\"intent\":{\"name\":\""+i.service+
            "\",\"priority\":\""+priority+"\",\"target_p99_ms\":"+milliseconds(i.target_latency_us)+
            ",\"desired_kbit\":"+std::to_string(i.desired_kbit)+",\"min_kbit\":"+std::to_string(i.minimum_kbit)+
            ",\"max_kbit\":"+std::to_string(i.maximum_kbit)+",\"ttl_seconds\":"+std::to_string(i.lifetime_seconds)+
            ",\"deadline_ms\":"+(i.deadline_us?milliseconds(i.deadline_us):"null")+
            ",\"reliability\":\""+(i.prefer_reliable?"prefer_reliable":"balanced")+"\"}}";
    }
    ~Channel() {
        { std::lock_guard lock(mutex); stopping=true; dropped+=count; count=0; }
        changed.notify_all();
        worker.join();
        if (udp_budget) udp_budget->revoke();
        udp_authorized=false;
    }
    bool push(Event e) noexcept {
        if (getpid()!=owner || !connected) { ++dropped; return false; }
        std::unique_lock lock(mutex,std::try_to_lock);
        if (!lock || stopping || count==queue.size()) { ++dropped; return false; }
        queue[(head+count)%queue.size()]=e;
        ++count;
        ++queued;
        changed.notify_one();
        return true;
    }
    std::string envelope(std::string_view action, const std::string& token) const {
        return "{\"op\":\"intent_v2\",\"version\":2,\"action\":\""+std::string(action)+"\",\"lease\":\""+token+"\"";
    }
    void run() noexcept {
        while (true) {
            std::array<Event,32> batch{};
            std::size_t size=0;
            std::string token;
            {
                std::unique_lock lock(mutex);
                if (!first) changed.wait_for(lock,std::chrono::milliseconds(lifetime<=1?200:500),[&]{return stopping || count;});
                first=false;
                if (stopping) break;
                busy=true;
                if (count && count<32) changed.wait_for(lock,std::chrono::milliseconds(1),[&]{return stopping || count>=32;});
                token=lease;
                while (count && size<batch.size()) {
                    batch[size++]=queue[head];
                    head=(head+1)%queue.size();
                    --count;
                }
            }
            try {
                if (!connected) {
                    auto reply=transport.exchange(registration);
                    auto next=field(reply,"\"lease\": \"");
                    const auto minimum=reply.find("\"protocol_min\": ");
                    const auto maximum=reply.find("\"protocol_max\": ");
                    if ((minimum==std::string::npos)!=(maximum==std::string::npos))
                        throw RpcError(Error::ProtocolError,"incomplete protocol version range");
                    if (minimum!=std::string::npos) {
                        const auto low=integer(reply,"\"protocol_min\": ");
                        const auto high=integer(reply,"\"protocol_max\": ");
                        if (low>high || low>2 || high<2)
                            throw RpcError(Error::ProtocolError,"no compatible intent protocol version");
                    }
                    if (next.size()!=32 || next.find_first_not_of("0123456789abcdef")!=std::string::npos)
                        throw std::runtime_error("invalid lease");
                    { std::lock_guard lock(mutex); lease=next; token=next; }
                    can_renew=reply.find("\"renewable\": true")!=std::string::npos;
                    absolute_deadlines=reply.find("\"deadline_admission_v1\"")!=std::string::npos;
                    protocol_version=2;
                    negotiated_features=feature_mask(reply);
                    std::uint32_t socket_mark=0, rate=0, burst=0, horizon=0;
                    std::uint64_t expires=0;
                    const bool authorized=reply.find("\"udp_message\": {\"authorized\": true")!=std::string::npos;
                    if (authorized) {
                        auto bounded = [&](std::string_view key, std::uint64_t maximum) {
                            auto value=integer(reply,key);
                            if (!value || value>maximum) throw std::runtime_error("invalid UDP message grant");
                            return static_cast<std::uint32_t>(value);
                        };
                        socket_mark=bounded("\"socket_mark\": ",UINT32_MAX);
                        rate=bounded("\"urgent_rate_pps\": ",100000);
                        burst=bounded("\"urgent_burst\": ",10000);
                        horizon=bounded("\"max_deadline_ms\": ",10000);
                        expires=integer(reply,"\"expires_ns\": ");
                    }
                    {
                        std::lock_guard lock(mutex);
                        if (udp_budget) udp_budget->revoke();
                        udp_budget.reset();
                        udp_socket_mark=socket_mark; udp_rate_pps=rate; udp_burst=burst; udp_max_deadline_ms=horizon;
                        udp_expires_ns=expires;
                        udp_authorized=authorized && expires>deadline::monotonic_ns();
                        notifications=0;notification_time=0;application_samples=0;network_observed=false;
                        ++generation;
                    }
                    connected=true;
                    renew_after=deadline::monotonic_ns()+static_cast<std::uint64_t>(lifetime)*500000000ULL;
                    last_error=Error::None;
                    dropped+=size;
                } else {
                    if (can_renew && deadline::monotonic_ns()>=renew_after) {
                        auto renewal=transport.exchange(envelope("renew",token)+"}");
                        if (udp_authorized) {
                            const auto expiry=integer(renewal,"\"expires_ns\": ");
                            std::lock_guard lock(mutex);
                            udp_expires_ns=expiry;
                            if (udp_budget) udp_budget->renew(expiry);
                        }
                        renew_after=deadline::monotonic_ns()+static_cast<std::uint64_t>(lifetime)*500000000ULL;
                    }
                    if (size || notifications_enabled.load(std::memory_order_acquire)) {
                        std::string message=envelope("batch",token)+",\"events\":[";
                        std::size_t sent=0;
                        constexpr const char* phases[]={"start","ok","timeout","failure","cancelled"};
                        for (std::size_t i=0;i<size;++i) {
                            const auto& e=batch[i];
                            if (e.generation!=generation) { ++dropped; continue; }
                            if (sent++) message+=",";
                            message+="{\"id\":"+std::to_string(e.id)+",\"phase\":\""+phases[e.phase]+
                                "\",\"start_ns\":"+std::to_string(e.start)+",\"end_ns\":"+std::to_string(e.end)+
                                ",\"deadline_us\":"+std::to_string(e.deadline)+",\"cookie\":"+(e.cookie?std::to_string(e.cookie):"null");
                            if (e.absolute_ns) message+=",\"deadline_ns\":"+std::to_string(e.absolute_ns);
                            if (e.logical) message+=",\"stream_id\":"+std::to_string(e.stream)+",\"stream_priority\":"+std::to_string(e.priority);
                            if (e.traffic.valid) {
                                const auto& t=e.traffic;
                                message+=",\"traffic\":{\"bytes_offered\":"+std::to_string(t.bytes_offered)+
                                    ",\"bytes_admitted\":"+std::to_string(t.bytes_admitted)+
                                    ",\"bytes_transmitted\":"+std::to_string(t.bytes_transmitted)+
                                    ",\"bytes_received\":"+std::to_string(t.bytes_received)+
                                    ",\"udp_messages_attempted\":"+std::to_string(t.udp_messages_attempted)+
                                    ",\"udp_messages_transmitted\":"+std::to_string(t.udp_messages_transmitted)+
                                    ",\"udp_send_failures\":"+std::to_string(t.udp_send_failures)+
                                    ",\"udp_urgent_messages\":"+std::to_string(t.udp_urgent_messages)+
                                    ",\"udp_background_messages\":"+std::to_string(t.udp_background_messages)+"}";
                            }
                            message+="}";
                        }
                        message+="]}";
                        auto reply=transport.exchange(std::move(message));
                        auto acknowledged=integer(reply,"\"accepted\": ");
                        auto invalid=integer(reply,"\"rejected\": ");
                        if (acknowledged>sent || invalid>sent || acknowledged+invalid!=sent)
                            throw RpcError(Error::ProtocolError,"invalid event acknowledgement counts");
                        accepted+=acknowledged;
                        rejected+=invalid;
                        last_error=invalid?Error::EventRejected:Error::None;
                        std::uint32_t flags=0;
                        constexpr const char* names[]={"slo_risk","congestion","policy_fallback","interface_degradation","insufficient_capacity"};
                        for (unsigned i=0;i<5;++i)
                            if (reply.find("\""+std::string(names[i])+"\": true")!=std::string::npos) flags|=1U<<i;
                        notifications=flags;
                        if (reply.find("\"observed_ns\": ")!=std::string::npos)
                            notification_time=integer(reply,"\"observed_ns\": ");
                        if (reply.find("\"application_samples\": ")!=std::string::npos)
                            application_samples=integer(reply,"\"application_samples\": ");
                        network_observed=boolean(reply,"\"network_observed\": ");
                    }
                }
            } catch (const RpcError& error) {
                ++errors; dropped+=size; last_error=error.code;
                if (error.retry_registration) connected=false;
                { std::lock_guard lock(mutex); if (udp_budget) udp_budget->revoke(); udp_authorized=false; }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            } catch (const std::exception& error) {
                ++errors; dropped+=size; last_error=Error::TransportUnavailable;
                { std::lock_guard lock(mutex); if (udp_budget) udp_budget->revoke(); udp_authorized=false; }
                // Preserve a known lease on transport ambiguity; retries must not leak leases.
                // A restarted daemon rejects the stale lease and permits fresh registration.
                if (std::string_view(error.what())=="FLOWCORE operation forbidden") { connected=false;last_error=Error::AuthorizationDenied; }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            } catch (...) {
                ++errors; dropped+=size; connected=false; last_error=Error::ProtocolError;
                { std::lock_guard lock(mutex); if (udp_budget) udp_budget->revoke(); udp_authorized=false; }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            }
            { std::lock_guard lock(mutex); busy=false; }
            changed.notify_all();
        }
        if (!lease.empty()) {
            try { transport.exchange(envelope("close",lease)+"}"); } catch (...) {}
        }
    }
};
}
Service::Service(std::string path, Intent intent) : channel_(std::make_shared<detail::Channel>(std::move(path),intent)) {}
std::expected<Service,OpenError> Service::try_open(std::string path, Intent intent) noexcept {
    try {
        return Service(std::move(path),std::move(intent));
    } catch (const std::invalid_argument& error) {
        return std::unexpected(OpenError{Error::InvalidRequest,error.what()});
    } catch (const std::exception& error) {
        return std::unexpected(OpenError{Error::TransportUnavailable,error.what()});
    } catch (...) {
        return std::unexpected(OpenError{Error::TransportUnavailable,"unknown SDK initialization error"});
    }
}
Request Service::begin_request(std::uint32_t deadline, std::uint64_t cookie) const { return Request(channel_,deadline,cookie); }
Request Service::begin_request(std::chrono::microseconds deadline, std::uint64_t cookie) const {
    return begin_request(microseconds(deadline),cookie);
}
bool Service::ready() const noexcept { return channel_->owner==getpid() && channel_->connected; }
bool Service::wait_ready(std::chrono::milliseconds timeout) const {
    if (channel_->owner!=getpid()) return false;
    std::unique_lock lock(channel_->mutex);
    return channel_->changed.wait_for(lock,timeout,[&]{return channel_->connected.load();});
}
bool Service::flush(std::chrono::milliseconds timeout) const {
    if (channel_->owner!=getpid()) return false;
    std::unique_lock lock(channel_->mutex);
    return channel_->changed.wait_for(lock,timeout,[&]{return !channel_->count && !channel_->busy;});
}
Statistics Service::statistics() const noexcept {
    return {channel_->queued,channel_->accepted,channel_->dropped,channel_->rejected,channel_->errors};
}
TransportStatistics Service::transport_statistics() const noexcept {
    return {channel_->transport.connections,channel_->transport.requests,channel_->transport.legacy};
}
CapabilitySnapshot Service::capabilities() const noexcept {
    if (!ready()) return {};
    return {channel_->protocol_version.load(),channel_->negotiated_features.load(),channel_->udp_authorized.load()};
}
NotificationSnapshot Service::notification_snapshot() const noexcept {
    return {channel_->notifications.load(),channel_->notification_time.load(),
            channel_->application_samples.load(),channel_->network_observed.load()};
}
void Service::subscribe_notifications(bool enabled) const noexcept {
    if (channel_->owner!=getpid()) return;
    channel_->notifications_enabled.store(enabled,std::memory_order_release);
    channel_->changed.notify_one();
}
Error Service::last_error() const noexcept { return channel_->last_error; }
std::uint64_t Service::registration_generation() const noexcept { return channel_->generation; }
std::uint64_t Service::notifications_time_ns() const noexcept { return channel_->notification_time; }
std::uint32_t Service::notifications() const noexcept { return channel_->notifications; }
std::expected<EffectivePolicy,OpenError> Service::effective_policy() const noexcept {
    if (channel_->owner!=getpid())
        return std::unexpected(OpenError{Error::TransportUnavailable,"service handle cannot be used after fork"});
    if (!ready())
        return std::unexpected(OpenError{Error::TransportUnavailable,"FLOWCORE daemon is offline"});
    try {
        std::string lease;
        {
            std::lock_guard lock(channel_->mutex);
            if (!ready()) return std::unexpected(OpenError{Error::TransportUnavailable,"FLOWCORE daemon is offline"});
            lease=channel_->lease;
        }
        detail::Transport transport(channel_->path);
        const auto reply=transport.exchange(channel_->envelope("inspect",lease)+"}");
        EffectivePolicy result;
        result.accepted_intent=channel_->requested;
        const auto read_u32=[&](std::string_view key,std::uint32_t maximum) {
            const auto value=integer(reply,key);
            if (value>maximum) throw detail::RpcError(Error::ProtocolError,"policy value exceeds protocol bound");
            return static_cast<std::uint32_t>(value);
        };
        const auto priority=read_u32("\"accepted_priority\": ",static_cast<std::uint32_t>(Priority::Critical));
        result.accepted_intent.priority=static_cast<Priority>(priority);
        result.accepted_intent.target_latency_us=read_u32("\"target_latency_us\": ",600000000);
        result.accepted_intent.deadline_us=read_u32("\"deadline_us\": ",600000000);
        result.accepted_intent.desired_kbit=read_u32("\"desired_kbit\": ",1000000);
        result.accepted_intent.minimum_kbit=read_u32("\"minimum_kbit\": ",1000000);
        result.accepted_intent.maximum_kbit=read_u32("\"maximum_kbit\": ",1000000);
        result.accepted_intent.lifetime_seconds=read_u32("\"lifetime_seconds\": ",3600);
        result.accepted_intent.prefer_reliable=protocol_boolean(reply,"\"prefer_reliable\": ");
        result.generation=integer(reply,"\"policy_generation\": ");
        result.kernel_verified=protocol_boolean(reply,"\"kernel_verified\": ");
        result.class_rate_kbit=read_u32("\"class_rate_kbit\": ",1000000);
        result.class_ceil_kbit=read_u32("\"class_ceil_kbit\": ",1000000);
        if (!result.accepted_intent.target_latency_us || !result.accepted_intent.lifetime_seconds ||
            (result.accepted_intent.minimum_kbit>result.accepted_intent.maximum_kbit &&
                result.accepted_intent.maximum_kbit))
            throw detail::RpcError(Error::ProtocolError,"inconsistent accepted policy values");
        result.state=result.kernel_verified?PolicyState::KernelVerified:PolicyState::RegisteredOnly;
        return result;
    } catch (const detail::RpcError& error) {
        return std::unexpected(OpenError{error.code,error.what()});
    } catch (const std::exception& error) {
        return std::unexpected(OpenError{Error::ProtocolError,error.what()});
    } catch (...) {
        return std::unexpected(OpenError{Error::ProtocolError,"unknown policy inspection failure"});
    }
}
std::unique_ptr<flowcore::udp::MessageSender> Service::udp_message_sender(int fd, bool enable_timestamps) const {
    if (channel_->owner!=getpid() || !ready()) throw std::runtime_error("daemon unavailable or service inherited across fork");
    std::lock_guard lock(channel_->mutex);
    if (!ready() || !channel_->udp_authorized || channel_->udp_expires_ns<=deadline::monotonic_ns())
        throw std::system_error(EPERM,std::generic_category(),"UDP per-message intent is not authorized for this service");
    if (!channel_->udp_budget)
        channel_->udp_budget=flowcore::udp::UrgencyBudget::create(channel_->udp_rate_pps,channel_->udp_burst,channel_->udp_expires_ns);
    return std::unique_ptr<flowcore::udp::MessageSender>(new flowcore::udp::MessageSender(
        fd,channel_->udp_socket_mark,channel_->udp_max_deadline_ms,channel_->udp_budget,enable_timestamps));
}
std::uint64_t Service::associate_socket(int fd) const {
    std::string token;
    { std::lock_guard lock(channel_->mutex);
      if (!ready()) throw std::runtime_error("daemon unavailable");
      token=channel_->lease; }
    detail::Transport transport(channel_->path);
    auto reply=transport.exchange(channel_->envelope("associate",token)+"}",fd);
    return std::stoull(field(reply,"\"cookie\": \""));
}
void Service::declare_transport(const TransportHints& hints) const {
    if (hints.session.empty() || hints.session.size()>64 ||
        hints.session.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")!=std::string::npos)
        throw std::invalid_argument("invalid logical session identifier");
    const auto index=static_cast<unsigned>(hints.kind);
    constexpr std::array<const char*,5> kinds{"tcp","mptcp","udp","quic","logical"};
    if (index>=kinds.size()) throw std::invalid_argument("unsupported transport kind");
    std::string token;
    { std::lock_guard lock(channel_->mutex);
      if (!ready()) throw std::runtime_error("daemon unavailable");
      token=channel_->lease; }
    static_cast<void>(flowcore::detail::exchange(channel_->path,
        channel_->envelope("transport_declare",token)+",\"transport\":{\"session\":\""+hints.session+
        "\",\"kind\":\""+kinds[index]+"\",\"multiplexed\":"+(hints.multiplexed?"true":"false")+
        ",\"mobility_preference\":"+(hints.mobility_preference?"true":"false")+
        ",\"cookie\":"+(hints.cookie?std::to_string(hints.cookie):"null")+"}}",-1));
}
void Service::report_transport_state(const std::string& session, bool degraded, const std::string& error) const {
    if (session.empty() || session.size()>64 ||
        session.find_first_not_of("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")!=std::string::npos)
        throw std::invalid_argument("invalid logical session identifier");
    constexpr std::array<const char*,7> errors{"none","handshake","timeout","reset","path","cancelled","other"};
    bool valid=false;
    for (const auto* value : errors) if (error==value) valid=true;
    if (!valid) throw std::invalid_argument("invalid transport error");
    std::string token;
    { std::lock_guard lock(channel_->mutex);
      if (!ready()) throw std::runtime_error("daemon unavailable");
      token=channel_->lease; }
    static_cast<void>(flowcore::detail::exchange(channel_->path,
        channel_->envelope("transport_state",token)+",\"session\":\""+session+
        "\",\"degraded\":"+(degraded?"true":"false")+",\"error\":\""+error+"\"}",-1));
}
void Service::set_socket_priority(int fd, unsigned priority) {
    if (priority>6) throw std::invalid_argument("ordinary socket priority must be 0..6");
    const int value=static_cast<int>(priority);
    if (setsockopt(fd,SOL_SOCKET,SO_PRIORITY,&value,sizeof(value)))
        throw std::system_error(errno,std::generic_category(),"SO_PRIORITY");
}
std::uint64_t Service::dissociate_socket(std::uint64_t cookie) const {
    if (!cookie) throw std::invalid_argument("socket cookie required");
    std::string token;
    { std::lock_guard lock(channel_->mutex);
      if (!ready()) throw std::runtime_error("daemon unavailable");
      token=channel_->lease; }
    auto reply=flowcore::detail::exchange(channel_->path,
        channel_->envelope("dissociate",token)+",\"cookie\":"+std::to_string(cookie)+"}",-1);
    return integer(reply,"\"abandoned\": ");
}
Request Service::begin_stream_request(std::uint64_t stream, unsigned priority,
                                      std::uint32_t deadline, std::uint64_t cookie) const {
    return Request(channel_,deadline,cookie,stream,priority,true);
}
Request Service::begin_stream_request(std::uint64_t stream, unsigned priority,
                                      std::chrono::microseconds deadline, std::uint64_t cookie) const {
    return begin_stream_request(stream,priority,microseconds(deadline),cookie);
}
deadline::Admission Service::admit(const deadline::Work& work, std::uint64_t cookie) const {
    const auto current = deadline::monotonic_ns();
    const auto fallback = deadline::estimate(work, {}, current);
    if (fallback.reason != deadline::Reason::MissingEvidence) return fallback;
    const auto unavailable = [&] {
        auto outcome = deadline::estimate(work, {}, deadline::monotonic_ns());
        if (outcome.reason == deadline::Reason::MissingEvidence) outcome.reason = deadline::Reason::Offline;
        return outcome;
    };
    std::string token;
    {
        const std::lock_guard lock(channel_->mutex);
        if (!ready() || !channel_->absolute_deadlines) return unavailable();
        token = channel_->lease;
    }
    try {
        const auto optional_us = [](std::optional<std::uint32_t> value) {
            return value ? std::to_string(*value) : std::string("null");
        };
        auto message = channel_->envelope("admission", token)+
            ",\"work\":{\"deadline_ns\":"+std::to_string(work.deadline_ns)+
            ",\"egress_bytes\":"+std::to_string(work.egress_bytes)+
            ",\"processing_us\":"+optional_us(work.processing_us)+
            ",\"connect_us\":"+optional_us(work.connect_us)+
            ",\"remote_us\":"+optional_us(work.remote_us)+
            ",\"reliability\":"+std::to_string(static_cast<unsigned>(work.reliability))+
            ",\"expiration\":"+std::to_string(static_cast<unsigned>(work.expiration))+
            ",\"cookie\":"+(cookie ? std::to_string(cookie) : "null")+"}}";
        detail::Transport transport(channel_->path);
        const auto reply = transport.exchange(std::move(message));
        auto number = [&](std::string_view key) { return integer(reply, key); };
        const auto decision = number("\"deadline_decision\": ");
        const auto reason = number("\"deadline_reason\": ");
        const auto quality = number("\"deadline_quality\": ");
        if (decision > static_cast<unsigned>(deadline::Decision::InsufficientData) ||
            reason > static_cast<unsigned>(deadline::Reason::Throttled) ||
            quality > static_cast<unsigned>(deadline::Quality::Observed)) return unavailable();
        auto result = deadline::estimate(work, {}, deadline::monotonic_ns());
        if (result.decision == deadline::Decision::RejectInfeasible) return result;
        result.decision = static_cast<deadline::Decision>(decision);
        result.reason = static_cast<deadline::Reason>(reason);
        result.quality = static_cast<deadline::Quality>(quality);
        auto read = [&](std::string_view key) -> std::optional<std::uint64_t> {
            auto value = number(key);
            return value == UINT64_MAX ? std::nullopt : std::optional{value};
        };
        result.capacity_bound_us = read("\"capacity_bound_us\": ");
        result.queue_us = read("\"queue_us\": ");
        result.network_p50_us = read("\"network_p50_us\": ");
        result.network_p95_us = read("\"network_p95_us\": ");
        // Account for time spent obtaining advice before returning it to the caller.
        if (result.network_p95_us && result.decision == deadline::Decision::Accept &&
            *result.network_p95_us > result.remaining_ns/1000 - result.reserved_us) {
            result.decision = deadline::Decision::AcceptWithRisk;
            result.reason = deadline::Reason::Tight;
        }
        return result;
    } catch (const detail::RpcError& error) {
        auto result = unavailable();
        if (result.reason != deadline::Reason::Expired && error.code == Error::AuthorizationDenied)
            result.reason = deadline::Reason::Unauthorized;
        return result;
    } catch (const std::exception&) {
        return unavailable();
    }
}

DeadlineRequest Service::begin_deadline_request(const deadline::Work& work, std::uint64_t cookie) const {
    if (!deadline::valid(work)) throw std::invalid_argument("invalid deadline work");
    return DeadlineRequest(Request(channel_, 0, cookie, 0, 0, false, work.deadline_ns), work);
}
DeadlineRequest Service::begin_deadline_stream(const deadline::Work& work, std::uint64_t stream,
                                                unsigned priority, std::uint64_t cookie) const {
    if (!deadline::valid(work)) throw std::invalid_argument("invalid deadline work");
    return DeadlineRequest(Request(channel_, 0, cookie, stream, priority, true, work.deadline_ns), work);
}
DeadlineRequest::DeadlineRequest(Request request, deadline::Work work) noexcept
    : request_(std::move(request)), work_(work) {}
DeadlineRequest::DeadlineRequest(DeadlineRequest&& other) noexcept
    : request_(std::move(other.request_)), work_(other.work_), completed_(other.completed_),
      finished_(std::exchange(other.finished_, true)) {}
DeadlineRequest& DeadlineRequest::operator=(DeadlineRequest&& other) noexcept {
    if (this==&other) return *this;
    request_=std::move(other.request_);
    work_=other.work_;
    completed_=other.completed_;
    finished_=std::exchange(other.finished_,true);
    return *this;
}
std::chrono::nanoseconds DeadlineRequest::remaining() const noexcept {
    const auto current = deadline::monotonic_ns();
    return std::chrono::nanoseconds(work_.deadline_ns > current ? work_.deadline_ns-current : 0);
}
bool DeadlineRequest::should_cancel() const noexcept {
    return !finished_ && work_.expiration == deadline::Expiration::CancelCooperatively && remaining().count() == 0;
}
bool DeadlineRequest::progress(std::uint64_t bytes) noexcept {
    if (finished_ || bytes < completed_ || bytes > work_.egress_bytes) return false;
    completed_ = bytes;
    return true;
}
deadline::Work DeadlineRequest::remaining_work() const noexcept {
    auto work = work_;
    work.egress_bytes -= completed_;
    return work;
}
std::optional<DeadlineCompletion> DeadlineRequest::complete(Outcome outcome) noexcept {
    if (std::exchange(finished_, true)) return {};
    const auto end = deadline::monotonic_ns();
    return DeadlineCompletion{outcome != Outcome::Ok || end > work_.deadline_ns, request_.finish_at(outcome, end)};
}
std::optional<DeadlineCompletion> DeadlineRequest::complete(Outcome outcome,
                                                             const TrafficAccounting& traffic) noexcept {
    if (finished_) return {};
    const auto end = deadline::monotonic_ns();
    const bool queued = request_.finish_at(outcome, end, &traffic);
    if (!queued) return {};
    finished_ = true;
    return DeadlineCompletion{outcome != Outcome::Ok || end > work_.deadline_ns, true};
}

Request::Request(std::shared_ptr<detail::Channel> channel, std::uint32_t deadline, std::uint64_t cookie,
                 std::uint64_t stream, unsigned priority, bool logical, std::uint64_t absolute)
    : channel_(std::move(channel)), start_(deadline::monotonic_ns()), generation_(channel_->generation), cookie_(cookie),
      deadline_(deadline ? deadline : channel_->default_deadline),stream_(stream),priority_(priority),logical_(logical) {
    if (priority>7 || stream>=(1ULL<<62)) throw std::invalid_argument("invalid logical stream metadata");
    if (absolute) {
        if (absolute <= start_ || absolute-start_ > deadline::maximum_budget_ns)
            throw std::invalid_argument("absolute deadline must be in the supported future window");
        deadline_=static_cast<std::uint32_t>((absolute-start_+999)/1000);
        if (channel_->absolute_deadlines) absolute_ns_=absolute;
    }
    if (deadline_>600000000) throw std::invalid_argument("deadline outside supported range");
    id_=++channel_->sequence;
    if (!channel_->push({id_,start_,0,generation_,cookie_,deadline_,0,stream_,priority_,logical_,absolute_ns_})) channel_.reset();
}
Request::Request(Request&& other) noexcept
    : channel_(std::move(other.channel_)),id_(other.id_),start_(other.start_),
      generation_(other.generation_),cookie_(other.cookie_),deadline_(other.deadline_),stream_(other.stream_),absolute_ns_(other.absolute_ns_),priority_(other.priority_),logical_(other.logical_) {}
Request& Request::operator=(Request&& other) noexcept {
    if (this==&other) return *this;
    if (channel_) static_cast<void>(complete(Outcome::Cancelled));
    channel_=std::move(other.channel_);
    id_=other.id_;start_=other.start_;generation_=other.generation_;cookie_=other.cookie_;
    deadline_=other.deadline_;stream_=other.stream_;absolute_ns_=other.absolute_ns_;
    priority_=other.priority_;logical_=other.logical_;
    other.id_=0;
    return *this;
}
Request::~Request() { if (channel_) static_cast<void>(complete(Outcome::Cancelled)); }
bool Request::complete(Outcome outcome) noexcept { return finish_at(outcome, deadline::monotonic_ns()); }
bool Request::complete(Outcome outcome, const TrafficAccounting& traffic) noexcept {
    return finish_at(outcome, deadline::monotonic_ns(), &traffic);
}
bool Request::finish_at(Outcome outcome, std::uint64_t end, const TrafficAccounting* traffic) noexcept {
    if (traffic && traffic->valid &&
        (traffic->bytes_offered > (1ULL << 40) || traffic->bytes_admitted > (1ULL << 40) ||
         traffic->bytes_transmitted > (1ULL << 40) || traffic->bytes_received > (1ULL << 40) ||
         traffic->bytes_admitted > traffic->bytes_offered ||
         traffic->bytes_transmitted > traffic->bytes_admitted ||
         traffic->udp_messages_attempted > 1'000'000 || traffic->udp_messages_transmitted > 1'000'000 ||
         traffic->udp_send_failures > 1'000'000 || traffic->udp_urgent_messages > 1'000'000 ||
         traffic->udp_background_messages > 1'000'000 ||
         traffic->udp_messages_transmitted > traffic->udp_messages_attempted ||
         traffic->udp_send_failures > traffic->udp_messages_attempted ||
         traffic->udp_urgent_messages > traffic->udp_messages_attempted ||
         traffic->udp_background_messages > traffic->udp_messages_attempted-traffic->udp_urgent_messages))
        return false;
    auto channel=std::move(channel_);
    if (!channel) return false;
    unsigned phase=outcome==Outcome::Ok?1:outcome==Outcome::Timeout?2:outcome==Outcome::Failure?3:4;
    detail::Event event{id_,start_,end,generation_,cookie_,deadline_,phase,stream_,priority_,logical_,absolute_ns_};
    if (traffic) event.traffic=*traffic;
    return channel->push(event);
}
}
