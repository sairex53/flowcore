# FLOWCORE release provenance

## Source identity

Release source archives are produced only from a clean tracked Git commit. packaging/source-archive.sh requires SOURCE_DATE_EPOCH to equal that commit's author timestamp, uses git archive, and writes a SHA-256 file. Untracked files are not included. The provenance tool records the full commit and archive hash and refuses a dirty checkout.

## Build record

packaging/release-provenance.py captures:

- Full Git commit and source archive SHA-256.
- Fedora release, architecture, kernel, compiler, CMake, Ninja, Python, RPM versions, and versions of declared build packages.
- RPM, SRPM, SPDX SBOM, DNF transaction report, and reproducibility report hashes.
- Reproduction command sequence and the exact RPM CMake configure, build, test, install, and rpmbuild settings (with temporary topdir represented by a placeholder).
- Official signing status.

Paths are recorded by artifact filename, not by absolute workspace path. Build records do not include credentials or private keys.

## Reproducibility

The source tarball is generated from Git and is tested byte-for-byte by the release-gate suite. RPM builds set SOURCE_DATE_EPOCH, a fixed build host, RPM build time, and timestamp clamping where supported. The Fedora release workflow builds the same RPM/SRPM twice with fixed source/build paths and separate output roots, then compares SHA-256 digests. RPM embeds expanded build paths in SRPM script sections, so the build context is deliberately normalized; this validates repeatability on the same architecture and toolchain rather than cross-host reproducibility.

A PASS only means those artifacts were byte-identical on the recorded toolchain and architecture. It does not prove cross-compiler, cross-architecture, or independently administered reproducibility. Build IDs, compiler/linker versions, RPM payload implementation, and distro dependency updates can change artifacts. Signed RPMs also differ from unsigned builds because signatures are embedded.

## Reproduction

On Fedora 45, build from a clean checkout with the documented tools:

    export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
    python3 -B packaging/build-local-rpm.py
    python3 -B packaging/release-provenance.py \
      --source-archive build/release/flowcore-0.2.0.tar.gz \
      --artifacts build/rpm-deploy/RPMS/$(rpm --eval '%{_arch}')/flowcore-*.rpm \
      --output build/release/release-provenance.json

The CI workflow additionally resolves dependencies in a clean DNF installroot, emits the runtime SPDX SBOM, compares two independent topdir builds, and performs test-key signing on copies. Official project signing remains blocked until an operator-managed production key is provisioned.
