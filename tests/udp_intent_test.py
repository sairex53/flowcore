"""Policy validation, service authorization, and optional live IPv4/IPv6 UDP SDK test."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
BUILD = Path(os.environ.get("FLOWCORE_UDP_INTENT_BUILD", ROOT / "build"))
sys.path.insert(0, str(ROOT / "scripts"))
from daemon import Service
from intent_v2 import cgroup_matches
from service_config import load

def current_cgroup():
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::"):
            return line[3:]
    raise RuntimeError("unified cgroup identity is unavailable")

def policy(uid, cgroup):
    return dict(priority="critical", uids=[uid], cgroup_prefix=cgroup, socket_mark=42,
                urgent_rate_pps=1, urgent_burst=1, max_deadline_ms=100)

class UdpIntentUnit(unittest.TestCase):
    def setUp(self):
        self.config = load(ROOT / "config/service.json")
        self.config["intent_api"]["priorities"] = ["standard", "interactive", "critical"]
        self.config["intent_api"]["udp_message_policies"] = {
            "udp_fixture4": policy(os.getuid(), current_cgroup()),
            "udp_fixture6": policy(os.getuid(), current_cgroup()),
        }
        self.service = Service(self.config)
        self.api = self.service.intent_api

    def tearDown(self):
        for token in list(self.api.leases):
            self.api.release(token, "test_cleanup")

    def register(self, name="udp_fixture4", priority="critical"):
        return self.api.call(dict(op="intent_v2", version=2, action="register", isolation="shared",
            intent=dict(name=name, priority=priority)), os.getuid(), os.getpid())

    def test_cgroup_boundary_and_udp_grant(self):
        self.assertTrue(cgroup_matches(f"0::{current_cgroup()}", current_cgroup()))
        self.assertFalse(cgroup_matches("0::/system.slice/fake.service", current_cgroup()))
        self.assertTrue(cgroup_matches(f"0::{current_cgroup()}/child", current_cgroup()))
        self.assertFalse(cgroup_matches("2:cpu:/unified-only", current_cgroup()))
        granted = self.register()
        self.assertGreater(granted["udp_message"].pop("expires_ns"), time.monotonic_ns())
        self.assertEqual(granted["udp_message"], dict(authorized=True, priority="critical",
                         socket_mark=42, urgent_rate_pps=1, urgent_burst=1, max_deadline_ms=100))
        with self.assertRaises(PermissionError):
            self.register(name="udp_fixture6", priority="standard")
        self.assertNotIn("udp_message", self.register(name="unconfigured", priority="standard"))

    def test_udp_grant_rejects_wrong_uid_and_cgroup(self):
        with self.assertRaises(PermissionError):
            self.api.call(dict(op="intent_v2", version=2, action="register", isolation="shared",
                intent=dict(name="udp_fixture4", priority="critical")), os.getuid()+1, os.getpid())
        with patch("intent_v2.identity", return_value=("1", "0::/system.slice/untrusted.service")):
            with self.assertRaises(PermissionError):
                self.register()

    def test_renewal_revokes_moved_service(self):
        from intent_v2 import identity
        token = self.register()["lease"]
        start, _ = identity(os.getpid())
        with patch("intent_v2.identity", return_value=(start, "0::/system.slice/untrusted.service")):
            with self.assertRaises(PermissionError):
                self.api.call(dict(op="intent_v2", version=2, action="renew", lease=token),
                              os.getuid(), os.getpid())
        self.assertNotIn(token, self.api.leases)

    def test_schema_is_strict_and_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            self.config["intent_api"]["udp_message_policies"]["udp_fixture4"]["urgent_burst"] = 10001
            path.write_text(json.dumps(self.config))
            with self.assertRaises(ValueError):
                load(path)
            self.config["intent_api"]["udp_message_policies"]["udp_fixture4"]["urgent_burst"] = 1
            self.config["intent_api"]["udp_message_policies"]["udp_fixture4"]["unexpected"] = True
            path.write_text(json.dumps(self.config))
            with self.assertRaises(ValueError):
                load(path)

def live():
    if os.geteuid() != 0:
        print("SKIP live UDP SDK: SO_MARK requires an operator-authorized marked socket")
        raise SystemExit(77)
    config = load(ROOT / "config/service.json")
    config["intent_api"]["priorities"] = ["standard", "interactive", "critical"]
    cg = current_cgroup()
    config["intent_api"]["udp_message_policies"] = {
        name: policy(os.getuid(), cg) for name in ("udp_fixture4", "udp_fixture6")
    }
    with tempfile.TemporaryDirectory(dir=BUILD, prefix="udp-intent-") as temp:
        temp = Path(temp)
        config_path = temp / "config.json"
        config_path.write_text(json.dumps(config))
        runtime = temp / "run"
        env = dict(os.environ, FLOWCORE_RUNTIME=str(runtime), FLOWCORE_STATE=str(temp / "state"))
        log_path = temp / "daemon.log"
        with log_path.open("w") as log:
            daemon = subprocess.Popen([BUILD / "flowcored", "--config", config_path],
                                      env=env, stdout=log, stderr=log)
            try:
                endpoint = runtime / "control.sock"
                for _ in range(200):
                    if endpoint.exists():
                        break
                    if daemon.poll() is not None:
                        raise RuntimeError(log_path.read_text())
                    time.sleep(0.025)
                else:
                    raise TimeoutError("FLOWCORE daemon did not create its local socket")
                result = subprocess.run([BUILD / "udp_intent_native_test", endpoint],
                                        check=False, capture_output=True, text=True, timeout=20)
                if result.returncode:
                    raise AssertionError(result.stdout + result.stderr + log_path.read_text())
                print(result.stdout.strip())
            finally:
                if daemon.poll() is None:
                    daemon.send_signal(signal.SIGTERM)
                    try:
                        daemon.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait(timeout=5)

if __name__ == "__main__":
    run_live = "--live" in sys.argv
    if run_live:
        sys.argv.remove("--live")
    program = unittest.main(exit=False)
    if not program.result.wasSuccessful():
        raise SystemExit(1)
    if run_live:
        live()
