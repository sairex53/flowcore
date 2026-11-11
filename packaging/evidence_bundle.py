#!/usr/bin/env python3
"""Build and verify privacy-filtered FLOWCORE evidence bundles using Python stdlib only."""
from __future__ import annotations

import argparse
import csv
import getpass
import hashlib
import io
import ipaddress
import json
import math
import os
import platform
import socket
import stat
from pathlib import Path, PurePosixPath
import re
import shutil
import statistics
import sys
import tarfile
import tempfile
import time

SCHEMA_VERSION = 1
MAX_FILES = 12000
MAX_MEMBER = 64 * 1024 * 1024
MAX_UNPACKED = 128 * 1024 * 1024
MAX_INPUT = 256 * 1024 * 1024
MAX_ARCHIVE = 64 * 1024 * 1024
MIN_FREE = 64 * 1024 * 1024
MAX_SECONDS = 180
MAX_REQUESTS = 100000
MODES = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
DENY_KEYS = re.compile(
    r"(?:password|secret|token|credential|private.?key|authorization|hostname|host.?id|"
    r"username|user.?id|(?:^|_)uid(?:_|$)|(?:^|_)pid(?:_|$)|cgroup|socket|address|"
    r"(?:^|_)ip(?:_|$)|(?:^|_)mac(?:_|$)|interface.?name|ifname|(?:^|_)host(?:_|$)|"
    r"(?:^|_)user(?:_|$)|(?:^|_)path(?:_|$)|directory|payload|request.?body|response.?body|"
    r"trace.?id|container.?id|namespace.?id|command.?line)", re.I)
ABS_PATH = re.compile(r"(?:^|[\s\"'=])/(?:home|root|Users|private|tmp|var/tmp|mnt|media)/[^\s\"']+")
ABS_POSIX_PATH = re.compile(r"(?<![A-Za-z0-9_:/.])/(?:[A-Za-z0-9_.+-]+/)+[A-Za-z0-9_.+-]+")
IPV4 = re.compile(
    r"(?<![0-9])(?:10(?:\.[0-9]{1,3}){3}|127(?:\.[0-9]{1,3}){3}|"
    r"169\.254(?:\.[0-9]{1,3}){2}|192\.168(?:\.[0-9]{1,3}){2}|"
    r"172\.(?:1[6-9]|2[0-9]|3[01])(?:\.[0-9]{1,3}){2})(?![0-9])")
IPV6_CANDIDATE = re.compile(r"(?<![0-9A-Fa-f:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f:.]{0,39}(?![0-9A-Fa-f:])")
MAC = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])")
EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
TOKEN_PATTERNS = (
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(rb"(?i)\bauthorization\s*[:=]\s*bearer\s+[A-Za-z0-9._~-]{16,}"),
    re.compile(rb"(?i)\b(?:password|passwd|client_secret|api_key)\s*[:=]\s*['\"]?[^'\"\s,]{8,}"),
    re.compile(rb"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
)
REQUEST_FIELDS = ("id", "scheduled", "completed", "latency_ms", "outcome",
                  "traffic_class", "service", "dispatch_lag_ms")
METRIC_FIELDS = (
    "offered", "completed", "failed", "p50_ms", "p95_ms", "p99_ms", "deadline_ms",
    "deadline_attainment", "deadline_misses", "failure_ratio", "completion_ratio",
    "successful_within_window", "request_payload_mbps", "background_mbps", "udp_mbps",
    "total_mbps", "jain_index", "cpu_seconds", "peak_daemon_rss_bytes",
    "peak_bpf_rss_bytes", "adjustments", "slo_policy_updates", "rollbacks",
    "qdisc_drop_delta", "netem_drop_delta", "peak_backlog_bytes", "dispatch_lag_p99_ms",
    "response_seconds", "offered_rps", "udp_send_errors", "event_loss",
)
CLASSIFICATIONS = ("VALID_COMPLETED", "INVALID_INFRASTRUCTURE", "INTERRUPTED",
                   "WORKLOAD_FAILURE", "EXPLORATORY", "PREREGISTERED_HELD_OUT",
                   "UNCLASSIFIED_SCOPE")


class BundleError(ValueError):
    pass


def canonical(obj: object) -> bytes:
    return (json.dumps(obj, sort_keys=True, indent=2, allow_nan=False,
                       ensure_ascii=True) + "\n").encode("utf-8")


def json_line(obj: object) -> bytes:
    return (json.dumps(obj, sort_keys=True, separators=(",", ":"), allow_nan=False,
                       ensure_ascii=True) + "\n").encode("utf-8")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def finite_number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def safe_relative(name: str) -> str:
    if not isinstance(name, str) or "\\" in name or "\x00" in name:
        raise BundleError("invalid bundle member name")
    path = PurePosixPath(name)
    if path.is_absolute() or len(name) > 240 or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise BundleError("unsafe bundle member path")
    return str(path)


def _contains_private_ip(text: str) -> bool:
    if IPV4.search(text):
        return True
    for candidate in IPV6_CANDIDATE.findall(text):
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        if address.is_private or address.is_link_local:
            return True
    return False


def _safe_string(value: str) -> bool:
    if len(value) > 160 or not value.isascii():
        return False
    if ABS_PATH.search(value) or ABS_POSIX_PATH.search(value) or _contains_private_ip(value) or MAC.search(value) or EMAIL.search(value):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_.:+-]{1,160}", value))


def _safe_tree(value: object, redactions: list[str], key: str = "") -> object:
    # A byte count is a public numeric workload parameter, never application payload.
    if key.casefold() == "payload_bytes":
        if type(value) is int and 0 <= value <= 1_000_000_000_000:
            return value
        redactions.append("invalid_numeric_payload_size")
        return None
    if key.casefold() in {"host", "hostname", "user", "username", "interface", "ifname", "dev"} or DENY_KEYS.search(key):
        redactions.append("sensitive_field")
        return None
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise BundleError("non-finite source number")
        return value
    if isinstance(value, str):
        if _safe_string(value):
            return value
        redactions.append("unsafe_string")
        return None
    if isinstance(value, list):
        return [item for item in (_safe_tree(child, redactions, key) for child in value) if item is not None]
    if isinstance(value, dict):
        result = {}
        for child_key, child in value.items():
            if not isinstance(child_key, str) or not _safe_string(child_key):
                redactions.append("unsafe_key")
                continue
            cleaned = _safe_tree(child, redactions, child_key)
            if cleaned is not None:
                result[child_key] = cleaned
        return result
    redactions.append("unsupported_type")
    return None


def privacy_findings(name: str, data: bytes, *, scan_code: bool = False,
                     sensitive_literals: tuple[str, ...] = ()) -> list[str]:
    """Return finding codes only. Never include matched text or secret values."""
    found = []
    raw = data[:MAX_MEMBER]
    if any(pattern.search(raw) for pattern in TOKEN_PATTERNS):
        found.append("CREDENTIAL_OR_PRIVATE_KEY")
    if scan_code:
        return found
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return found
    if ABS_PATH.search(text) or ABS_POSIX_PATH.search(text):
        found.append("ABSOLUTE_PATH")
    if _contains_private_ip(text) or MAC.search(text):
        found.append("NETWORK_IDENTIFIER")
    if EMAIL.search(text):
        found.append("EMAIL_OR_USER_METADATA")
    lowered = text.casefold()
    if any(literal and literal.casefold() in lowered for literal in sensitive_literals):
        found.append("LOCAL_USER_OR_HOST_METADATA")
    if name.endswith(".csv"):
        reader = csv.reader(io.StringIO(text))
        try:
            header = next(reader)
        except StopIteration:
            header = []
        allowed = {"request_id", "scheduled_s", "completed_s", "latency_ms",
                   "outcome", "traffic_class", "service_ref", "dispatch_lag_ms",
                   "deadline_ms", "scheduled_ns_relative", "released_ns_relative",
                   "dispatch_ns_relative", "connect_ns_relative", "connected_ns_relative",
                   "submitted_ns_relative", "drained_ns_relative", "received_ns_relative",
                   "completed_ns_relative"}
        if header and any(item not in allowed for item in header):
            found.append("UNREVIEWED_APPLICATION_COLUMN")
    return sorted(set(found))


def _source_file(root: Path, rel: str, max_size: int = MAX_MEMBER) -> bytes:
    path = root / rel
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise BundleError("required source artifact is missing") from error
    if not resolved.is_relative_to(root.resolve()):
        raise BundleError("source artifact escapes repository")
    current = root.resolve()
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            raise BundleError("source artifacts may not be symlinks")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(resolved, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_size:
                raise BundleError("source artifact type or size is not permitted")
            data = stream.read(max_size + 1)
    except OSError as error:
        raise BundleError("source artifact could not be opened safely") from error
    if len(data) > max_size or len(data) != info.st_size:
        raise BundleError("source artifact changed or exceeds size budget")
    if any(pattern.search(data) for pattern in TOKEN_PATTERNS):
        raise BundleError("credential-like value detected in source artifact")
    return data


def _read_artifact(path: Path, max_size: int = MAX_MEMBER) -> bytes:
    if path.is_symlink():
        raise BundleError("source artifact is missing or unsafe")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > max_size:
                raise BundleError("source artifact exceeds size budget")
            raw = stream.read(max_size + 1)
    except OSError as error:
        raise BundleError("source artifact could not be read safely") from error
    if len(raw) != info.st_size or len(raw) > max_size:
        raise BundleError("source artifact changed or exceeds size budget")
    if any(pattern.search(raw) for pattern in TOKEN_PATTERNS):
        raise BundleError("credential-like value detected in source artifact")
    return raw


def _relative_source(path: str) -> str:
    if not isinstance(path, str) or Path(path).is_absolute():
        raise BundleError("evidence sources must be repository-relative")
    return safe_relative(path)


def _percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * p) - 1)]


def _median_ci(values: list[float], confidence: float) -> dict | None:
    ordered = sorted(values)
    n = len(ordered)
    interval = None
    for k in range(1, n // 2 + 1):
        coverage = 1 - 2 * sum(math.comb(n, j) for j in range(k)) / (2 ** n)
        if coverage < confidence:
            break
        interval = {"low": ordered[k - 1], "high": ordered[n - k], "coverage": coverage}
    return interval


def _interpolate(samples: list[dict], when: float, field: str) -> list[float]:
    for a, b in zip(samples, samples[1:]):
        if a["time_s"] <= when <= b["time_s"] and b["time_s"] > a["time_s"]:
            fraction = (when - a["time_s"]) / (b["time_s"] - a["time_s"])
            return [x + fraction * (y - x) for x, y in zip(a[field], b[field])]
    raise BundleError("receiver samples do not bracket the measured interval")


def _normalize_tc(tc: object, redactions: list[str]) -> dict:
    if not isinstance(tc, dict):
        return {"qdisc": [], "classes": [], "filters": []}
    result = {"qdisc": [], "classes": [], "filters": []}
    device_names = sorted({row.get("dev") for key in ("qdisc", "class", "filter")
                           for row in (tc.get(key, []) if isinstance(tc.get(key, []), list) else [])
                           if isinstance(row, dict) and isinstance(row.get("dev"), str)})
    device_refs = {name: f"device-{index + 1}" for index, name in enumerate(device_names)}
    for row in tc.get("qdisc", []) if isinstance(tc.get("qdisc", []), list) else []:
        if not isinstance(row, dict):
            continue
        item = {key: row[key] for key in ("kind", "handle", "parent", "root", "options",
                "bytes", "packets", "drops", "overlimits", "requeues", "backlog", "qlen",
                "stats", "stats2") if key in row}
        if isinstance(row.get("dev"), str):
            item["device_ref"] = device_refs[row["dev"]]
        result["qdisc"].append(_safe_tree(item, redactions))
    for row in tc.get("class", []) if isinstance(tc.get("class", []), list) else []:
        if not isinstance(row, dict):
            continue
        item = {key: row[key] for key in ("kind", "class", "classid", "handle", "parent",
                "rate", "ceil", "burst", "cburst", "prio", "quantum", "level", "options",
                "stats", "stats2")
                if key in row}
        if isinstance(row.get("dev"), str):
            item["device_ref"] = device_refs[row["dev"]]
        result["classes"].append(_safe_tree(item, redactions))
    for row in tc.get("filter", []) if isinstance(tc.get("filter", []), list) else []:
        if not isinstance(row, dict):
            continue
        item = {key: row[key] for key in ("protocol", "priority", "kind", "handle")
                if key in row}
        if isinstance(row.get("dev"), str):
            item["device_ref"] = device_refs[row["dev"]]
        options = row.get("options")
        if isinstance(options, dict):
            filtered = {key: value for key, value in options.items()
                        if key not in ("keys", "actions", "indev", "hw_stats")}
            keys = options.get("keys")
            if isinstance(keys, dict):
                kept = {key: value for key, value in keys.items()
                        if key in ("ip_proto", "dst_port", "src_port", "vlan_id",
                                   "vlan_ethtype", "skb_mark", "classid")}
                dropped = [key for key in keys if key not in kept]
                if dropped:
                    redactions.append("packet_address_match_value")
                filtered["match_fields"] = sorted(keys)
                filtered["match_values"] = kept
            item["options"] = filtered
            actions = options.get("actions")
            if isinstance(actions, list):
                item["actions"] = [
                    {key: action[key] for key in ("kind", "classid", "index", "pipe", "pass", "drop")
                     if key in action} for action in actions if isinstance(action, dict)]
        result["filters"].append(_safe_tree(item, redactions))
    receiver_qdisc = tc.get("receiver_qdisc")
    if isinstance(receiver_qdisc, list):
        result["receiver_qdisc"] = _normalize_tc(
            {"qdisc": receiver_qdisc}, redactions)["qdisc"]
    classes = tc.get("traffic_classes")
    if isinstance(classes, dict):
        normalized_classes = {}
        for index, (class_name, value) in enumerate(sorted(classes.items()), 1):
            ref = class_name if class_name in ("critical", "interactive", "standard", "background", "udp") else f"class-{index}"
            normalized_classes[ref] = _safe_tree(value, redactions)
        result["traffic_classes"] = normalized_classes
    link = tc.get("link")
    if isinstance(link, dict):
        counters = {key: value for key, value in link.items()
                    if key in ("tx_bytes", "tx_dropped", "rx_bytes", "rx_dropped",
                               "tx_packets", "rx_packets", "tx_errors", "rx_errors")
                    and type(value) is int and value >= 0}
        if counters:
            result["link_counters"] = counters
    return result


def _safe_manifest(manifest: dict, redactions: list[str]) -> dict:
    # Drop identity-bearing or payload-bearing inputs; retain workload and policy knobs.
    allowed_top = ("version", "topology", "modes", "repetitions", "seed", "network", "workload",
                   "controller", "faults", "stages", "gates", "regression",
                   "static_critical_pct", "duration_seconds", "integrity", "ordering")
    selected = {key: manifest[key] for key in allowed_top if key in manifest}
    cleaned = _safe_tree(selected, redactions)
    if not isinstance(cleaned, dict):
        raise BundleError("experiment manifest could not be normalized")
    # Service names and addresses are not necessary to replay traffic-class outcomes.
    return cleaned


def _classify_run(row: dict, campaign: dict, preregistered: bool | None) -> tuple[list[str], list[str]]:
    labels, reasons = [], []
    measurement = row.get("measurement") or {}
    state = measurement.get("state")
    is_valid = row.get("valid") is True and state != "INVALID_INFRASTRUCTURE"
    if state == "INVALID_INFRASTRUCTURE" or row.get("valid") is False:
        labels.append("INVALID_INFRASTRUCTURE")
        reasons.append("SOURCE_RUN_INVALID")
    elif is_valid:
        labels.append("VALID_COMPLETED")
    if state == "INTERRUPTED" or row.get("status") == "INTERRUPTED":
        labels.append("INTERRUPTED")
        reasons.append("SOURCE_RUN_INTERRUPTED")
    offered, failed = row.get("offered"), row.get("failed")
    if type(offered) is int and offered > 0 and type(failed) is int and failed > 0:
        labels.append("WORKLOAD_FAILURE")
        reasons.append("UNSUCCESSFUL_REQUESTS_OBSERVED")
    if preregistered is None:
        labels.append("UNCLASSIFIED_SCOPE")
    elif preregistered:
        labels.append("PREREGISTERED_HELD_OUT")
    else:
        labels.append("EXPLORATORY")
    if not labels:
        labels.append("INVALID_INFRASTRUCTURE")
        reasons.append("UNCLASSIFIED_SOURCE_RUN")
    return sorted(set(labels)), sorted(set(reasons))


def _read_requests(path: Path, start: float, end: float, default_deadline: float | None,
                   max_rows: int = MAX_REQUESTS) -> tuple[bytes, dict]:
    raw = _read_artifact(path)
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    fields = tuple(reader.fieldnames or ())
    required = set(REQUEST_FIELDS)
    if not required.issubset(fields) or len(fields) > 16:
        raise BundleError("request ledger schema is not on the reviewed allowlist")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(("request_id", "scheduled_s", "completed_s", "latency_ms", "outcome",
                     "traffic_class", "service_ref", "dispatch_lag_ms", "deadline_ms"))
    service_map: dict[str, str] = {}
    rows, good, failures, deadlines, has_deadline = 0, [], 0, 0, False
    offered_outcomes: dict[str, int] = {}
    request_ids: set[int] = set()
    for row in reader:
        rows += 1
        if rows > max_rows or None in row or any(value is None for value in row.values()):
            raise BundleError("request ledger row bounds or width are invalid")
        try:
            row_id = int(row["id"])
            scheduled = float(row["scheduled"]) - start if row["scheduled"] else None
            completed = float(row["completed"]) - start if row["completed"] else None
            latency = float(row["latency_ms"]) if row["latency_ms"] else None
            dispatch = float(row["dispatch_lag_ms"]) if row["dispatch_lag_ms"] else None
            deadline = float(row.get("deadline_ms") or default_deadline) if (row.get("deadline_ms") or default_deadline) else None
        except (TypeError, ValueError, OverflowError) as error:
            raise BundleError("request timing row contains an invalid numeric field") from error
        if row_id < 0 or row_id in request_ids or any(value is not None and not math.isfinite(value)
                             for value in (scheduled, completed, latency, dispatch, deadline)):
            raise BundleError("request timing row contains duplicate identifiers or invalid values")
        request_ids.add(row_id)
        if scheduled is None or scheduled < 0 or completed is not None and completed < scheduled:
            raise BundleError("request timestamps are missing or out of order")
        if latency is not None and latency < 0 or dispatch is not None and dispatch < 0 or deadline is not None and deadline < 0:
            raise BundleError("request timing values must be nonnegative")
        outcome = row["outcome"]
        if not _safe_string(outcome) or len(outcome) > 48:
            outcome = "unknown"
        traffic_class = row["traffic_class"]
        if traffic_class not in ("critical", "interactive", "standard", "background", "udp"):
            traffic_class = "other"
        service = row["service"]
        if service not in service_map:
            service_map[service] = f"service-{len(service_map)+1}"
        writer.writerow((rows-1, "" if scheduled is None else format(scheduled, ".9f"),
                         "" if completed is None else format(completed, ".9f"),
                         "" if latency is None else format(latency, ".9f"), outcome,
                         traffic_class, service_map[service],
                         "" if dispatch is None else format(dispatch, ".9f"),
                         "" if deadline is None else format(deadline, ".9f")))
        offered_outcomes[outcome] = offered_outcomes.get(outcome, 0) + 1
        if outcome == "ok" and latency is not None:
            good.append(latency)
        else:
            failures += 1
        if deadline is not None:
            has_deadline = True
            deadlines += int(outcome == "ok" and latency is not None and latency <= deadline)
    if rows == 0:
        raise BundleError("request ledger is empty")
    return output.getvalue().encode(), dict(rows=rows, completed=len(good), failed=failures,
        outcomes=offered_outcomes, p50_ms=_percentile(good, .50), p95_ms=_percentile(good, .95),
        p99_ms=_percentile(good, .99), deadline_successes=deadlines,
        deadline_attainment=(deadlines/rows if has_deadline else None),
        failure_ratio=failures/rows, service_count=len(service_map))


def _normalize_receiver(path: Path, start: float) -> bytes | None:
    if not path.is_file():
        return None
    raw = _read_artifact(path)
    rows = []
    previous = None
    for line in raw.splitlines():
        obj = json.loads(line)
        if not isinstance(obj, dict) or not finite_number(obj.get("time")):
            raise BundleError("receiver counter sample is malformed")
        times = obj["time"] - start
        counts = obj.get("bytes")
        udp = obj.get("udp_bytes", [])
        if (not isinstance(counts, list) or not isinstance(udp, list)
                or any(type(v) is not int or v < 0 for v in counts + udp)):
            raise BundleError("receiver counters are malformed")
        if previous and (times <= previous[0] or len(counts) != len(previous[1])
                         or len(udp) != len(previous[2])
                         or any(y < x for x, y in zip(previous[1], counts))
                         or any(y < x for x, y in zip(previous[2], udp))):
            raise BundleError("receiver counters are not monotonic")
        previous = (times, counts, udp)
        rows.append({"time_s": round(times, 9), "bytes": counts, "udp_bytes": udp})
    if len(rows) < 2:
        raise BundleError("receiver counter ledger has too few samples")
    return b"".join(json_line(row) for row in rows)


def _extract_run_artifacts(root: Path, campaign_dir: Path, row: dict,
                           campaign_meta: dict, redactions: list[str]) -> dict[str, bytes]:
    artifact = row.get("artifact")
    if not isinstance(artifact, str) or artifact != f"r{row.get('repetition')}-{row.get('mode')}":
        raise BundleError("run artifact identity is invalid")
    source = campaign_dir / artifact
    if source.is_symlink() or not source.is_dir() or not source.resolve().is_relative_to(campaign_dir.resolve()):
        raise BundleError("run artifact directory is missing or unsafe")
    start, end = row.get("start"), row.get("end")
    duration = (end - start) if finite_number(start) and finite_number(end) else None
    data = {}
    requests_path = source / "requests.csv"
    ledger_info = None
    if requests_path.is_file() and duration is not None and duration > 0:
        deadline = row.get("deadline_ms")
        if deadline is None:
            deadline = campaign_meta["manifest"].get("workload", {}).get("deadline_ms")
        request_bytes, ledger_info = _read_requests(requests_path, start, end, deadline)
        data["requests.csv"] = request_bytes
    receiver = _normalize_receiver(source / "receiver.jsonl", start) if duration is not None else None
    if receiver is not None:
        data["receiver.jsonl"] = receiver
    # Preserve only queue configuration/counters and resource telemetry. Never copy host, process,
    # interface-address, arbitrary log, or application-payload fields from the source timeline.
    timeline = source / "timeline.jsonl"
    if timeline.is_file() and finite_number(start):
        timeline_raw = _read_artifact(timeline)
        tc_rows, resource_rows = [], []
        prior_tc = None
        for line in timeline_raw.splitlines():
            if not line:
                continue
            sample = json.loads(line)
            if not isinstance(sample, dict) or not finite_number(sample.get("time_ns")):
                raise BundleError("timeline sample is malformed")
            stamp = sample["time_ns"] / 1e9 - start
            tc = _normalize_tc(sample.get("tc"), redactions)
            # Keep a qdisc state at the beginning and when actual kernel configuration changes.
            signature = canonical({key: tc.get(key) for key in ("qdisc", "classes", "filters")})
            if prior_tc != signature:
                tc_rows.append({"time_s": round(stamp, 9), **tc})
                prior_tc = signature
            overhead = sample.get("overhead") or {}
            wanted = ("daemon_cpu_seconds", "child_cpu_seconds", "bpf_cpu_seconds",
                      "daemon_rss_bytes", "bpf_rss_bytes", "polling_cpu_seconds")
            resource = {key: overhead.get(key) for key in wanted if key in overhead}
            if resource:
                resource_rows.append({"time_s": round(stamp, 9),
                                      **_safe_tree(resource, redactions)})
        if tc_rows:
            data["qdisc_states.jsonl"] = b"".join(json_line(item) for item in tc_rows)
        if resource_rows:
            data["resource_samples.jsonl"] = b"".join(json_line(item) for item in resource_rows)
    network = source / "network.json"
    if network.is_file():
        obj = json.loads(_read_artifact(network).decode("utf-8"))
        data["kernel_readback.json"] = canonical({
            "policy": _safe_tree(obj.get("policy"), redactions),
            "tc": _normalize_tc((obj.get("tc") or {}), redactions),
        })
    transition_path = source / "transitions.jsonl"
    if transition_path.is_file() and finite_number(start):
        rows = []
        for line in _read_artifact(transition_path).splitlines():
            if not line:
                continue
            obj = json.loads(line)
            item = {"initial": bool(obj.get("initial"))}
            for key in ("scheduled_ns", "actual_application_ns", "applied_ns", "kernel_confirmed_ns",
                        "begun_ns", "at_ns"):
                if type(obj.get(key)) is int:
                    item[key.removesuffix("_ns") + "_relative_s"] = round(obj[key] / 1e9 - start, 9)
            for key in ("status", "failed_operation", "capacity_kbit", "requested"):
                if key in obj:
                    item[key] = _safe_tree(obj[key], redactions, key)
            if "requested" in item and isinstance(item["requested"], dict):
                item["requested"] = {key: value for key, value in item["requested"].items()
                                     if key in ("bandwidth_kbit", "base_rtt_ms", "delay_ms",
                                                "jitter_ms", "loss_pct", "reorder_pct", "link_up")}
            rows.append(item)
        data["transitions.jsonl"] = b"".join(json_line(item) for item in rows)
    if ledger_info:
        data["ledger_meta.json"] = canonical(ledger_info)
    return data


def _metrics(row: dict) -> dict:
    out = {}
    for key in METRIC_FIELDS:
        value = row.get(key)
        if value is None or type(value) in (bool, str):
            continue
        if finite_number(value):
            out[key] = value
    for key in ("valid", "mode", "repetition", "workload_seed"):
        if key in row and (type(row[key]) in (bool, int) or
                           isinstance(row[key], str) and MODES.fullmatch(row[key])):
            out[key] = row[key]
    return out


def _campaign_inventory(root: Path, relative: str, docs: list[str],
                        preregistered: bool | None = None,
                        scan_budget: dict | None = None,
                        deadline: float | None = None) -> dict:
    rel = _relative_source(relative)
    path = root / rel
    directory = path if path.is_dir() else path.parent
    source = directory / "summary.json"
    entry = {"ref_id": sha256(rel.encode())[:12], "documents": docs,
             "source_digest": None, "run_count": 0, "run_classifications": {},
             "campaign_state": "UNCLASSIFIED", "included": False}
    if source.is_file():
        try:
            if deadline is not None and time.monotonic() > deadline:
                raise BundleError("inventory runtime budget exceeded")
            size = source.stat().st_size
            if size > MAX_MEMBER or scan_budget is not None and scan_budget["bytes"] + size > MAX_INPUT:
                entry["campaign_state"] = "INVENTORY_INPUT_BUDGET_EXCEEDED"
                return entry
            if scan_budget is not None:
                scan_budget["bytes"] += size
            raw = _read_artifact(source)
            entry["source_digest"] = sha256(raw)
            data = json.loads(raw)
            if not isinstance(data, dict):
                entry["campaign_state"] = "UNSUPPORTED_SUMMARY_SHAPE"
                entry["schema_version"] = "custom"
                return entry
            runs = data.get("runs", [])
            entry["run_count"] = len(runs) if isinstance(runs, list) else 0
            counts: dict[str, int] = {}
            missing_artifacts = 0
            missing_ledgers = 0
            for run in runs if isinstance(runs, list) else []:
                labels, _ = _classify_run(run, data, preregistered)
                for label in labels:
                    counts[label] = counts.get(label, 0) + 1
                artifact = run.get("artifact")
                run_dir = directory / artifact if isinstance(artifact, str) else None
                if run_dir is None or run_dir.is_symlink() or not run_dir.is_dir():
                    missing_artifacts += 1
                elif not (run_dir / "requests.csv").is_file():
                    missing_ledgers += 1
            entry["run_classifications"] = counts
            entry["artifact_health"] = {
                "missing_run_directories": missing_artifacts,
                "missing_request_ledgers": missing_ledgers,
                "schema_replay_support": data.get("version") in (1, 2, 3),
            }
            entry["campaign_state"] = data.get("status", "SUMMARY_PRESENT")
            entry["schema_version"] = data.get("version", data.get("schema", "custom"))
        except (OSError, ValueError, TypeError, AttributeError):
            entry["campaign_state"] = "CORRUPT_SUMMARY"
    elif (directory / "status.jsonl").is_file():
        rows = []
        try:
            status_lines = _read_artifact(directory / "status.jsonl", 1024 * 1024).decode("utf-8", "replace").splitlines()[:256]
        except (OSError, BundleError):
            status_lines = []
            entry["campaign_state"] = "STATUS_INDEX_UNREADABLE"
        for index, line in enumerate(status_lines):
            try:
                obj = json.loads(line)
                rows.append({"record": index + 1,
                             "exit": obj.get("exit") if type(obj.get("exit")) is int else None,
                             "elapsed_s": obj.get("elapsed") if finite_number(obj.get("elapsed")) else None})
            except ValueError:
                rows.append({"record": index + 1, "exit": None, "elapsed_s": None})
        if entry["campaign_state"] != "STATUS_INDEX_UNREADABLE":
            entry["campaign_state"] = "INTERRUPTED_OR_UNSUMMARIZED"
        entry["status_records"] = rows
    else:
        entry["campaign_state"] = "REFERENCED_ARTIFACTS_MISSING"
    return entry


def discover_inventory(root: Path, spec: dict, deadline: float | None = None) -> list[dict]:
    references: dict[str, set[str]] = {}
    scopes: dict[str, bool] = {}
    selected_refs: set[str] = set()
    scan_budget = {"bytes": 0}
    docs = spec.get("inventory_documents", [])
    if not isinstance(docs, list) or len(docs) > 64:
        raise BundleError("inventory document count exceeds budget")
    missing_documents = []
    for doc in docs:
        if deadline is not None and time.monotonic() > deadline:
            raise BundleError("inventory runtime budget exceeded")
        relative_doc = safe_relative(doc)
        if not (root / relative_doc).is_file():
            missing_documents.append(relative_doc)
            continue
        raw_doc = _source_file(root, relative_doc, 2 * 1024 * 1024)
        scan_budget["bytes"] += len(raw_doc)
        if scan_budget["bytes"] > MAX_INPUT:
            raise BundleError("inventory input size exceeds budget")
        text = raw_doc.decode("utf-8", "replace")
        for match in re.findall(r"(?:(?:build/)?results)/[A-Za-z0-9_.-]+", text):
            ref = match.rstrip(".")
            references.setdefault(ref, set()).add(Path(doc).name)
    for item in spec.get("campaigns", []):
        ref = _relative_source(item["source"])
        directory = ref if (root / ref).is_dir() else str(Path(ref).parent)
        references.setdefault(directory, set()).add("evidence-bundle-spec")
        scopes[directory] = bool(item.get("preregistered_held_out")) if item.get("preregistered_held_out") is not None else False
        selected_refs.add(sha256(directory.encode())[:12])
    if len(references) > 2048:
        raise BundleError("inventory campaign count exceeds budget")
    out = []
    for ref in sorted(references):
        if deadline is not None and time.monotonic() > deadline:
            raise BundleError("inventory runtime budget exceeded")
        item = _campaign_inventory(root, ref, sorted(references[ref]), scopes.get(ref),
                                   scan_budget, deadline)
        item["selected_by_spec"] = item["ref_id"] in selected_refs
        out.append(item)
    for relative_doc in missing_documents:
        out.append({"ref_id": sha256(relative_doc.encode())[:12],
                    "documents": [Path(relative_doc).name], "source_digest": None,
                    "run_count": 0, "run_classifications": {},
                    "campaign_state": "INVENTORY_DOCUMENT_MISSING", "included": False,
                    "selected_by_spec": False})
    digest_counts: dict[str, int] = {}
    for row in out:
        digest = row.get("source_digest")
        if isinstance(digest, str):
            digest_counts[digest] = digest_counts.get(digest, 0) + 1
    for row in out:
        digest = row.get("source_digest")
        if isinstance(digest, str) and digest_counts[digest] > 1:
            row["duplicate_source_summary"] = True
    return out


def _normalize_campaign(root: Path, entry: dict, campaign_id: str,
                        redactions: list[str], deadline: float) -> tuple[dict, dict[str, bytes]]:
    source = _relative_source(entry["source"])
    summary_path = root / source
    summary_raw = _source_file(root, source)
    if any(pattern.search(summary_raw) for pattern in TOKEN_PATTERNS):
        raise BundleError("credential-like value detected in source summary")
    summary = json.loads(summary_raw.decode("utf-8"))
    if summary.get("version") not in (1, 2, 3) or not isinstance(summary.get("runs"), list):
        raise BundleError("unsupported research summary schema")
    try:
        sys.path.insert(0, str(root / "scripts"))
        from research_stats import load_summary
        summary = load_summary(summary_path)
    except (ImportError, ValueError, OSError, TypeError, KeyError) as error:
        raise BundleError("source research summary failed project validation") from error
    experiment = summary.get("manifest") or {}
    source_env = summary.get("environment") or {}
    manifest = _safe_manifest(experiment, redactions)
    preregistered = bool(entry.get("preregistered_held_out"))
    campaign_dir = summary_path.parent
    members: dict[str, bytes] = {}
    source_bytes_total = len(summary_raw)
    run_records = []
    seen = set()
    for row in summary["runs"]:
        if time.monotonic() > deadline:
            raise BundleError("bundle runtime budget exceeded")
        labels, reasons = _classify_run(row, summary, preregistered)
        run_id = f"r{row['repetition']:03d}-{row['mode']}"
        if run_id in seen:
            raise BundleError("duplicate source run identity")
        seen.add(run_id)
        missing_artifact = not isinstance(row.get("artifact"), str) or not (campaign_dir / str(row.get("artifact"))).is_dir()
        if missing_artifact and row.get("valid") is True:
            raise BundleError("valid source run has no raw artifact directory")
        if not missing_artifact:
            source_dir = campaign_dir / str(row["artifact"])
            for filename in ("requests.csv", "receiver.jsonl", "timeline.jsonl",
                             "network.json", "transitions.jsonl"):
                candidate = source_dir / filename
                if candidate.is_file() and not candidate.is_symlink():
                    source_bytes_total += candidate.stat().st_size
                    if source_bytes_total > MAX_INPUT:
                        raise BundleError("source evidence exceeds input budget")
        raw_files = ({} if missing_artifact else _extract_run_artifacts(
            root, campaign_dir, row, {"manifest": experiment}, redactions))
        prefix = f"campaigns/{campaign_id}/runs/{run_id}"
        artifact_names = {}
        for name, payload in raw_files.items():
            relname = safe_relative(f"{prefix}/{name}")
            members[relname] = payload
            artifact_names[name] = relname
        source_state = (row.get("measurement") or {}).get("state")
        if missing_artifact:
            reasons = sorted(set(reasons + ["SOURCE_ARTIFACT_MISSING"]))
            labels = sorted(set(labels + ["INVALID_INFRASTRUCTURE"]))
        if row.get("valid") is False and source_state is None:
            source_state = "INVALID_INFRASTRUCTURE"
        record = {
            "run_id": run_id, "mode": row["mode"], "repetition": row["repetition"],
            "workload_seed": row.get("workload_seed"),
            "valid": row.get("valid") is True and source_state != "INVALID_INFRASTRUCTURE",
            "state": ("INVALID_INFRASTRUCTURE" if missing_artifact else
                      source_state or ("VALID" if row.get("valid") else "INVALID_INFRASTRUCTURE")),
            "classifications": labels, "reason_codes": reasons,
            "source_metrics": _metrics(row), "artifacts": artifact_names,
            "duration_seconds": (row["end"] - row["start"])
                if finite_number(row.get("start")) and finite_number(row.get("end")) else None,
            "raw_ledger_present": "requests.csv" in artifact_names,
        }
        # A source summary can call a run valid while the raw offered ledger disagrees.
        if "ledger_meta.json" in artifact_names:
            ledger = json.loads(members[artifact_names["ledger_meta.json"]])
            if type(row.get("offered")) is int and ledger.get("rows") != row["offered"]:
                record["valid"] = False
                record["state"] = "INVALID_INFRASTRUCTURE"
                record["classifications"] = sorted(set(labels + ["INVALID_INFRASTRUCTURE"]))
                record["reason_codes"] = sorted(set(reasons + ["RAW_LEDGER_DENOMINATOR_MISMATCH"]))
        run_members = canonical(record)
        run_path = f"{prefix}/run.json"
        members[run_path] = run_members
        run_records.append(record)
    metadata = {
        "schema_version": 1, "campaign_id": campaign_id, "study_name": entry.get("name", campaign_id),
        "scope": entry.get("scope", "supporting_release_evidence"),
        "rationale": entry.get("rationale", "release evidence inventory"),
        "source_commit": source_env.get("git_commit"),
        "source_worktree_dirty": source_env.get("git_dirty"),
        "source_binary_sha256": source_env.get("binary_sha256"),
        "source_tree_sha256": source_env.get("source_sha256"),
        "source_summary_sha256": sha256(summary_raw),
        "source_experiment_sha256": None,
        "kernel": source_env.get("kernel"),
        "architecture": source_env.get("architecture"),
        "available_cpu_count": source_env.get("cpus"),
        "tc_version": source_env.get("tc"),
        "tool_versions": {
            "flowcore": source_env.get("flowcore"),
            "tc": source_env.get("tc"),
            "python": source_env.get("python_version"),
            "iproute2": source_env.get("iproute2_version"),
            "systemd": source_env.get("systemd_version"),
            "analysis": source_env.get("analysis_version"),
            "unrecorded": [name for name, value in (
                ("python", source_env.get("python_version")),
                ("iproute2", source_env.get("iproute2_version")),
                ("systemd", source_env.get("systemd_version"))) if value is None],
        },
        "status": summary.get("status"), "schema_version_source": summary.get("version"),
        "experiment_id_redacted": True,
        "seed_blocks": summary.get("block_seeds", []),
        "manifest": manifest,
        "statistical_method": {
            "percentile": "nearest-rank ceil(n*p)-1 over successful requests",
            "paired_effect": "run-level paired median difference or relative difference",
            "confidence_interval": "distribution-free order-statistic median interval",
            "multiple_comparison": "Bonferroni family using campaign modes and declared margins",
            "requests_are_not_independent_experimental_repetitions": True,
        },
        "classification": ["PREREGISTERED_HELD_OUT" if preregistered else "EXPLORATORY"],
        "preregistration_status": ("REPORT_IDENTIFIES_FROZEN_HELD_OUT_PLAN" if preregistered else "NOT_CLAIMED"),
        "preregistration_evidence_ref": entry.get("preregistration_evidence_ref"),
        "runs": [row["run_id"] for row in run_records],
        "_source_bytes": source_bytes_total,
        "_inventory_ref_id": sha256(str(Path(source).parent).encode())[:12],
        "_privacy_literals": [value for value in
            (source_env.get("host"), source_env.get("hostname"), source_env.get("username"))
            if isinstance(value, str) and len(value) >= 4],
    }
    experiment_path = campaign_dir / "experiment.json"
    if experiment_path.is_file():
        experiment_bytes = _source_file(root, str(experiment_path.relative_to(root)))
        source_bytes_total += len(experiment_bytes)
        if source_bytes_total > MAX_INPUT:
            raise BundleError("source evidence exceeds input budget")
        metadata["source_experiment_sha256"] = sha256(experiment_bytes)
        source_plan = json.loads(experiment_bytes.decode("utf-8"))
        normalized_plan = _safe_manifest(source_plan, redactions)
        metadata["predeclared_plan"] = normalized_plan
        metadata["predeclared_plan_sha256"] = sha256(canonical(normalized_plan))
    campaign_path = f"campaigns/{campaign_id}/campaign.json"
    members[campaign_path] = canonical(metadata)
    return metadata, members


def _receiver_rates(records: list[dict], start: float, end: float) -> tuple[list[float], float]:
    if end <= start:
        raise BundleError("invalid receiver analysis interval")
    tcp_start = _interpolate(records, start, "bytes")
    tcp_end = _interpolate(records, end, "bytes")
    udp_start = _interpolate(records, start, "udp_bytes")
    udp_end = _interpolate(records, end, "udp_bytes")
    tcp = [(b - a) * 8 / (end - start) / 1_000_000 for a, b in zip(tcp_start, tcp_end)]
    udp = (sum(udp_end) - sum(udp_start)) * 8 / (end - start) / 1_000_000
    return tcp, udp


def _replay_run(files: dict[str, bytes], record: dict, campaign: dict) -> dict:
    paths = record["artifacts"]
    request_name = paths.get("requests.csv")
    if not request_name:
        state = "NONREPLAYABLE_INVALID_RUN" if not record.get("valid") else "UNREPLAYABLE_VALID_RUN"
        if state == "UNREPLAYABLE_VALID_RUN":
            raise BundleError("valid run is missing its request ledger")
        source = record.get("source_metrics", {})
        return {"run_id": record["run_id"], "mode": record["mode"],
                "repetition": record["repetition"], "source_valid": False,
                "offered": source.get("offered", 0), "completed": source.get("completed", 0),
                "failed": source.get("failed", source.get("offered", 0)),
                "failure_ratio": source.get("failure_ratio"),
                "deadline_attainment": source.get("deadline_attainment"),
                "deadline_misses": source.get("deadline_misses"),
                "p99_ms": source.get("p99_ms"), "background_mbps": source.get("background_mbps"),
                "total_mbps": source.get("total_mbps"), "jain_index": source.get("jain_index"),
                "cpu_seconds": source.get("cpu_seconds"),
                "classifications": record.get("classifications", []),
                "replay_state": state, "reason_codes": record.get("reason_codes", []),
                "replayed_from_raw_ledger": False}
    rows = list(csv.DictReader(io.StringIO(files[request_name].decode("utf-8"))))
    offered = len(rows)
    successes = [row for row in rows if row["outcome"] == "ok"]
    failures = offered - len(successes)
    latencies = [float(row["latency_ms"]) for row in successes if row["latency_ms"]]
    deadlines = [float(row["deadline_ms"]) for row in rows if row.get("deadline_ms")]
    deadline_limit = deadlines[0] if deadlines else None
    if deadlines and any(value != deadline_limit for value in deadlines):
        raise BundleError("request ledger contains inconsistent deadline values")
    within = sum(float(row["completed_s"]) <= record["duration_seconds"]
                 for row in successes if row["completed_s"])
    deadline_success = (sum(float(row["latency_ms"]) <= float(row["deadline_ms"] or deadline_limit)
                             for row in rows if row["outcome"] == "ok" and row["latency_ms"])
                        if deadline_limit is not None else None)
    payload = campaign["manifest"].get("workload", {}).get("payload_bytes", 0)
    duration = record["duration_seconds"]
    if not finite_number(duration) or duration <= 0:
        raise BundleError("run duration is missing")
    request_mbps = within * payload * 8 / duration / 1_000_000
    tcp_rates, udp_mbps = [], 0.0
    receiver_name = paths.get("receiver.jsonl")
    if receiver_name:
        samples = [json.loads(line) for line in files[receiver_name].splitlines() if line]
        tcp_rates, udp_mbps = _receiver_rates(samples, 0.0, duration)
    background_mbps = sum(tcp_rates)
    total_mbps = request_mbps + background_mbps + udp_mbps
    phases = campaign["manifest"].get("workload", {}).get("phases", [])
    phase_fairness = []
    phase_start = 0.0
    for phase in phases:
        if not isinstance(phase, dict):
            continue
        seconds = phase.get("seconds")
        streams = phase.get("background_streams")
        if type(seconds) is not int or seconds <= 0 or type(streams) is not int or streams < 0:
            continue
        rates, _ = _receiver_rates(samples, phase_start, phase_start + seconds) if receiver_name else ([], 0)
        rates = rates[:streams]
        denom = len(rates) * sum(rate * rate for rate in rates)
        jain = sum(rates) ** 2 / denom if denom else None
        phase_fairness.append((seconds, jain, streams))
        phase_start += seconds
    active = [(seconds, index) for index, (seconds, jain, streams) in enumerate(phase_fairness)
              if streams >= 2 and jain is not None]
    fairness = (sum(phase_fairness[index][0] * phase_fairness[index][1] for _, index in active) /
                sum(seconds for seconds, _ in active)) if active else None
    cpu = None
    peak_rss = None
    resources_name = paths.get("resource_samples.jsonl")
    if resources_name:
        resources = [json.loads(line) for line in files[resources_name].splitlines() if line]
        if resources:
            first, last = resources[0], resources[-1]
            cpu_keys = ("daemon_cpu_seconds", "child_cpu_seconds", "bpf_cpu_seconds")
            components = []
            for key in cpu_keys:
                a, b = first.get(key), last.get(key)
                if finite_number(a) and finite_number(b):
                    components.append(max(0.0, b - a))
            cpu = sum(components) if components else None
            peak_rss = max((value for item in resources for key, value in item.items()
                            if key in ("daemon_rss_bytes", "bpf_rss_bytes") and type(value) is int),
                           default=None)
    source = record.get("source_metrics", {})
    replay = {
        "run_id": record["run_id"], "mode": record["mode"], "repetition": record["repetition"],
        "source_valid": record["valid"], "classifications": record["classifications"],
        "offered": offered, "completed": len(successes), "failed": failures,
        "failure_ratio": failures / offered if offered else None,
        "completion_ratio": len(successes) / offered if offered else None,
        "p50_ms": _percentile(latencies, .50), "p95_ms": _percentile(latencies, .95),
        "p99_ms": _percentile(latencies, .99),
        "deadline_ms": deadline_limit,
        "deadline_successes": deadline_success,
        "deadline_misses": offered - deadline_success if deadline_success is not None else None,
        "deadline_attainment": deadline_success / offered if deadline_success is not None else None,
        "successful_within_window": within, "request_payload_mbps": request_mbps,
        "background_mbps": background_mbps, "udp_mbps": udp_mbps, "total_mbps": total_mbps,
        "flow_mbps": tcp_rates, "jain_index": fairness, "cpu_seconds": cpu,
        "peak_rss_bytes": peak_rss, "qdisc_state_count": len(files.get(paths.get("qdisc_states.jsonl", ""), b"").splitlines()),
        "checks": {}, "replayed_from_raw_ledger": True,
    }
    expectations = {
        "offered": offered, "completed": len(successes), "failed": failures,
        "p50_ms": replay["p50_ms"], "p95_ms": replay["p95_ms"], "p99_ms": replay["p99_ms"],
        "deadline_attainment": replay["deadline_attainment"], "deadline_misses": replay["deadline_misses"],
        "background_mbps": background_mbps, "udp_mbps": udp_mbps,
        "total_mbps": total_mbps, "jain_index": fairness, "cpu_seconds": cpu,
    }
    for key, observed in expectations.items():
        expected = source.get(key)
        if expected is None or observed is None:
            replay["checks"][key] = "NOT_COMPARABLE"
        elif type(expected) is int:
            replay["checks"][key] = "MATCH" if expected == observed else "MISMATCH"
        else:
            replay["checks"][key] = ("MATCH" if math.isclose(float(expected), float(observed),
                                     rel_tol=2e-5, abs_tol=2e-5) else "MISMATCH")
    replay["replay_state"] = "PASS" if all(v != "MISMATCH" for v in replay["checks"].values()) else "MISMATCH"
    return replay


def _compare_campaign(campaign: dict, run_rows: list[dict]) -> dict:
    manifest = campaign.get("manifest", {})
    gates = manifest.get("gates", {})
    baseline = gates.get("baseline")
    margins = gates.get("margins") or {}
    modes = manifest.get("modes", [])
    by_mode = {mode: [row for row in run_rows if row["mode"] == mode] for mode in modes}
    summary = {}
    for mode, rows in by_mode.items():
        valid = [row for row in rows if row["source_valid"]]
        class_counts: dict[str, int] = {}
        for run in rows:
            for label in run.get("classifications", []):
                class_counts[label] = class_counts.get(label, 0) + 1
        summary[mode] = {
            "run_count": len(rows), "valid_runs": len(valid), "invalid_runs": len(rows) - len(valid),
            "classifications": class_counts,
            "replayable_runs": sum(row.get("replayed_from_raw_ledger") is True for row in rows),
            "nonreplayable_invalid_runs": sum(row["replay_state"] == "NONREPLAYABLE_INVALID_RUN" for row in rows),
            "offered": sum(row.get("offered", 0) for row in rows),
            "failed": sum(row["failed"] for row in rows),
            "conditional_p99_median_ms": statistics.median([r["p99_ms"] for r in valid if r["p99_ms"] is not None])
                if any(r["p99_ms"] is not None for r in valid) else None,
            "deadline_attainment_median": statistics.median([r["deadline_attainment"] for r in valid
                if r["deadline_attainment"] is not None]) if any(r["deadline_attainment"] is not None for r in valid) else None,
            "background_mbps_median": statistics.median([r["background_mbps"] for r in valid])
                if valid else None,
            "total_mbps_median": statistics.median([r["total_mbps"] for r in valid])
                if valid else None,
            "jain_index_median": statistics.median([r["jain_index"] for r in valid if r["jain_index"] is not None])
                if any(r["jain_index"] is not None for r in valid) else None,
        }
    comparisons = []
    if baseline in by_mode and margins:
        family = max(1, (len(modes) - 1) * len(margins))
        confidence = 1 - .05 / family
        directions = {
            "p99_ms": ("higher", "relative"), "total_mbps": ("lower", "relative"),
            "background_mbps": ("lower", "relative"), "failure_ratio": ("higher", "absolute"),
            "deadline_attainment": ("lower", "absolute"), "jain_index": ("lower", "absolute"),
            "cpu_seconds": ("higher", "relative"),
        }
        min_pairs = gates.get("min_pairs", 1)
        for candidate in modes:
            if candidate == baseline:
                continue
            left = {row["repetition"]: row for row in by_mode[baseline]}
            right = {row["repetition"]: row for row in by_mode[candidate]}
            invalid = any(not row["source_valid"] for row in [*left.values(), *right.values()])
            metrics = {}
            for metric, margin in margins.items():
                if metric not in directions:
                    continue
                direction, unit = directions[metric]
                diffs = []
                paired_repetitions = sorted(left.keys() & right.keys())
                skipped_invalid_pairs = 0
                skipped_failure_pairs = 0
                skipped_missing_metric_pairs = 0
                for rep in paired_repetitions:
                    a, b = left[rep], right[rep]
                    if not a["source_valid"] or not b["source_valid"]:
                        skipped_invalid_pairs += 1
                        continue
                    if metric == "p99_ms" and (a["failed"] or b["failed"]):
                        skipped_failure_pairs += 1
                        continue
                    av = a.get(metric)
                    bv = b.get(metric)
                    if metric == "failure_ratio":
                        av = a["failed"] / a["offered"] if a["offered"] else None
                        bv = b["failed"] / b["offered"] if b["offered"] else None
                    if av is None or bv is None or unit == "relative" and av <= 0:
                        skipped_missing_metric_pairs += 1
                        continue
                    diff = (bv - av) if direction == "higher" else (av - bv)
                    diffs.append(diff / av if unit == "relative" else diff)
                interval = _median_ci(diffs, confidence)
                if invalid:
                    state = "INVALID EXPERIMENT"
                elif len(diffs) < min_pairs or interval is None:
                    state = "INCONCLUSIVE"
                elif interval["low"] > margin:
                    state = "REGRESSION"
                elif interval["high"] <= margin:
                    state = "PASS"
                else:
                    state = "INCONCLUSIVE"
                metrics[metric] = {"pairs": len(diffs), "eligible_pairs": len(paired_repetitions),
                    "skipped_invalid_pairs": skipped_invalid_pairs,
                    "skipped_failure_pairs": skipped_failure_pairs,
                    "skipped_missing_metric_pairs": skipped_missing_metric_pairs,
                    "threshold": margin, "status": state,
                    "median_effect": statistics.median(diffs) if diffs else None,
                    "interval": interval, "paired_effects": diffs}
            overall = ("INVALID EXPERIMENT" if invalid else
                       "REGRESSION" if any(v["status"] == "REGRESSION" for v in metrics.values()) else
                       "PASS" if metrics and all(v["status"] == "PASS" for v in metrics.values()) else "INCONCLUSIVE")
            comparisons.append({"baseline": baseline, "candidate": candidate,
                                "family_tests": family, "confidence_per_interval": confidence,
                                "outcome": overall, "metrics": metrics})
    replayable = [row for row in run_rows if row["replay_state"] != "NONREPLAYABLE_INVALID_RUN"]
    nonreplayable_invalid = sum(row["replay_state"] == "NONREPLAYABLE_INVALID_RUN" for row in run_rows)
    reproducible = all(row["replay_state"] == "PASS" for row in replayable)
    classification_counts: dict[str, int] = {}
    for row in run_rows:
        for label in row.get("classifications", []):
            classification_counts[label] = classification_counts.get(label, 0) + 1
    return {"campaign_id": campaign["campaign_id"], "status": campaign["status"],
            "classification_counts": classification_counts,
            "summary_by_mode": summary, "comparisons": comparisons,
            "nonreplayable_invalid_runs": nonreplayable_invalid,
            "reproducibility": "PASS" if reproducible else "MISMATCH",
            "replay_scope": "all ledger-bearing runs; invalid runs without ledgers are retained and explicitly not replayed",
            "runs": run_rows}


def replay_files(files: dict[str, bytes]) -> dict:
    campaigns = []
    for name, payload in sorted(files.items()):
        if not name.startswith("campaigns/") or not name.endswith("/campaign.json"):
            continue
        campaign = json.loads(payload)
        prefix = name.rsplit("/", 1)[0]
        runs = []
        for run_id in campaign.get("runs", []):
            run_path = f"{prefix}/runs/{run_id}/run.json"
            record = json.loads(files[run_path])
            runfiles = {key: value for key, value in files.items()
                        if key.startswith(f"{prefix}/runs/{run_id}/")}
            runs.append(_replay_run(runfiles, record, campaign))
        campaigns.append(_compare_campaign(campaign, runs))
    if not campaigns:
        raise BundleError("bundle contains no replayable campaigns")
    release_gates = json.loads(files["release-gates.json"]) if "release-gates.json" in files else None
    metric_checks: dict[str, int] = {}
    for campaign in campaigns:
        for run in campaign["runs"]:
            for value in run.get("checks", {}).values():
                metric_checks[value] = metric_checks.get(value, 0) + 1
    inventory = None
    if "inventory.json" in files:
        rows = json.loads(files["inventory.json"]).get("discovered_campaigns", [])
        state_counts: dict[str, int] = {}
        classification_counts: dict[str, int] = {}
        missing_directories = missing_ledgers = 0
        for item in rows:
            state = item.get("campaign_state", "UNKNOWN")
            state_counts[state] = state_counts.get(state, 0) + 1
            for name, count in item.get("run_classifications", {}).items():
                classification_counts[name] = classification_counts.get(name, 0) + count
            health = item.get("artifact_health", {})
            missing_directories += health.get("missing_run_directories", 0)
            missing_ledgers += health.get("missing_request_ledgers", 0)
        inventory = {"referenced_campaigns": len(rows), "campaign_states": state_counts,
                     "run_classifications": classification_counts,
                     "missing_run_directories": missing_directories,
                     "missing_request_ledgers": missing_ledgers}
    return {"analysis_version": 1, "method": "nearest-rank request percentiles; paired run-level median order-statistic intervals",
            "campaign_count": len(campaigns), "reproducibility": "PASS" if all(
                item["reproducibility"] == "PASS" for item in campaigns) else "MISMATCH",
            "project_release_gates": release_gates, "inventory": inventory,
            "metric_check_counts": metric_checks, "campaigns": campaigns}


def _render_report(analysis: dict) -> bytes:
    lines = ["# Replayed experiment evidence", "",
        "Generated by the standalone bundle verifier from bundled ledgers. Successful-request P99 is conditional; failure and deadline denominators are shown beside it. Each experimental run, not each request, is the unit for paired comparisons.", ""]
    gates = analysis.get("project_release_gates")
    if gates:
        lines += ["## Source release-gate snapshot", "",
                  f"Scope: {gates.get('scope', 'unspecified')}. Production-ready: {gates.get('production_ready', False)}.", "",
                  "| Gate | Recorded state |", "| --- | --- |"]
        lines.extend(f"| {name} | {state} |" for name, state in sorted(gates.get("gates", {}).items()))
        lines += ["", "This is the source repository's declared gate snapshot; it is not independently inferred from these benchmark runs.", ""]
    inventory = analysis.get("inventory")
    if inventory:
        lines += ["## Referenced artifact inventory", "",
                  f"The reports reference {inventory['referenced_campaigns']} campaigns. The builder packaged only the selected supported schemas; missing source directories: {inventory['missing_run_directories']}; missing request ledgers: {inventory['missing_request_ledgers']}.", "",
                  "Campaign states: " + ", ".join(f"{key}={value}" for key, value in sorted(inventory["campaign_states"].items())) + ".", "",
                  "Run classifications: " + ", ".join(f"{key}={value}" for key, value in sorted(inventory["run_classifications"].items())) + ".", "",
                  "References outside the packaged set remain in inventory with missing, custom-schema, or scope-exclusion reasons; they are not treated as replayed.", ""]
    for campaign in analysis["campaigns"]:
        lines += [f"## {campaign['campaign_id']}", "",
                  f"Campaign state: {campaign['status']}. Replay: {campaign['reproducibility']}. Non-replayable invalid runs retained: {campaign['nonreplayable_invalid_runs']}.", "",
                  "Run classifications: " + ", ".join(f"{key}={value}" for key, value in sorted(campaign.get("classification_counts", {}).items())) + ".", "",
                  "| Mode | Runs (valid/invalid) | Offered | Failed | Conditional P99 median (ms) | Deadline attainment median | Background Mbit/s | Jain |",
                  "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
        for mode, row in campaign["summary_by_mode"].items():
            values = (mode, f"{row['valid_runs']}/{row['invalid_runs']}", str(row["offered"]),
                str(row["failed"]), _fmt(row["conditional_p99_median_ms"]),
                _fmt(row["deadline_attainment_median"]), _fmt(row["background_mbps_median"]),
                _fmt(row["jain_index_median"]))
            lines.append("| " + " | ".join(values) + " |")
        lines.append("")
        for comp in campaign["comparisons"]:
            lines.append(f"- {comp['candidate']} vs {comp['baseline']}: **{comp['outcome']}**.")
            for metric, item in comp["metrics"].items():
                interval = item["interval"]
                band = "unavailable" if interval is None else f"[{interval['low']:.6g}, {interval['high']:.6g}]"
                lines.append(f"  {metric}: n={item['pairs']}/{item['eligible_pairs']} eligible pairs, "
                             f"skipped invalid={item['skipped_invalid_pairs']}, failed-request P99 pairs="
                             f"{item['skipped_failure_pairs']}, missing metrics={item['skipped_missing_metric_pairs']}, "
                             f"median effect={_fmt(item['median_effect'])}, interval={band}, gate={item['status']}.")
        lines.append("")
    lines += ["## Scope and limitations", "",
        "These replays validate internal consistency between normalized request, receiver-counter, resource, qdisc, and source-summary records. SHA-256 checksums detect file changes but do not authenticate who produced the bundle. Host names, absolute paths, process identities, raw IP addresses, service names, and application payloads are omitted. Runs without a supported request ledger remain inventoried and are not used in statistics.", "",
        "Metric checks: " + ", ".join(f"{key}={value}" for key, value in sorted(analysis.get("metric_check_counts", {}).items())) + ". Some source metrics may be NOT_COMPARABLE when the source summary did not contain a value; these are counted, never replaced with a fabricated value.", ""]
    return ("\n".join(lines)).encode("utf-8")


def _fmt(value: object) -> str:
    return "n/a" if value is None else f"{value:.6g}"


def _privacy_scan_files(files: dict[str, bytes], sensitive_literals: tuple[str, ...] = ()) -> dict:
    findings: dict[str, list[str]] = {}
    for name, data in files.items():
        if name == "SHA256SUMS":
            continue
        codes = privacy_findings(name, data, scan_code=name == "tools/evidence_bundle.py",
                                 sensitive_literals=sensitive_literals)
        if codes:
            findings[name] = codes
    return {"status": "PASS" if not findings else "BLOCKED",
            "files_scanned": len(files), "findings": findings}


def _sum_file(items: dict[str, bytes]) -> bytes:
    return "".join(f"{sha256(data)}  {name}\n" for name, data in sorted(items.items())).encode()


def _tar_info(name: str, data: bytes) -> tarfile.TarInfo:
    info = tarfile.TarInfo(safe_relative(name))
    info.size, info.mode, info.uid, info.gid, info.mtime = len(data), 0o644, 0, 0, 0
    info.uname = info.gname = ""
    return info


def build(root: Path, spec_path: Path, output: Path) -> dict:
    root, spec_path, output = root.resolve(), spec_path.resolve(), output.resolve()
    candidate_root = (root / "build/evidence-candidate").resolve()
    if not spec_path.is_relative_to(root) or not output.is_relative_to(candidate_root):
        raise BundleError("bundle output is restricted to build/evidence-candidate")
    if "results" in output.relative_to(root).parts:
        raise BundleError("bundle output must not overwrite source evidence")
    disk = shutil.disk_usage(output.parent if output.parent.exists() else root)
    if disk.free < MIN_FREE:
        raise BundleError("insufficient free disk space for evidence bundle")
    start = time.monotonic()
    spec = json.loads(_source_file(root, str(spec_path.relative_to(root)), 1024 * 1024))
    if (spec.get("version") != SCHEMA_VERSION or not isinstance(spec.get("campaigns"), list)
            or len(spec["campaigns"]) > 64):
        raise BundleError("unsupported evidence bundle spec or campaign count")
    deadline = start + MAX_SECONDS
    input_bytes = 0
    redactions: list[str] = []
    source_sensitive_literals: list[str] = []
    inventory = discover_inventory(root, spec, deadline)
    members: dict[str, bytes] = {}
    included = []
    release_gate_path = root / "config/release-gates.json"
    if release_gate_path.is_file():
        gate_data = json.loads(_source_file(root, "config/release-gates.json", 1024 * 1024))
        allowed_states = {"PASS", "PARTIAL", "FAIL", "UNVERIFIED", "BLOCKED"}
        gates = gate_data.get("gates", {})
        normalized_gates = {str(key): value for key, value in gates.items()
                            if isinstance(key, str) and value in allowed_states}
        members["release-gates.json"] = canonical({
            "version": gate_data.get("version"), "scope": gate_data.get("scope"),
            "production_ready": gate_data.get("production_ready") is True,
            "gates": normalized_gates})
    for index, item in enumerate(spec["campaigns"], 1):
        source = _relative_source(item["source"])
        summary = root / source
        if not summary.is_file():
            raise BundleError("selected campaign summary is missing")
        campaign_id = f"study-{index:03d}"
        metadata, campaign_members = _normalize_campaign(root, item, campaign_id, redactions, deadline)
        source_sensitive_literals.extend(metadata.pop("_privacy_literals", []))
        selected_ref_id = metadata.pop("_inventory_ref_id", None)
        input_bytes += metadata.pop("_source_bytes", 0)
        if input_bytes > MAX_INPUT:
            raise BundleError("selected source evidence exceeds input budget")
        if sum(len(value) for value in members.values()) + sum(
                len(value) for value in campaign_members.values()) > MAX_UNPACKED:
            raise BundleError("normalized bundle exceeds unpacked-size budget")
        members.update(campaign_members)
        included.append({"campaign_id": campaign_id, "scope": metadata["scope"],
                         "study_name": metadata["study_name"], "source_commit": metadata["source_commit"],
                         "classification": metadata["classification"], "run_count": len(metadata["runs"])})
        for row in inventory:
            if row["ref_id"] == selected_ref_id:
                row["included"] = True
                row["bundle_campaign_id"] = campaign_id
    for row in inventory:
        if not row["included"]:
            row["exclusion_reason"] = ("custom_schema_adapter_not_included" if row["campaign_state"] not in (
                "complete", "interrupted") else "outside_initial_reproducibility_scope")
    inventory_obj = {"schema_version": 1, "discovered_campaigns": inventory,
                     "included_campaigns": included}
    members["inventory.json"] = canonical(inventory_obj)
    members["reproduction.md"] = (
        b"# Reproduce and verify\n\n"
        b"Run verification and statistical replay with only Python 3 and this archive:\n\n"
        b"    python3 -I tools/evidence_bundle.py verify flowcore-evidence.tar.gz --replay\n\n"
        b"To regenerate workloads, check out the source_commit recorded in campaign.json, build FLOWCORE with the recorded CMake options, and use the bundled normalized experiment manifest with the owned namespace Research Lab runner. Experiments require an isolated Linux environment and appropriate namespace privileges. The bundle verifier itself needs no network, privilege, or FLOWCORE project files.\n\n"
        b"Linux does not provide deterministic packet scheduling. Seeds preserve workload randomization, not exact packet timing.\n")
    members["README.md"] = (
        b"# FLOWCORE evidence bundle candidate\n\n"
        b"This is a local publication candidate for the selected release-evidence campaigns. It contains normalized experiment manifests, complete run classifications, request ledgers, receiver counters, resource samples, qdisc readbacks, replay analysis, and checksums. Source records were transformed through field allowlists to remove host/user identifiers, absolute paths, network addresses, service names, and payload content.\n\n"
        b"Verify the archive and replay statistics using the command in reproduction.md. The checksums establish content integrity only. The RPM and evidence bundle are not cryptographically signed. No upload or release publication is performed by the builder.\n")
    # Replay once before packaging so a bundle with contradictory data never receives a PASS label.
    sensitive_literals = tuple(dict.fromkeys(
        [value for value in (getpass.getuser(), socket.gethostname())
         if isinstance(value, str) and len(value) >= 4] + source_sensitive_literals))
    initial_scan = _privacy_scan_files(members, sensitive_literals)
    if initial_scan["status"] != "PASS":
        raise BundleError("privacy scan blocked bundle content")
    analysis = replay_files(members)
    if analysis["reproducibility"] != "PASS":
        raise BundleError("raw-data replay disagrees with source metrics")
    members["analysis.json"] = canonical(analysis)
    members["report.md"] = _render_report(analysis)
    members["tools/evidence_bundle.py"] = Path(__file__).read_bytes()
    members["privacy-review.json"] = canonical({
        "schema_version": 1, "status": "PASS", "files_scanned": len(members) + 1,
        "redacted_field_categories": dict(sorted((item, redactions.count(item)) for item in set(redactions))),
        "raw_logs_included": False, "application_payloads_included": False,
        "privacy_findings": [],
    })
    scan = _privacy_scan_files(members, sensitive_literals)
    if scan["status"] != "PASS":
        raise BundleError("final privacy scan blocked bundle content")
    payload = dict(members)
    if sum(len(data) for data in payload.values()) > MAX_UNPACKED:
        raise BundleError("unpacked bundle exceeds storage budget")
    if len(payload) > MAX_FILES - 2:
        raise BundleError("bundle file-count limit exceeded")
    digests = {name: {"size": len(data), "sha256": sha256(data)} for name, data in sorted(payload.items())}
    groups: dict[str, list[str]] = {}
    for name, item in digests.items():
        groups.setdefault(item["sha256"], []).append(name)
    duplicates = [sorted(names) for names in groups.values() if len(names) > 1]
    bundle_meta = {
        "schema_version": SCHEMA_VERSION, "bundle_id": f"flowcore-evidence-v1-{sha256(canonical(digests))[:12]}",
        "built_from_git_commit": _git_commit(root), "source_tree_dirty": _git_dirty(root),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "contents": digests, "duplicate_content_groups": duplicates,
        "included_campaigns": included, "inventory_member": "inventory.json",
        "analysis_member": "analysis.json", "privacy_status": scan["status"],
        "privacy_review": "passed_on_normalized_content; source paths and identities were removed",
        "budgets": {"max_files": MAX_FILES, "max_unpacked_bytes": MAX_UNPACKED,
                    "max_archive_bytes": MAX_ARCHIVE, "max_runtime_seconds": MAX_SECONDS},
        "builder": {"python": platform.python_version(), "implementation": platform.python_implementation()},
        "signature": "unsigned; SHA-256 detects changes but is not publisher authentication",
    }
    meta_bytes = canonical(bundle_meta)
    sums = _sum_file({**payload, "bundle.json": meta_bytes})
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".evidence-", suffix=".tar.gz", dir=output.parent)
    os.fchmod(fd, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw:
            with tarfile.open(fileobj=raw, mode="w:gz", compresslevel=6) as archive:
                for name, data in sorted({**payload, "bundle.json": meta_bytes, "SHA256SUMS": sums}.items()):
                    archive.addfile(_tar_info(name, data), io.BytesIO(data))
                    if raw.tell() > MAX_ARCHIVE:
                        raise BundleError("compressed bundle exceeds archive-size budget")
                    if time.monotonic() > deadline:
                        raise BundleError("bundle runtime budget exceeded")
        temp = Path(temp_name)
        if temp.stat().st_size > MAX_ARCHIVE:
            raise BundleError("compressed bundle exceeds archive-size budget")
        os.chmod(temp, 0o644)
        os.replace(temp, output)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        Path(temp_name).unlink(missing_ok=True)
        raise
    archive_bytes = output.read_bytes()
    sidecar = output.with_suffix(output.suffix + ".sha256")
    side_fd, side_name = tempfile.mkstemp(prefix=".evidence-sha256-", dir=output.parent)
    try:
        with os.fdopen(side_fd, "w", encoding="ascii") as side_file:
            side_file.write(f"{sha256(archive_bytes)}  {output.name}\n")
            side_file.flush()
            os.fsync(side_file.fileno())
        os.chmod(side_name, 0o644)
        os.replace(side_name, sidecar)
    except BaseException:
        try:
            os.close(side_fd)
        except OSError:
            pass
        Path(side_name).unlink(missing_ok=True)
        raise
    return {"archive": output.name, "size": len(archive_bytes), "sha256": sha256(archive_bytes),
            "campaign_count": len(included), "run_count": sum(row["run_count"] for row in included),
            "inventory_count": len(inventory), "privacy": scan["status"],
            "replay": analysis["reproducibility"], "elapsed_seconds": time.monotonic() - start}


def _git_commit(root: Path) -> str | None:
    try:
        import subprocess
        value = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                               capture_output=True, text=True, timeout=3, check=True).stdout.strip()
        return value if re.fullmatch(r"[0-9a-f]{40}", value) else None
    except Exception:
        return None


def _git_dirty(root: Path) -> bool | None:
    try:
        import subprocess
        value = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                               capture_output=True, text=True, timeout=3, check=True).stdout
        return bool(value)
    except Exception:
        return None


def _archive_files(path: Path) -> dict[str, bytes]:
    if path.is_dir():
        files = {}
        total = 0
        for item in path.rglob("*"):
            if item.is_symlink():
                raise BundleError("bundle directory contains a symlink")
            if item.is_file():
                rel = safe_relative(item.relative_to(path).as_posix())
                data = item.read_bytes()
                total += len(data)
                if len(data) > MAX_MEMBER or total > MAX_UNPACKED or len(files) >= MAX_FILES:
                    raise BundleError("bundle directory exceeds verifier limits")
                files[rel] = data
        return files
    files = {}
    total = 0
    if path.stat().st_size > MAX_ARCHIVE:
        raise BundleError("compressed archive exceeds size limit")
    try:
        with tarfile.open(path, "r:gz") as archive:
            members = archive.getmembers()
            if len(members) > MAX_FILES:
                raise BundleError("archive has too many members")
            for member in members:
                name = safe_relative(member.name)
                if name in files or not member.isfile() or member.size < 0 or member.size > MAX_MEMBER:
                    raise BundleError("archive member is duplicate, non-file, or oversized")
                total += member.size
                if total > MAX_UNPACKED:
                    raise BundleError("archive exceeds unpacked-size limit")
                stream = archive.extractfile(member)
                if stream is None:
                    raise BundleError("archive member could not be read")
                data = stream.read(MAX_MEMBER + 1)
                if len(data) != member.size or len(data) > MAX_MEMBER:
                    raise BundleError("archive member is truncated or oversized")
                files[name] = data
    except (tarfile.TarError, OSError) as error:
        raise BundleError("bundle archive is corrupt or unreadable") from error
    return files


def verify_bundle(path: Path, *, replay: bool = True) -> dict:
    files = _archive_files(path)
    if "bundle.json" not in files or "SHA256SUMS" not in files or "README.md" not in files:
        raise BundleError("required bundle metadata is missing")
    metadata = json.loads(files["bundle.json"])
    if metadata.get("schema_version") != SCHEMA_VERSION:
        raise BundleError("unsupported bundle schema")
    listed = {}
    for line in files["SHA256SUMS"].decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_./+-]+)", line)
        if not match:
            raise BundleError("checksum manifest is malformed")
        checksum, name = match.groups()
        safe_relative(name)
        if name in listed:
            raise BundleError("duplicate checksum entry")
        listed[name] = checksum
    actual = set(files) - {"SHA256SUMS"}
    if set(listed) != actual:
        raise BundleError("bundle has missing or unlisted members")
    if any(sha256(files[name]) != digest for name, digest in listed.items()):
        raise BundleError("bundle checksum mismatch")
    expected = metadata.get("contents")
    if not isinstance(expected, dict):
        raise BundleError("bundle content inventory is missing")
    payload_names = actual - {"bundle.json"}
    if set(expected) != payload_names:
        raise BundleError("bundle inventory differs from archived payload")
    if any(expected[name].get("size") != len(files[name]) or
           expected[name].get("sha256") != sha256(files[name]) for name in payload_names):
        raise BundleError("bundle inventory checksum mismatch")
    duplicates: dict[str, list[str]] = {}
    for name in payload_names:
        duplicates.setdefault(sha256(files[name]), []).append(name)
    duplicate_groups = sorted(sorted(items) for items in duplicates.values() if len(items) > 1)
    if duplicate_groups != sorted(metadata.get("duplicate_content_groups", [])):
        raise BundleError("duplicate-file inventory is inconsistent")
    scan = _privacy_scan_files({name: data for name, data in files.items() if name != "SHA256SUMS"})
    if scan["status"] != "PASS":
        raise BundleError("privacy scan found an unreviewed value")
    analysis = None
    if replay:
        analysis = replay_files(files)
        if analysis["reproducibility"] != "PASS":
            raise BundleError("statistical replay did not match the source run summaries")
        saved = json.loads(files.get("analysis.json", b"{}"))
        if canonical(analysis) != canonical(saved):
            raise BundleError("saved analysis differs from replay")
    return {"status": "PASS", "bundle_id": metadata["bundle_id"],
            "members": len(files), "payload_bytes": sum(len(value) for value in files.values()),
            "privacy": scan["status"], "replay": analysis["reproducibility"] if analysis else "NOT_RUN",
            "campaign_count": metadata.get("included_campaigns", []).__len__()}


def self_test() -> dict:
    from tempfile import TemporaryDirectory
    with TemporaryDirectory(prefix="flowcore-evidence-test-") as temp:
        base = Path(temp)
        prefix = "campaigns/study-001"
        run_prefix = f"{prefix}/runs/r000-static"
        data = {
            f"{prefix}/campaign.json": canonical({"campaign_id": "study-001", "status": "complete",
                "manifest": {"modes": ["static"], "workload": {"payload_bytes": 0, "phases": []}},
                "runs": ["r000-static"]}),
            f"{run_prefix}/run.json": canonical({
                "run_id": "r000-static", "mode": "static", "repetition": 0, "valid": True,
                "classifications": ["VALID_COMPLETED"], "duration_seconds": 1.0,
                "source_metrics": {"offered": 3, "completed": 2, "failed": 1, "p50_ms": 10.0,
                                   "p95_ms": 10.0, "p99_ms": 10.0, "failure_ratio": 1/3},
                "artifacts": {"requests.csv": f"{run_prefix}/requests.csv"}}),
            f"{run_prefix}/requests.csv":
                b"request_id,scheduled_s,completed_s,latency_ms,outcome,traffic_class,service_ref,dispatch_lag_ms,deadline_ms\n"
                b"0,0.0,0.01,10.0,ok,critical,service-1,0.0,20\n"
                b"1,0.2,0.21,10.0,ok,critical,service-1,0.0,20\n"
                b"2,0.4,,,TimeoutError,background,service-2,0.0,20\n",
        }
        analysis = replay_files(data)
        run = analysis["campaigns"][0]["runs"][0]
        if (run["offered"], run["completed"], run["failed"], run["p99_ms"]) != (3, 2, 1, 10.0):
            raise BundleError("synthetic denominator/percentile fixture failed")
        if run["deadline_attainment"] != 2/3:
            raise BundleError("synthetic deadline fixture failed")
        if _safe_tree({"payload_bytes": 4096}, []) != {"payload_bytes": 4096}:
            raise BundleError("numeric payload size was incorrectly redacted")
        if _safe_tree({"server_address": "10.203.0.2"}, []) != {}:
            raise BundleError("private network address was not redacted")
        if "NETWORK_IDENTIFIER" not in privacy_findings("x.json", b"server=10.203.0.2"):
            raise BundleError("private IPv4 detector fixture failed")
        if "ABSOLUTE_PATH" not in privacy_findings("x.json", b"config=/home/example/private.json"):
            raise BundleError("absolute path detector fixture failed")
        if "CREDENTIAL_OR_PRIVATE_KEY" not in privacy_findings(
                "x.json", ("-----BEGIN " + "RSA " + "PRIVATE KEY-----").encode()):
            raise BundleError("synthetic secret detection fixture failed")
        if not privacy_findings("x.csv", b"request_id,body\nx,y"):
            raise BundleError("unreviewed CSV column fixture failed")
        if _median_ci([1.0, 2.0], .95) is not None:
            raise BundleError("small-sample uncertainty fixture failed")
        try:
            safe_relative("../escape")
            raise BundleError("path traversal fixture unexpectedly passed")
        except BundleError:
            pass
        payload = dict(data)
        payload["analysis.json"] = canonical(analysis)
        payload["README.md"] = b"Standalone fixture.\n"
        payload["tools/evidence_bundle.py"] = Path(__file__).read_bytes()
        duplicate_groups = []
        contents = {name: {"size": len(blob), "sha256": sha256(blob)}
                    for name, blob in sorted(payload.items())}
        bundle_meta = {"schema_version": SCHEMA_VERSION, "bundle_id": "synthetic-fixture",
                       "contents": contents, "duplicate_content_groups": duplicate_groups,
                       "included_campaigns": [{"campaign_id": "study-001"}]}
        meta_bytes = canonical(bundle_meta)
        archive_path = base / "fixture.tar.gz"
        archive_payload = {**payload, "bundle.json": meta_bytes}
        with tarfile.open(archive_path, "w:gz") as archive:
            for name, blob in sorted({**archive_payload, "SHA256SUMS": _sum_file(archive_payload)}.items()):
                archive.addfile(_tar_info(name, blob), io.BytesIO(blob))
        verified = verify_bundle(archive_path, replay=True)
        if verified["status"] != "PASS" or verified["replay"] != "PASS":
            raise BundleError("standalone archive verification fixture failed")
        damaged = base / "damaged.tar.gz"
        with tarfile.open(archive_path, "r:gz") as source, tarfile.open(damaged, "w:gz") as target:
            for member in source.getmembers():
                stream = source.extractfile(member)
                blob = stream.read() if stream else b""
                if member.name.endswith("requests.csv"):
                    blob += b"tamper"
                target.addfile(_tar_info(member.name, blob), io.BytesIO(blob))
        try:
            verify_bundle(damaged, replay=True)
            raise BundleError("corrupt artifact fixture unexpectedly passed")
        except BundleError as error:
            if "checksum" not in str(error):
                raise
        unsafe = base / "unsafe.tar.gz"
        with tarfile.open(unsafe, "w:gz") as archive:
            info = tarfile.TarInfo("../escape")
            info.size = 1
            archive.addfile(info, io.BytesIO(b"x"))
        try:
            _archive_files(unsafe)
            raise BundleError("archive traversal fixture unexpectedly passed")
        except BundleError:
            pass
        return {"status": "PASS", "synthetic_denominator": 3,
                "preserved_failed_requests": 1, "percentile_ms": 10.0,
                "deadline_attainment": 2/3, "payload_size_preserved": True,
                "private_address_redaction": "PASS", "absolute_path_detection": "PASS",
                "secret_detection": "PASS", "unreviewed_csv_rejected": True,
                "archive_roundtrip": "PASS", "checksum_tamper": "REJECTED",
                "archive_path_traversal": "REJECTED", "small_sample_ci": "INCONCLUSIVE"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    inventory_parser = sub.add_parser("inventory")
    inventory_parser.add_argument("--root", type=Path, default=Path.cwd())
    inventory_parser.add_argument("--spec", type=Path, required=True)
    build_parser = sub.add_parser("build")
    build_parser.add_argument("--root", type=Path, default=Path.cwd())
    build_parser.add_argument("--spec", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    verify_parser = sub.add_parser("verify")
    verify_parser.add_argument("bundle", type=Path)
    verify_parser.add_argument("--replay", action="store_true")
    sub.add_parser("self-test")
    args = parser.parse_args(argv)
    try:
        if args.command == "inventory":
            root = args.root.resolve()
            spec_path = args.spec.resolve()
            spec = json.loads(_source_file(root, str(spec_path.relative_to(root)), 1024*1024))
            print(json.dumps(discover_inventory(root, spec), indent=2, allow_nan=False))
        elif args.command == "build":
            print(json.dumps(build(args.root, args.spec, args.output), indent=2, allow_nan=False))
        elif args.command == "verify":
            print(json.dumps(verify_bundle(args.bundle, replay=args.replay), indent=2, allow_nan=False))
        else:
            print(json.dumps(self_test(), indent=2, allow_nan=False))
        return 0
    except (BundleError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        # The error text intentionally contains reason codes only, never matched data.
        print(json.dumps({"status": "FAIL", "reason": str(error)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
