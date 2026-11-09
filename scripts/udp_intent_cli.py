"""Read-only discovery of UDP message-intent socket and scheduler capabilities."""
import ctypes
import errno
import json
import os
import re
import socket
import struct
import subprocess
import time
from pathlib import Path

from paths import TC

def _probe(family, address, controls):
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.bind(address)
            return sock.sendmsg([b"flowcore-capability-probe"], controls,
                                socket.MSG_DONTWAIT, sock.getsockname()) > 0
    except OSError:
        return False

def _sendmmsg():
    try:
        call = ctypes.CDLL(None, use_errno=True).sendmmsg
    except AttributeError:
        return False
    call.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint)
    return call(-1, None, 1, 0) == -1 and ctypes.get_errno() == errno.EBADF

def capabilities(interface=None):
    priority = (socket.SOL_SOCKET, getattr(socket, "SO_PRIORITY", 12), struct.pack("=i", 6))
    tos = struct.pack("=i", 46 << 2)
    report = {
        "version": 1,
        "platform": "linux" if sys_platform_linux() else "unsupported",
        "message_priority": _probe(socket.AF_INET, ("127.0.0.1", 0), [priority]),
        "ipv4_tos_ancillary": _probe(socket.AF_INET, ("127.0.0.1", 0),
                                     [(socket.IPPROTO_IP, socket.IP_TOS, tos), priority]),
        "ipv6_tclass_ancillary": _probe(socket.AF_INET6, ("::1", 0),
                                        [(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, tos), priority]),
        "sendmmsg": _sendmmsg(),
        "software_tx_timestamping": False,
        "txtime_socket_option": False,
        "queueing_modules": {
            name: Path("/sys/module", "sch_" + name).exists()
            for name in ("fq_codel", "cake", "etf", "taprio")
        },
        "active_qdiscs": None,
        "timestamp_domain": "CLOCK_REALTIME for software TX timestamp reports; deadline inputs use CLOCK_MONOTONIC",
        "modifies_network_state": False,
    }
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            flags = (1 << 4) | (1 << 7) | (1 << 11)
            sock.setsockopt(socket.SOL_SOCKET, 65, struct.pack("=I", flags))
            report["software_tx_timestamping"] = True
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # struct sock_txtime { clockid_t clockid; __u32 flags; }, SOF_TXTIME_REPORT_ERRORS=2.
            sock.setsockopt(socket.SOL_SOCKET, 61, struct.pack("=iI", getattr(time, "CLOCK_MONOTONIC", 1), 2))
            report["txtime_socket_option"] = True
    except OSError:
        pass
    if interface is not None:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,15}", interface):
            raise ValueError("invalid interface name")
        socket.if_nametoindex(interface)
        if TC.is_file():
            result = subprocess.run([str(TC), "-j", "qdisc", "show", "dev", interface],
                                    capture_output=True, text=True, timeout=3, check=False)
            if result.returncode == 0:
                report["active_qdiscs"] = json.loads(result.stdout)
            else:
                report["active_qdiscs"] = {"error": result.stderr.strip()[:512]}
    return report

def sys_platform_linux():
    return os.name == "posix" and Path("/proc/sys/kernel/osrelease").exists()

def dispatch(args):
    if args.action != "capabilities":
        raise ValueError("unsupported UDP intent diagnostic")
    print(json.dumps(capabilities(args.interface), indent=None if args.json else 2))
    return 0
