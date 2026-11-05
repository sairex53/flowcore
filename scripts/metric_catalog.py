"""Stable meanings and availability rules, shared by all JSON snapshots."""
from tcp_diag import INFO

DESCRIPTIONS = {
 'rtt_us':'Kernel smoothed RTT, sampled per socket. Not request latency.',
 'rttvar_us':'Kernel RTT mean deviation estimate.',
 'rto_us':'Current retransmission timeout estimate, not a timeout count.',
 'total_rto':'Lifetime RTO timeout count including handshake and recurring timeouts.',
 'total_rto_recoveries':'Lifetime RTO recovery periods including unfinished recovery.',
 'total_rto_time_ms':'Lifetime time in RTO recovery, including unfinished recovery.',
 'cwnd_segments':'Current sender congestion window in segments.',
 'peer_receive_window_bytes':'Scaled receive window advertised by the peer.',
 'local_receive_window_bytes':'Scaled local advertised receive window.',
 'rcv_space_bytes':'Kernel receive-space estimate, distinct from advertised receive window.',
 'delivery_rate_bytes_per_second':'Last delivery-rate estimate. Not sustained interface goodput.',
 'state_code':'Linux TCP state enumeration at this snapshot.',
 'send_mss_bytes':'Current send maximum segment payload size.',
 'unacked_segments':'Outstanding unacknowledged segments.',
 'lost_segments':'Segments currently considered lost by TCP.',
 'total_retrans_segments':'Lifetime retransmitted segments, including handshake retransmissions.',
 'bytes_acked':'Lifetime data octets acknowledged, kernel TCP accounting.',
 'bytes_received':'Lifetime data octets received.',
 'data_segments_in':'Lifetime received TCP data segments.',
 'data_segments_out':'Lifetime transmitted data segments, including retransmissions.',
 'segments_in':'Lifetime received TCP segments including control segments.',
 'segments_out':'Lifetime sent TCP segments including control segments.',
 'notsent_bytes':'Data buffered but not yet sent.',
 'min_rtt_us':'Minimum RTT observed by TCP.',
 'bytes_sent':'Lifetime transmitted data octets including retransmitted data.',
 'bytes_retrans':'Lifetime retransmitted data octets.',
 'busy_time_us':'Cumulative time busy sending.',
 'receive_window_limited_us':'Cumulative sending time limited by peer receive window.',
 'send_buffer_limited_us':'Cumulative sending time limited by sender buffer.',
 'delivered_segments':'Segments delivered to receiver according to TCP delivery accounting.',
 'delivered_ce_segments':'Delivered segments reported with ECN congestion experienced marks.',
 'current_retransmits':'Current retry/backoff state, not lifetime retransmissions.',
 'current_retrans_segments':'Currently retransmitted unacknowledged segments.',
 'congestion_state_code':'TCP_CA state from Linux UAPI.',
}


def catalog():
    result = {}
    for name,(offset,fmt) in INFO.items():
        unit = ('bytes_per_second' if name.endswith('bytes_per_second') else
                'microseconds' if name.endswith('_us') else 'milliseconds' if name.endswith('_ms') else
                'bytes' if 'bytes' in name else 'segments' if 'segments' in name else 'count_or_code')
        result[name] = dict(unit=unit,source='TCP_INFO via INET_DIAG or owned socket getsockopt',
                            available_when=f'returned TCP_INFO length >= {offset+{"B":1,"H":2,"I":4,"Q":8}[fmt]}',
                            meaning=DESCRIPTIONS.get(name,'Linux TCP_INFO '+name+'; sampled socket value'))
    return dict(version=1,tcp=result,
                absent_fields='Unavailable fields are omitted. A returned zero is a kernel value.',
                process_identity='External trace context is not socket owner. PID remains unknown.',
                buffers='INET_DIAG SKMEMINFO and owned SO_SNDBUF/SO_RCVBUF are kernel allocations or limits.',
                lifecycle='Trace events cover attached hooks; INET_DIAG absence has unknown disposition.',
                queues='tc readback in owned lab; native rtnetlink root stats and interface counters on host.')
