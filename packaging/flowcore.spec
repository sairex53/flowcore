%global debug_package %{nil}
Name:           flowcore
Version:        0.2.0
Release:        1%{?dist}
Summary:        Linux adaptive networking laboratory and read-only interface monitor
License:        LicenseRef-FLOWCORE-Unspecified
Source0:        %{name}-%{version}.tar.gz
BuildRequires:  gcc-c++
BuildRequires:  cmake
BuildRequires:  ninja-build
BuildRequires:  python3
Requires:       python3 >= 3.12
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
%doc README.md ARCHITECTURE.md
%config(noreplace) %{_sysconfdir}/flowcore/service.json
%config(noreplace) %{_sysconfdir}/flowcore/helper.json
%{_bindir}/flowcore
%{_bindir}/flowcored
%{_bindir}/flowcore-sdk-request
%{_bindir}/flowcore-container-request
%{_datadir}/flowcore/
%{_includedir}/flowcore/
%{_libdir}/libflowcore_sdk.a
%{_libdir}/cmake/flowcore/
%{_prefix}/lib/systemd/system/flowcored.service
%{_prefix}/lib/systemd/system/flowcore-network.service
%{_prefix}/lib/systemd/system/flowcore-network.socket
%{_prefix}/lib/sysusers.d/flowcore-operator.conf
%{_bindir}/libflowcore_intelligence.so
