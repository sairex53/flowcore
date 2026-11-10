# Release and deployment

FLOWCORE builds C++23 targets for Linux. The source monitor and staged
installation were validated on Fedora 45 ARM64. An independent Ubuntu x86_64 VM passed the scoped namespace service
and recovery tests with the pinned tc toolchain. See [candidate
validation](INDEPENDENT_VALIDATION.md) for the exact environments.

## Staged installation

```sh
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build
ctest --test-dir build --output-on-failure
python3 -B tests/install_test.py
```

The test installs into a temporary DESTDIR under build/, starts the
installed monitor with zero effective capabilities, checks its watchdog
messages, builds a downstream CMake consumer of `flowcore::sdk`, verifies
the unit syntax, and removes exactly the staged files. This does not modify
the host's package database or systemd services.

## Fedora RPM

On a Fedora build host install rpm-build, gcc-c++, cmake, ninja-build and
python3. Use a clean source tree and record the compiler, package versions,
source revision and release epoch. The archive uses the committed Git tree,
the commit timestamp and a timestamp-free gzip stream.

```sh
export SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)"
archive=$(./packaging/source-archive.sh)
mkdir -p build/rpm/{BUILD,BUILDROOT,RPMS,SOURCES,SPECS,SRPMS,tmp}
cp "$archive" build/rpm/SOURCES/
rpmbuild -ba --define "_topdir $PWD/build/rpm" \
  --define "_tmppath $PWD/build/rpm/tmp" packaging/flowcore.spec
sha256sum -c "$archive.sha256"
rpm -K --nosignature build/rpm/RPMS/*/*.rpm
python3 -B packaging/verify-rpm.py build/rpm/RPMS/*/*.rpm
rpm -qpl build/rpm/RPMS/*/*.rpm
```

Build two clean environments with the same tools and epoch before claiming
bit-identical binary reproducibility. The local package is unsigned. A
digest verifies bytes without authenticating the publisher. The project and
RPM declare Apache-2.0. See the [release gates](FLOWCORE_1_0_READINESS.md)
for the remaining packaging and provenance work.

After selecting a trusted package, install it with dnf. The RPM includes
a systemd unit but does not enable it automatically. Review
/etc/flowcore/service.json before opting in to `systemctl enable --now flowcored`.
The service uses DynamicUser, no capabilities, a private runtime socket,
read-only monitoring and a separate StateDirectory. Root can query its
read endpoint. The privileged lab is a separate explicit invocation and
needs a different runtime path if the monitor is running.

Uninstall the installed RPM with dnf after stopping the service. Policy
state is deliberately separate from installed code and can be archived
or removed by the operator. Laboratory scope requires network namespaces,
veth, HTB, fq_codel and flower. CAKE, MPTCP, BPF and cgroup v2 plus nft socket
matching are capability-dependent. The Fedora package disables optional BPF
at build time, while the source build can use clang, bpftool, libbpf and BTF.
