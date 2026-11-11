#!/usr/bin/env python3
"""Run real, dependency-resolved RPM transactions in a disposable DNF installroot."""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv, *, timeout=900, log=None):
    result = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    if log is not None:
        with log.open("a") as stream:
            stream.write("$ " + " ".join(map(str, argv)) + "\n")
            stream.write(result.stdout)
            stream.write(result.stderr)
            stream.write("\n")
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(map(str, argv))}\n"
                           f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}")
    return result.stdout.strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--packages-dir", type=Path, required=True)
    parser.add_argument("--source-archive", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--releasever", default="45")
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("dependency-resolved DNF installroot transactions require root in the disposable CI container")
    for binary in ("dnf", "rpm", "createrepo_c", "cmake", "python3"):
        if not shutil.which(binary):
            raise SystemExit(f"missing required package test tool: {binary}")
    packages_dir = args.packages_dir.resolve()
    source_archive = args.source_archive.resolve()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    rpms = {}
    for release in (1, 2, 3):
        matches = sorted(packages_dir.glob(f"flowcore-0.2.0-{release}*.rpm"))
        if len(matches) != 1:
            raise SystemExit(f"expected exactly one release {release} RPM in {packages_dir}; found {len(matches)}")
        rpms[release] = matches[0].resolve()

    log = output_dir / "dnf-transactions.log"
    log.write_text("")
    with tempfile.TemporaryDirectory(prefix="flowcore-dnf-", dir=output_dir) as temporary:
        work = Path(temporary)
        installroot = work / "root"
        repo = work / "repo"
        cache = work / "cache"
        installroot.mkdir()
        repo.mkdir()
        cache.mkdir()
        installroot_repos = installroot / "etc/yum.repos.d"
        installroot_repos.mkdir(parents=True)
        repo_dirs = (
            Path("/etc/yum.repos.d"),
            Path("/etc/dnf/repos.d"),
            Path("/usr/share/dnf5/repos.d"),
            Path("/usr/share/dnf/repos.d"),
        )
        host_repo_files = {}
        for repo_dir in repo_dirs:
            for repo_file in sorted(repo_dir.glob("*.repo")):
                host_repo_files.setdefault(repo_file.name, repo_file)
        if not host_repo_files:
            searched = ", ".join(str(path) for path in repo_dirs)
            raise SystemExit(f"Fedora repository definitions are unavailable; searched: {searched}")
        for repo_file in host_repo_files.values():
            shutil.copy2(repo_file, installroot_repos / repo_file.name)
        with log.open("a") as stream:
            stream.write("Fedora repositories copied into installroot: "
                         + ", ".join(sorted(host_repo_files)) + "\n")
        key_dirs = (Path("/etc/pki/rpm-gpg"), Path("/usr/share/pki/rpm-gpg"))
        host_gpg_keys = {}
        for key_dir in key_dirs:
            for key in sorted(key_dir.glob("RPM-GPG-KEY-fedora*")):
                host_gpg_keys.setdefault(key.name, key)
        if not host_gpg_keys:
            searched = ", ".join(str(path) for path in key_dirs)
            raise SystemExit(f"Fedora repository signing keys are unavailable; searched: {searched}")
        # Fedora 45 repo definitions point into /usr/share/pki; also seed the
        # legacy path for repo files that still use /etc/pki/rpm-gpg.
        for destination in (installroot / "usr/share/pki/rpm-gpg",
                            installroot / "etc/pki/rpm-gpg"):
            destination.mkdir(parents=True, exist_ok=True)
            for key in host_gpg_keys.values():
                shutil.copy2(key, destination / key.name)
        for artifact in rpms.values():
            shutil.copy2(artifact, repo / artifact.name)
        run(["createrepo_c", "--quiet", repo], log=log)
        flags = [
            "dnf", "--installroot", str(installroot), "--releasever", args.releasever,
            f"--setopt=reposdir={installroot_repos}",
            "--setopt=module_platform_id=platform:f45",
            "--setopt=install_weak_deps=False",
            f"--setopt=cachedir={cache}",
            f"--repofrompath=flowcore,file://{repo}", "--enablerepo=flowcore",
            "--setopt=flowcore.gpgcheck=0", "--assumeyes",
        ]

        def dnf(*argv):
            return run([*flags, *argv], log=log)

        def version():
            return run(["rpm", "--root", installroot, "-q", "--qf", "%{VERSION}-%{RELEASE}", "flowcore"])

        dnf("install", rpms[1])
        assert version().startswith("0.2.0-1"), version()
        resolved_package_count = len(run([
            "rpm", "--root", installroot, "-qa", "--qf", "%{NAME}\n"
        ]).splitlines())
        package_specs = {
            release: run(["rpm", "-qp", "--qf",
                          "%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}", artifact])
            for release, artifact in rpms.items()
        }
        config = installroot / "etc/flowcore/service.json"
        original = config.read_bytes()
        modified = original + b"\n"
        config.write_bytes(modified)
        state = installroot / "var/lib/flowcore/state"
        state.mkdir(parents=True, exist_ok=True)
        marker = state / "transaction-preservation-marker"
        marker.write_text("preserve-across-upgrade-downgrade-remove\n")
        marker.chmod(0o600)

        dnf("upgrade", package_specs[3])
        assert version().startswith("0.2.0-3"), version()
        assert config.read_bytes() == modified, "operator configuration changed during upgrade"
        assert marker.read_text() == "preserve-across-upgrade-downgrade-remove\n"
        installed_cli = installroot / "usr/bin/flowcored"
        config_check = run([installed_cli, "--check-config", "--config", config])
        assert "valid" in config_check.lower() or config_check == "", config_check

        before_invalid = config.read_bytes()
        invalid = json.loads(before_invalid)
        invalid["version"] = 999
        config.write_text(json.dumps(invalid))
        rejected = subprocess.run([installed_cli, "--check-config", "--config", config],
                                  text=True, capture_output=True, timeout=20)
        assert rejected.returncode != 0, "unsupported config schema was accepted"
        assert config.read_text() == json.dumps(invalid), "failed schema check unexpectedly rewrote config"
        config.write_bytes(before_invalid)
        run([installed_cli, "--check-config", "--config", config])

        units = ("flowcored.service", "flowcore-monitor.service", "flowcore-dashboard.service",
                 "flowcore-sdk.socket", "flowcore-network.socket", "flowcore-network.service")
        wants = ("multi-user.target.wants", "sockets.target.wants", "default.target.wants")
        for target in units:
            for group in wants:
                path = installroot / "etc/systemd/system" / group / target
                assert not path.exists(), f"package unexpectedly enabled {target}"
        scriptlets = run(["rpm", "-qp", "--scripts", rpms[3]])
        assert "daemon-reload" in scriptlets and "try-restart" in scriptlets
        assert "systemctl enable" not in scriptlets and "systemctl preset" not in scriptlets
        assert "systemctl start" not in scriptlets

        sbom = output_dir / "flowcore-runtime.spdx.json"
        run(["python3", ROOT / "packaging/generate-sbom.py", "--rpm", rpms[3],
             "--installroot", installroot, "--source-archive", source_archive, "--output", sbom])
        manifest = json.loads(sbom.read_text())
        assert manifest["spdxVersion"] == "SPDX-2.3"
        assert any(package["name"] == "flowcore" for package in manifest["packages"])

        dnf("downgrade", package_specs[2])
        assert version().startswith("0.2.0-2"), version()
        assert config.read_bytes() == modified, "operator configuration changed during downgrade"
        assert marker.is_file(), "persistent state lost during downgrade"

        dnf("remove", "flowcore")
        removed = subprocess.run(["rpm", "--root", str(installroot), "-q", "flowcore"],
                                 text=True, capture_output=True, timeout=20)
        assert removed.returncode != 0, "flowcore remained installed after DNF erase"
        assert not (installroot / "usr/bin/flowcore").exists()
        saved = config.with_suffix(".json.rpmsave")
        assert saved.read_bytes() == modified, "modified operator config was not retained on erase"
        assert marker.read_text() == "preserve-across-upgrade-downgrade-remove\n"

    report = {
        "schema": 1,
        "status": "PASS",
        "transaction_scope": "dependency-resolved DNF transactions in a disposable Fedora installroot",
        "operations": ["install 0.2.0-1", "upgrade 0.2.0-3", "downgrade 0.2.0-2", "erase"],
        "scriptlets": "executed by DNF/RPM; no service enable/start observed",
        "service_manager": "installroot has no active PID 1; live systemd activation lifecycle is not tested here",
        "configuration": "version-1 validated; unknown schema rejected without rewrite; noreplace preserved; rpmsave retained on erase",
        "persistent_state": "operator marker survived upgrade, downgrade, and package erase",
        "resolved_installroot_package_count": resolved_package_count,
        "source_archive_sha256": digest(source_archive),
        "package_sha256": {str(release): digest(path) for release, path in rpms.items()},
        "dnf_log": log.name,
        "sbom": sbom.name,
        "official_signature": "not signed by this test",
    }
    report_path = output_dir / "transaction-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
