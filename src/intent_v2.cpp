#include "flowcore/intent_v2.hpp"
#include "intent_transport.hpp"
#include <array>
#include <atomic>
#include <condition_variable>
#include <ctime>
#include <mutex>
#include <stdexcept>
#include <string_view>
#include <thread>
#include <utility>
#include <unistd.h>

namespace flowcore::detail {
std::string exchange(const std::string&, std::string, int);
}
namespace flowcore::v2 {
namespace {
std::uint64_t now() noexcept {
    timespec value{};
    clock_gettime(CLOCK_MONOTONIC, &value);
    return static_cast<std::uint64_t>(value.tv_sec)*1000000000ULL+value.tv_nsec;
}
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
};
struct Channel {
    const pid_t owner=getpid();
    const std::string path, registration;
    const std::uint32_t default_deadline,lifetime;
    Transport transport;
    std::mutex mutex;
    std::condition_variable changed;
    std::array<Event,512> queue{};
    std::size_t head=0, count=0;
    bool stopping=false, busy=false;
    std::string lease;
    std::atomic<bool> connected{false};
    std::atomic<std::uint64_t> sequence{0}, generation{0}, queued{0}, accepted{0}, dropped{0}, rejected{0}, errors{0};
    std::atomic<std::uint32_t> notifications{0};
    std::atomic<Error> last_error{Error::None};
    std::atomic<std::uint64_t> notification_time{0};
    std::uint64_t renew_after=0;
    bool first=true,can_renew=false;
    std::thread worker;

    Channel(std::string socket, const Intent& intent)
        : path(std::move(socket)), registration(make_registration(intent)), default_deadline(intent.deadline_us),lifetime(intent.lifetime_seconds),transport(path),
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
                    if (next.size()!=32 || next.find_first_not_of("0123456789abcdef")!=std::string::npos)
                        throw std::runtime_error("invalid lease");
                    { std::lock_guard lock(mutex); lease=next; token=next; }
                    can_renew=reply.find("\"renewable\": true")!=std::string::npos;
                    notifications=0;notification_time=0;
                    ++generation;
                    connected=true;
                    renew_after=now()+static_cast<std::uint64_t>(lifetime)*500000000ULL;
                    last_error=Error::None;
                    dropped+=size;
                } else {
                    if (can_renew && now()>=renew_after) {
                        transport.exchange(envelope("renew",token)+"}");
                        renew_after=now()+static_cast<std::uint64_t>(lifetime)*500000000ULL;
                    }
                    std::string message=envelope("batch",token)+",\"events\":[";
                    std::size_t sent=0;
                    constexpr const char* phases[]={"start","ok","timeout","failure","cancelled"};
                    for (std::size_t i=0;i<size;++i) {
                        const auto& e=batch[i];
                        if (e.generation!=generation) { ++dropped; continue; }
                        if (sent++) message+=",";
                        message+="{\"id\":"+std::to_string(e.id)+",\"phase\":\""+phases[e.phase]+
                            "\",\"start_ns\":"+std::to_string(e.start)+",\"end_ns\":"+std::to_string(e.end)+
                            ",\"deadline_us\":"+std::to_string(e.deadline)+",\"cookie\":"+(e.cookie?std::to_string(e.cookie):"null")+"}";
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
                }
            } catch (const RpcError& error) {
                ++errors; dropped+=size; last_error=error.code;
                if (error.retry_registration) connected=false;
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            } catch (const std::exception& error) {
                ++errors; dropped+=size; last_error=Error::TransportUnavailable;
                // Preserve a known lease on transport ambiguity; retries must not leak leases.
                // A restarted daemon rejects the stale lease and permits fresh registration.
                if (std::string_view(error.what())=="FLOWCORE operation forbidden") { connected=false;last_error=Error::AuthorizationDenied; }
                std::this_thread::sleep_for(std::chrono::milliseconds(50));
            } catch (...) {
                ++errors; dropped+=size; connected=false; last_error=Error::ProtocolError;
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
Request Service::begin_request(std::uint32_t deadline, std::uint64_t cookie) const { return Request(channel_,deadline,cookie); }
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
Error Service::last_error() const noexcept { return channel_->last_error; }
std::uint64_t Service::registration_generation() const noexcept { return channel_->generation; }
std::uint64_t Service::notifications_time_ns() const noexcept { return channel_->notification_time; }
std::uint32_t Service::notifications() const noexcept { return channel_->notifications; }
std::uint64_t Service::associate_socket(int fd) const {
    std::string token;
    { std::lock_guard lock(channel_->mutex);
      if (!ready()) throw std::runtime_error("daemon unavailable");
      token=channel_->lease; }
    auto reply=flowcore::detail::exchange(channel_->path,channel_->envelope("associate",token)+"}",fd);
    return std::stoull(field(reply,"\"cookie\": \""));
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
Request::Request(std::shared_ptr<detail::Channel> channel, std::uint32_t deadline, std::uint64_t cookie)
    : channel_(std::move(channel)), start_(now()), generation_(channel_->generation), cookie_(cookie),
      deadline_(deadline ? deadline : channel_->default_deadline) {
    if (deadline_>600000000) throw std::invalid_argument("deadline outside supported range");
    id_=++channel_->sequence;
    if (!channel_->push({id_,start_,0,generation_,cookie_,deadline_,0})) channel_.reset();
}
Request::Request(Request&& other) noexcept
    : channel_(std::move(other.channel_)),id_(other.id_),start_(other.start_),
      generation_(other.generation_),cookie_(other.cookie_),deadline_(other.deadline_) {}
Request::~Request() { if (channel_) static_cast<void>(complete(Outcome::Cancelled)); }
bool Request::complete(Outcome outcome) noexcept {
    auto channel=std::move(channel_);
    if (!channel) return false;
    unsigned phase=outcome==Outcome::Ok?1:outcome==Outcome::Timeout?2:outcome==Outcome::Failure?3:4;
    return channel->push({id_,start_,now(),generation_,cookie_,deadline_,phase});
}
}
