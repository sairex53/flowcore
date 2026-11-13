#include <linux/tcp.h>
#include <cstddef>

template <typename T>
void check_layout() {
    if constexpr (requires(T value) { value.tcpi_state; }) {
        static_assert(offsetof(T, tcpi_state) == 0);
    }
    if constexpr (requires(T value) { value.tcpi_ca_state; }) {
        static_assert(offsetof(T, tcpi_ca_state) == 1);
    }
    if constexpr (requires(T value) { value.tcpi_retransmits; }) {
        static_assert(offsetof(T, tcpi_retransmits) == 2);
    }
    if constexpr (requires(T value) { value.tcpi_rto; }) {
        static_assert(offsetof(T, tcpi_rto) == 8);
    }
    if constexpr (requires(T value) { value.tcpi_snd_mss; }) {
        static_assert(offsetof(T, tcpi_snd_mss) == 16);
    }
    if constexpr (requires(T value) { value.tcpi_unacked; }) {
        static_assert(offsetof(T, tcpi_unacked) == 24);
    }
    if constexpr (requires(T value) { value.tcpi_lost; }) {
        static_assert(offsetof(T, tcpi_lost) == 32);
    }
    if constexpr (requires(T value) { value.tcpi_retrans; }) {
        static_assert(offsetof(T, tcpi_retrans) == 36);
    }
    if constexpr (requires(T value) { value.tcpi_rtt; }) {
        static_assert(offsetof(T, tcpi_rtt) == 68);
    }
    if constexpr (requires(T value) { value.tcpi_rttvar; }) {
        static_assert(offsetof(T, tcpi_rttvar) == 72);
    }
    if constexpr (requires(T value) { value.tcpi_snd_cwnd; }) {
        static_assert(offsetof(T, tcpi_snd_cwnd) == 80);
    }
    if constexpr (requires(T value) { value.tcpi_rcv_space; }) {
        static_assert(offsetof(T, tcpi_rcv_space) == 96);
    }
    if constexpr (requires(T value) { value.tcpi_total_retrans; }) {
        static_assert(offsetof(T, tcpi_total_retrans) == 100);
    }
    if constexpr (requires(T value) { value.tcpi_pacing_rate; }) {
        static_assert(offsetof(T, tcpi_pacing_rate) == 104);
    }
    if constexpr (requires(T value) { value.tcpi_max_pacing_rate; }) {
        static_assert(offsetof(T, tcpi_max_pacing_rate) == 112);
    }
    if constexpr (requires(T value) { value.tcpi_bytes_acked; }) {
        static_assert(offsetof(T, tcpi_bytes_acked) == 120);
    }
    if constexpr (requires(T value) { value.tcpi_bytes_received; }) {
        static_assert(offsetof(T, tcpi_bytes_received) == 128);
    }
    if constexpr (requires(T value) { value.tcpi_segs_out; }) {
        static_assert(offsetof(T, tcpi_segs_out) == 136);
    }
    if constexpr (requires(T value) { value.tcpi_segs_in; }) {
        static_assert(offsetof(T, tcpi_segs_in) == 140);
    }
    if constexpr (requires(T value) { value.tcpi_notsent_bytes; }) {
        static_assert(offsetof(T, tcpi_notsent_bytes) == 144);
    }
    if constexpr (requires(T value) { value.tcpi_min_rtt; }) {
        static_assert(offsetof(T, tcpi_min_rtt) == 148);
    }
    if constexpr (requires(T value) { value.tcpi_data_segs_in; }) {
        static_assert(offsetof(T, tcpi_data_segs_in) == 152);
    }
    if constexpr (requires(T value) { value.tcpi_data_segs_out; }) {
        static_assert(offsetof(T, tcpi_data_segs_out) == 156);
    }
    if constexpr (requires(T value) { value.tcpi_delivery_rate; }) {
        static_assert(offsetof(T, tcpi_delivery_rate) == 160);
    }
    if constexpr (requires(T value) { value.tcpi_busy_time; }) {
        static_assert(offsetof(T, tcpi_busy_time) == 168);
    }
    if constexpr (requires(T value) { value.tcpi_rwnd_limited; }) {
        static_assert(offsetof(T, tcpi_rwnd_limited) == 176);
    }
    if constexpr (requires(T value) { value.tcpi_sndbuf_limited; }) {
        static_assert(offsetof(T, tcpi_sndbuf_limited) == 184);
    }
    if constexpr (requires(T value) { value.tcpi_delivered; }) {
        static_assert(offsetof(T, tcpi_delivered) == 192);
    }
    if constexpr (requires(T value) { value.tcpi_delivered_ce; }) {
        static_assert(offsetof(T, tcpi_delivered_ce) == 196);
    }
    if constexpr (requires(T value) { value.tcpi_bytes_sent; }) {
        static_assert(offsetof(T, tcpi_bytes_sent) == 200);
    }
    if constexpr (requires(T value) { value.tcpi_bytes_retrans; }) {
        static_assert(offsetof(T, tcpi_bytes_retrans) == 208);
    }
    if constexpr (requires(T value) { value.tcpi_snd_wnd; }) {
        static_assert(offsetof(T, tcpi_snd_wnd) == 228);
    }
    if constexpr (requires(T value) { value.tcpi_rcv_wnd; }) {
        static_assert(offsetof(T, tcpi_rcv_wnd) == 232);
    }
    if constexpr (requires(T value) { value.tcpi_total_rto; }) {
        static_assert(offsetof(T, tcpi_total_rto) == 240);
    }
    if constexpr (requires(T value) { value.tcpi_total_rto_recoveries; }) {
        static_assert(offsetof(T, tcpi_total_rto_recoveries) == 242);
    }
    if constexpr (requires(T value) { value.tcpi_total_rto_time; }) {
        static_assert(offsetof(T, tcpi_total_rto_time) == 244);
    }
    if constexpr (requires(T value) { value.tcpi_received_ce; }) {
        static_assert(offsetof(T, tcpi_received_ce) == 248);
    }
    if constexpr (requires(T value) { value.tcpi_received_ce_bytes; }) {
        static_assert(offsetof(T, tcpi_delivered_e1_bytes) == 252);
        static_assert(offsetof(T, tcpi_received_ce_bytes) == 272);
        static_assert(sizeof(T) >= 280);
    }
}

int main() {
    check_layout<tcp_info>();
}
