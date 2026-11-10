%global debug_package %{nil}
Name:           flowcore
Version:        0.2.0
Release:        3%{?dist}
Summary:        Linux adaptive networking laboratory and read-only interface monitor
License:        Apache-2.0
Source0:        %{name}-%{version}.tar.gz
BuildRequires:  gcc-c++ >= 13
BuildRequires:  cmake
BuildRequires:  ninja-build
BuildRequires:  python3
BuildRequires:  openssl-devel
BuildRequires:  openssl
Requires:       python3 >= 3.12
Requires(post): systemd
Requires(preun): systemd
Requires(postun): systemd
Requires:       iproute
Requires:       iproute-tc
Requires:       util-linux
Requires:       ethtool
Recommends:     nftables
Recommends:     podman

%description
FLOWCORE monitors Linux interfaces and runs explicitly isolated adaptive
networking experiments. The shipped systemd unit runs a read-only monitor.
The optional BPF collector is omitted from this RPM.

%prep
%autosetup

%build
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=%{_prefix} -DCMAKE_INSTALL_SYSCONFDIR=%{_sysconfdir} \
  -DCMAKE_INSTALL_LIBDIR=%{_lib} -DFLOWCORE_BPF=OFF \
  -DCMAKE_CXX_FLAGS="-ffile-prefix-map=$PWD=/usr/src/flowcore"
cmake --build build

%check
ctest --test-dir build --output-on-failure

%install
DESTDIR=%{buildroot} cmake --install build

%files
%license LICENSE
%doc README.md ARCHITECTURE.md
%dir %attr(0755,root,root) %{_sysconfdir}/flowcore
%config(noreplace) %attr(0644,root,root) %{_sysconfdir}/flowcore/service.json
%config(noreplace) %attr(0644,root,root) %{_sysconfdir}/flowcore/helper.json
%{_bindir}/flowcore
%{_bindir}/flowcored
%{_bindir}/flowcore-sdk-request
%{_bindir}/flowcore-udp-intent-demo
%{_bindir}/flowid-client
%{_bindir}/flowcore-container-request
%{_datadir}/flowcore/
%{_includedir}/flowcore/
%{_includedir}/flowid/
%{_libdir}/libflowcore_sdk.a
%{_libdir}/cmake/flowcore/
%{_prefix}/lib/systemd/system/flowcored.service
%{_prefix}/lib/systemd/system/flowcore-monitor.service
%{_prefix}/lib/systemd/system/flowcore-dashboard.service
%{_prefix}/lib/systemd/system/flowcore-network.service
%{_prefix}/lib/systemd/system/flowcore-network.socket
%{_prefix}/lib/systemd/system/flowcore-sdk.socket
%{_prefix}/lib/sysusers.d/flowcore-operator.conf
%{_prefix}/lib/sysusers.d/flowcore-dashboard.conf
%{_bindir}/libflowcore_intelligence.so
%{_bindir}/libflowcore_telemetry.so*

%{_libdir}/libflowcore_intent.so
%{_libdir}/libflowcore_flowid.so
%{_libdir}/libflowcore_pacing_lab.so


%post
# Deliberately do not enable/preset/start any service.
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :

%preun
if [ "$1" -eq 0 ]; then
    /usr/bin/systemctl stop flowcore-dashboard.service flowcored.service flowcore-monitor.service flowcore-sdk.socket flowcore-network.socket flowcore-network.service >/dev/null 2>&1 || :
fi

%postun
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :
if [ "$1" -ge 1 ]; then
    /usr/bin/systemctl try-restart flowcored.service flowcore-monitor.service flowcore-dashboard.service >/dev/null 2>&1 || :
fi
