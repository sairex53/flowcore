cmake_minimum_required(VERSION 3.20)
project(flowcore VERSION 0.2.0 LANGUAGES C CXX)
include(GNUInstallDirs)
set(FLOWCORE_DATA "${CMAKE_INSTALL_DATADIR}/flowcore")
file(RELATIVE_PATH FLOWCORE_DATA_FROM_BIN "${CMAKE_INSTALL_FULL_BINDIR}" "${CMAKE_INSTALL_FULL_DATADIR}/flowcore")
file(RELATIVE_PATH FLOWCORE_BIN_FROM_DATA "${CMAKE_INSTALL_FULL_DATADIR}/flowcore" "${CMAKE_INSTALL_FULL_BINDIR}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/installed-layout" "${FLOWCORE_BIN_FROM_DATA}\n")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/VERSION" "${PROJECT_VERSION}\n")
set(CMAKE_CXX_STANDARD 23)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)
add_library(flowcore_policy src/policy.cpp)
target_include_directories(flowcore_policy PUBLIC include)
target_compile_options(flowcore_policy PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_executable(flowcore src/main.cpp src/netlink.cpp)
target_link_libraries(flowcore PRIVATE flowcore_policy)
target_compile_options(flowcore PRIVATE -Wall -Wextra -Wpedantic -Werror)
enable_testing()
add_executable(policy_test tests/policy_test.cpp)
target_link_libraries(policy_test PRIVATE flowcore_policy)
add_test(NAME policy COMMAND policy_test)

target_compile_definitions(flowcore PRIVATE FLOWCORE_CLI_PATH="${CMAKE_CURRENT_SOURCE_DIR}/scripts/cli.py"
    FLOWCORE_INSTALLED_CLI_REL="${FLOWCORE_DATA_FROM_BIN}/scripts/cli.py" FLOWCORE_SOURCE_BINARY_DIR="${CMAKE_CURRENT_BINARY_DIR}"
    FLOWCORE_VERSION="${PROJECT_VERSION}")
configure_file(scripts/flowcored.in flowcored @ONLY)
file(CHMOD "${CMAKE_CURRENT_BINARY_DIR}/flowcored"
     PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE GROUP_READ GROUP_EXECUTE WORLD_READ WORLD_EXECUTE)
configure_file(scripts/flowcored-installed.in flowcored-installed @ONLY)
install(TARGETS flowcore RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/flowcored-installed" DESTINATION "${CMAKE_INSTALL_BINDIR}" RENAME flowcored)
install(DIRECTORY scripts config scenarios DESTINATION "${FLOWCORE_DATA}"
    FILES_MATCHING PATTERN "*.py" PATTERN "*.json" PATTERN "*.conf" PATTERN "__pycache__" EXCLUDE PATTERN "flow_engine_benchmark.py" EXCLUDE PATTERN "udp_intent_lab.py" EXCLUDE)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/installed-layout" "${CMAKE_CURRENT_BINARY_DIR}/VERSION"
              "${CMAKE_CURRENT_BINARY_DIR}/bpf-enabled" DESTINATION "${FLOWCORE_DATA}")
install(FILES config/service.json config/helper.json DESTINATION "${CMAKE_INSTALL_SYSCONFDIR}/flowcore")
install(FILES packaging/flowcored.service packaging/flowcore-network.service
              packaging/flowcore-network.socket packaging/flowcore-sdk.socket DESTINATION lib/systemd/system)
install(FILES packaging/flowcore-operator.conf DESTINATION lib/sysusers.d)

add_test(NAME service_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/service_unit.py")
set_tests_properties(service_unit PROPERTIES ENVIRONMENT "FLOWCORE_TEST_BINARY=$<TARGET_FILE:flowcore>")

option(FLOWCORE_BPF "Build the optional CO-RE TCP event collector" ON)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/bpf-enabled" "0\n")
if(FLOWCORE_BPF)
    find_package(PkgConfig QUIET)
    if(PkgConfig_FOUND)
        pkg_check_modules(LIBBPF QUIET IMPORTED_TARGET libbpf)
    endif()
    find_program(BPFTOOL bpftool)
    find_program(BPF_CLANG clang)
    if(BPF_CLANG)
        execute_process(COMMAND "${BPF_CLANG}" --print-targets
            OUTPUT_VARIABLE FLOWCORE_CLANG_TARGETS ERROR_QUIET)
    endif()
    if(TARGET PkgConfig::LIBBPF AND BPFTOOL AND FLOWCORE_CLANG_TARGETS MATCHES "bpf +-" AND EXISTS /sys/kernel/btf/vmlinux)
        file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/bpf-enabled" "1\n")
        add_executable(flowcore-bpf src/observe_bpf.cpp)
        target_include_directories(flowcore-bpf PRIVATE bpf)
        target_compile_options(flowcore-bpf PRIVATE -Wall -Wextra -Werror)
        target_link_libraries(flowcore-bpf PRIVATE PkgConfig::LIBBPF)
        add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/bpf/observe.bpf.o"
            COMMAND bash "${CMAKE_CURRENT_SOURCE_DIR}/scripts/build-observer-bpf.sh"
                "${BPFTOOL}" "${BPF_CLANG}" "${CMAKE_CURRENT_BINARY_DIR}/bpf" "${CMAKE_CURRENT_SOURCE_DIR}"
            DEPENDS bpf/observe.bpf.c bpf/observe_event.h scripts/build-observer-bpf.sh /sys/kernel/btf/vmlinux
            VERBATIM)
        add_custom_target(flowcore-bpf-object ALL DEPENDS "${CMAKE_CURRENT_BINARY_DIR}/bpf/observe.bpf.o")
        add_dependencies(flowcore-bpf flowcore-bpf-object)
        if(LIBBPF_VERSION VERSION_GREATER_EQUAL "1.3")
            add_executable(flowcore-flow-engine src/flow_engine.cpp)
            target_include_directories(flowcore-flow-engine PRIVATE bpf)
            target_link_libraries(flowcore-flow-engine PRIVATE PkgConfig::LIBBPF)
            target_compile_options(flowcore-flow-engine PRIVATE -Wall -Wextra -Wpedantic -Werror)
            add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/bpf/flow_engine.bpf.o"
                COMMAND bash "${CMAKE_CURRENT_SOURCE_DIR}/scripts/build-flow-bpf.sh"
                    "${BPF_CLANG}" "${CMAKE_CURRENT_BINARY_DIR}/bpf" "${CMAKE_CURRENT_SOURCE_DIR}"
                DEPENDS flowcore-bpf-object bpf/flow_engine.bpf.c bpf/flow_engine.h bpf/flow_parse.h scripts/build-flow-bpf.sh
                VERBATIM)
            add_custom_target(flowcore-flow-object ALL DEPENDS "${CMAKE_CURRENT_BINARY_DIR}/bpf/flow_engine.bpf.o")
            add_dependencies(flowcore-flow-engine flowcore-flow-object)
            add_executable(flow_parser_kernel tests/flow_parser_kernel.cpp)
            target_include_directories(flow_parser_kernel PRIVATE bpf)
            target_link_libraries(flow_parser_kernel PRIVATE PkgConfig::LIBBPF)
            target_compile_options(flow_parser_kernel PRIVATE -Wall -Wextra -Wpedantic -Werror)
            add_dependencies(flow_parser_kernel flowcore-flow-object)
            install(TARGETS flowcore-flow-engine RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
            install(FILES "${CMAKE_CURRENT_BINARY_DIR}/bpf/flow_engine.bpf.o" DESTINATION "${FLOWCORE_DATA}/bpf")
        endif()
    else()
        message(STATUS "CO-RE build prerequisites absent, INET_DIAG fallback available")
    endif()
endif()

add_test(NAME observer_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/observer_test.py")

add_library(flowcore_sdk src/client.cpp src/intent_v2.cpp src/intent_transport.cpp
    src/socket_pacing.cpp src/pacing_controller.cpp src/deadline.cpp src/cooperative_slo.cpp src/udp_intent.cpp)
find_package(Threads REQUIRED)
target_link_libraries(flowcore_sdk PUBLIC Threads::Threads)
target_include_directories(flowcore_sdk PUBLIC $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>)
target_compile_features(flowcore_sdk PUBLIC cxx_std_23)
set_target_properties(flowcore_sdk PROPERTIES EXPORT_NAME sdk POSITION_INDEPENDENT_CODE ON)
add_library(flowcore::sdk ALIAS flowcore_sdk)
target_compile_options(flowcore_sdk PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_executable(flowcore-sdk-request examples/sdk_request.cpp)
target_link_libraries(flowcore-sdk-request PRIVATE flowcore_sdk)
install(TARGETS flowcore_sdk EXPORT FlowcoreTargets ARCHIVE DESTINATION "${CMAKE_INSTALL_LIBDIR}")
install(EXPORT FlowcoreTargets FILE flowcore-targets.cmake NAMESPACE flowcore:: DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/flowcore")
install(FILES include/flowcore/client.hpp include/flowcore/intent_v2.hpp include/flowcore/socket_pacing.hpp include/flowcore/deadline.hpp include/flowcore/udp_intent.hpp DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/flowcore")

add_test(NAME adaptive_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/adaptive_unit.py")

add_test(NAME research_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/research_test.py")
add_test(NAME overload_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/overload_test.py")
add_test(NAME measurement_integrity COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/measurement_integrity_test.py")
set_tests_properties(measurement_integrity PROPERTIES TIMEOUT 20)
install(FILES MEASUREMENT_INTEGRITY.md BENCHMARK_VALIDATION.md SCIENTIFIC_RESULTS_V3.md DESTINATION "${FLOWCORE_DATA}/docs")
add_test(NAME research_properties COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/research_properties.py")
add_executable(flowcore-container-request examples/container_request.cpp)
target_link_libraries(flowcore-container-request PRIVATE flowcore_sdk)

install(TARGETS flowcore-sdk-request flowcore-container-request RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
if(TARGET flowcore-bpf)
    install(TARGETS flowcore-bpf RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
    install(FILES "${CMAKE_CURRENT_BINARY_DIR}/bpf/observe.bpf.o" DESTINATION "${FLOWCORE_DATA}/bpf")
endif()

add_test(NAME platform_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/platform_test.py")
add_test(NAME multipath_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/multipath_test.py")
add_test(NAME path_adaptation_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/path_adaptation_test.py")
install(FILES DYNAMIC_NETWORK_ENGINE.md PATH_ADAPTATION.md PATH_ADAPTATION_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES monitoring/flowcore-grafana.json DESTINATION "${FLOWCORE_DATA}/monitoring")
install(FILES README.md ARCHITECTURE.md PLATFORM.md SECURITY.md CLI.md RELEASE.md RESEARCH.md BENCHMARK_V2.md INTENT_API.md APPLICATION_INTEGRATION.md EBPF_FLOW_ENGINE.md EBPF_PERFORMANCE.md PREDICTIVE_ENGINE.md PREDICTIVE_RESULTS.md MULTIPATH_ENGINE.md MULTIPATH_RESULTS.md
    DESTINATION "${FLOWCORE_DATA}/docs")

install(FILES PLATFORM_RESULTS.md AUDIT.md AUDIT_FIXES.md REMAINING_RISKS.md
              THREAT_MODEL.md PRIVILEGES.md ADAPTIVE_ROOT_CAUSE.md DEEP_OBSERVABILITY_REPORT.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME helper_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/helper_test.py")

add_executable(tcp_info_layout tests/tcp_info_layout.cpp)
target_compile_options(tcp_info_layout PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_test(NAME tcp_info_layout COMMAND tcp_info_layout)

add_test(NAME deep_host COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/deep_integration.py")
set_tests_properties(deep_host PROPERTIES ENVIRONMENT "FLOWCORE_TEST_BINARY=$<TARGET_FILE:flowcore>")

add_library(flowcore_intelligence SHARED src/intelligence.cpp src/predictive.cpp src/optimizer.cpp src/slo_optimizer.cpp src/deadline.cpp src/deadline_bridge.cpp src/cooperative_slo.cpp src/cooperative_slo_bridge.cpp)
target_include_directories(flowcore_intelligence PRIVATE include)
target_compile_options(flowcore_intelligence PRIVATE -Wall -Wextra -Wpedantic -Werror)
install(TARGETS flowcore_intelligence LIBRARY DESTINATION "${CMAKE_INSTALL_BINDIR}")
add_test(NAME intelligence_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/intelligence_test.py")
add_test(NAME performance_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/performance_test.py")

file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/flowcore-config.cmake" [=[
include(CMakeFindDependencyMacro)
find_dependency(Threads)
include("${CMAKE_CURRENT_LIST_DIR}/flowcore-targets.cmake")
]=])
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/flowcore-config.cmake" DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/flowcore")
add_library(flowcore_intent SHARED src/intent_c.cpp)
target_link_libraries(flowcore_intent PRIVATE flowcore_sdk)
target_compile_options(flowcore_intent PRIVATE -Wall -Wextra -Wpedantic -Werror)
set_target_properties(flowcore_intent PROPERTIES VERSION 2.0.0 SOVERSION 2)
if(CMAKE_SYSTEM_NAME STREQUAL "Linux")
    target_link_options(flowcore_intent PRIVATE "LINKER:--version-script=${CMAKE_CURRENT_SOURCE_DIR}/packaging/flowcore-intent.map")
    set_property(TARGET flowcore_intent APPEND PROPERTY LINK_DEPENDS
        "${CMAKE_CURRENT_SOURCE_DIR}/packaging/flowcore-intent.map")
endif()
install(TARGETS flowcore_intent LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}")
install(FILES include/flowcore/intent_c.h DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/flowcore")
add_executable(flowcore-intent-v2-demo examples/intent_v2_demo.cpp)
target_link_libraries(flowcore-intent-v2-demo PRIVATE flowcore_sdk)
add_executable(intent_v2_native tests/intent_v2_native.cpp)
target_link_libraries(intent_v2_native PRIVATE flowcore_sdk)
add_executable(flowcore-intent-c-demo examples/intent_c_demo.c)
target_include_directories(flowcore-intent-c-demo PRIVATE include)
target_link_libraries(flowcore-intent-c-demo PRIVATE flowcore_intent)
add_executable(flowcore-intent-c-udp-demo examples/intent_c_udp_demo.c)
target_include_directories(flowcore-intent-c-udp-demo PRIVATE include)
target_link_libraries(flowcore-intent-c-udp-demo PRIVATE flowcore_intent)
add_executable(intent_old_surface_compat tests/intent_old_surface_compat.c)
target_include_directories(intent_old_surface_compat PRIVATE include)
target_link_libraries(intent_old_surface_compat PRIVATE flowcore_intent)
add_executable(intent_c_api_unit tests/intent_c_api_unit.cpp)
target_include_directories(intent_c_api_unit PRIVATE include)
target_link_libraries(intent_c_api_unit PRIVATE flowcore_intent)
target_compile_options(intent_c_api_unit PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_test(NAME intent_c_api_unit COMMAND intent_c_api_unit)
add_executable(intent_c_abi_layout tests/intent_c_abi_layout.c)
target_include_directories(intent_c_abi_layout PRIVATE include)
target_compile_features(intent_c_abi_layout PRIVATE c_std_11)
target_compile_options(intent_c_abi_layout PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_test(NAME intent_c_abi_layout COMMAND intent_c_abi_layout)
add_test(NAME intent_abi_symbols COMMAND /usr/bin/python3 -B
    "${CMAKE_CURRENT_SOURCE_DIR}/packaging/check-abi.py"
    --library $<TARGET_FILE:flowcore_intent>
    --baseline "${CMAKE_CURRENT_SOURCE_DIR}/packaging/abi/libflowcore_intent-v2.symbols"
    --report "${CMAKE_CURRENT_BINARY_DIR}/intent-abi-report.json")
add_executable(intent_protocol_compat tests/intent_protocol_compat.cpp)
target_include_directories(intent_protocol_compat PRIVATE include)
target_link_libraries(intent_protocol_compat PRIVATE flowcore_sdk)
target_compile_options(intent_protocol_compat PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_test(NAME intent_protocol_compat COMMAND /usr/bin/python3 -B
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/intent_protocol_compat.py" $<TARGET_FILE:intent_protocol_compat>)
set_tests_properties(intent_protocol_compat PROPERTIES TIMEOUT 15)
add_test(NAME intent_v2_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/intent_v2_test.py")
add_test(NAME intent_v2_live COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/intent_v2_test.py" --live)
set_tests_properties(intent_v2_live PROPERTIES TIMEOUT 30 ENVIRONMENT "FLOWCORE_INTENT_BUILD=${CMAKE_CURRENT_BINARY_DIR}")

add_test(NAME flow_engine_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/flow_engine_test.py")

add_executable(intent_session_native tests/intent_session_native.cpp)
target_link_libraries(intent_session_native PRIVATE flowcore_sdk)
target_compile_options(intent_session_native PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_test(NAME intent_session_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/intent_session_test.py")

add_executable(intent_parser_test tests/intent_parser.cpp)
target_include_directories(intent_parser_test PRIVATE src)
target_link_libraries(intent_parser_test PRIVATE flowcore_sdk)
target_compile_options(intent_parser_test PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_test(NAME intent_parser COMMAND intent_parser_test)

add_test(NAME predictive_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/predictive_test.py")

add_test(NAME workload_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/workload_test.py")

install(FILES WORKLOAD_NETWORKING.md CONTAINER_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")

option(FLOWCORE_SANITIZE "Enable AddressSanitizer and UndefinedBehaviorSanitizer for native telemetry and SDK test paths" OFF)
add_library(flowcore_telemetry SHARED src/telemetry.cpp)
target_include_directories(flowcore_telemetry PUBLIC
    $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>)
target_compile_features(flowcore_telemetry PUBLIC cxx_std_23)
target_compile_options(flowcore_telemetry PRIVATE -Wall -Wextra -Wpedantic -Werror)
set_target_properties(flowcore_telemetry PROPERTIES VERSION 1.0.0 SOVERSION 1)
set_target_properties(flowcore_telemetry PROPERTIES EXPORT_NAME telemetry)
install(TARGETS flowcore_telemetry EXPORT FlowcoreTargets LIBRARY DESTINATION "${CMAKE_INSTALL_BINDIR}")
install(FILES include/flowcore/telemetry.hpp DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/flowcore")
add_executable(telemetry_native_test tests/telemetry_native.cpp)
target_link_libraries(telemetry_native_test PRIVATE flowcore_telemetry Threads::Threads)
if(FLOWCORE_SANITIZE)
    foreach(target flowcore_telemetry telemetry_native_test)
        target_compile_options(${target} PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
        target_link_options(${target} PRIVATE -fsanitize=address,undefined)
    endforeach()
endif()
add_test(NAME telemetry_native COMMAND telemetry_native_test)
add_test(NAME telemetry_python_compat COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/native_telemetry_test.py")
set_tests_properties(telemetry_python_compat PROPERTIES ENVIRONMENT "FLOWCORE_HISTOGRAM_BACKEND=python")
add_test(NAME telemetry_native_compat COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/native_telemetry_test.py")
set_tests_properties(telemetry_native_compat PROPERTIES ENVIRONMENT "FLOWCORE_HISTOGRAM_BACKEND=native")
install(FILES NATIVE_CORE.md MIGRATION_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")
add_test(NAME transport_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/transport_test.py")
add_executable(transport_native_test tests/transport_native.cpp)
target_link_libraries(transport_native_test PRIVATE flowcore_sdk)
add_test(NAME transport_native COMMAND transport_native_test)
install(FILES TRANSPORT_AWARE.md QUIC_INTEGRATION.md TRANSPORT_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES REAL_APPLICATIONS.md GRPC_INTEGRATION.md DATABASE_INTEGRATION.md APPLICATION_BENCHMARKS.md
    REAL_APPLICATIONS_V2.md SERVICE_STACK.md APPLICATION_PERFORMANCE_RESULTS.md
    DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES SDK_V2.md PUBLIC_API_CONTRACT.md SDK_PERFORMANCE.md DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES UDP_EFFICIENCY.md MPTCP_V2.md TRANSPORT_VALIDATION_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES PACKAGING_V2.md API_COMPATIBILITY.md SUPPLY_CHAIN.md RELEASE_PROVENANCE.md
    DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES examples/intent_v2_demo.cpp examples/intent_c_demo.c examples/intent_c_udp_demo.c
              examples/intent_rust_demo.rs examples/intent_go_demo.go DESTINATION "${FLOWCORE_DATA}/examples/intent-sdk")
install(FILES config/real-app-v2-fedora45.txt
    DESTINATION "${FLOWCORE_DATA}/config")
install(DIRECTORY examples/real_apps DESTINATION "${FLOWCORE_DATA}/examples"
    PATTERN "__pycache__" EXCLUDE PATTERN "*.pyc" EXCLUDE)
add_test(NAME real_apps_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/real_apps_test.py")


file(RELATIVE_PATH FLOWCORE_INTENT_FROM_DATA "${CMAKE_INSTALL_FULL_DATADIR}/flowcore" "${CMAKE_INSTALL_FULL_LIBDIR}/libflowcore_intent.so")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/intent-library" "${FLOWCORE_INTENT_FROM_DATA}
")
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/intent-library" DESTINATION "${FLOWCORE_DATA}")
install(FILES config/quic-requirements.txt DESTINATION "${FLOWCORE_DATA}/config")
install(FILES examples/python/flowcore_intent.py examples/python/quic_adapter.py DESTINATION "${FLOWCORE_DATA}/examples/python")

install(FILES packaging/flowcore-monitor.service DESTINATION lib/systemd/system)
add_test(NAME installed_security COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/installed_security_test.py")
add_test(NAME deployment_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/deployment_test.py")
install(FILES DEPLOYMENT.md OPERATIONS.md RECOVERY.md RELEASE_VALIDATION.md DESTINATION "${FLOWCORE_DATA}/docs")

add_executable(sdk_lifetime_test tests/sdk_lifetime.cpp)
target_link_libraries(sdk_lifetime_test PRIVATE flowcore_sdk)
add_test(NAME sdk_lifetime COMMAND sdk_lifetime_test)
if(FLOWCORE_SANITIZE)
    target_compile_options(flowcore_sdk PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
    target_link_options(flowcore_sdk INTERFACE -fsanitize=address,undefined)
    foreach(target sdk_lifetime_test intent_parser_test)
        target_compile_options(${target} PRIVATE -fsanitize=address,undefined -fno-omit-frame-pointer)
    endforeach()
endif()

add_test(NAME release_gate COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/release_gate_test.py")
add_test(NAME documentation_links COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/packaging/check-docs.py")
install(FILES API.md FEATURE_MATRIX.md RELEASE_READINESS.md BENCHMARK_REPORT.md
    OSS_ROADMAP.md REVIEW_FINDINGS.md CONTRIBUTING.md CHANGELOG.md
    DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME workload_legacy COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/workload_legacy_test.py")
set_tests_properties(workload_legacy PROPERTIES TIMEOUT 15)

add_test(NAME bottleneck_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/bottleneck_test.py")
install(FILES LICENSE LICENSE.GPL-2.0 DESTINATION "${FLOWCORE_DATA}/docs")

install(FILES BOTTLENECK_ENGINE.md DIRECTIONAL_QOS.md BOTTLENECK_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES ADAPTIVE_ACTION_ENGINE.md DECISION_TRACE.md ACTION_VALIDATION_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES INSTALLED_RUNTIME.md SELINUX_VALIDATION.md PRIVILEGED_HELPER_SECURITY.md DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES OVERLOAD_ENGINE.md REQUEST_FAILURE_DIAGNOSTICS.md OVERLOAD_REMEDIATION_RESULTS.md
        DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME causal_diagnostics COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/causal_diagnostics_test.py")

install(FILES CAUSAL_DIAGNOSTICS.md INCIDENT_ENGINE.md DIAGNOSTIC_VALIDATION.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME optimizer_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/optimizer_test.py")
set_tests_properties(optimizer_unit PROPERTIES ENVIRONMENT "FLOWCORE_TEST_OPTIMIZER=$<TARGET_FILE:flowcore_intelligence>")

install(FILES POLICY_OPTIMIZER.md OPTIMIZER_TESTS.md OPTIMIZATION_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")

install(FILES RUNTIME_SAFETY.md RECOVERY_ENGINE.md FAULT_INJECTION_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")
install(FILES FLOWCORE_1_0_READINESS.md INDEPENDENT_VALIDATION.md PERFORMANCE_EVIDENCE.md
    KNOWN_LIMITATIONS.md NEXT_RESEARCH_ROADMAP.md DESTINATION "${FLOWCORE_DATA}/docs")
add_test(NAME safety_model COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/safety_model_test.py")

install(FILES SAFETY_INVARIANTS.md STATE_MACHINE_SPEC.md MODEL_TEST_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME autotune_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/autotune_test.py")
install(FILES AUTOTUNING.md OPTIMIZATION_EXPERIMENTS.md AUTOTUNING_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME multi_host_lab_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/multi_host_test.py")
install(FILES MULTI_HOST_LAB.md HARDWARE_VALIDATION.md EXTERNAL_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME fabric_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/fabric_test.py")
install(FILES FABRIC_ARCHITECTURE.md DISTRIBUTED_PROTOCOL.md FABRIC_SECURITY.md FABRIC_BENCHMARKS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME fleet_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/fleet_test.py")
install(FILES FLEET_MANAGEMENT.md POLICY_ROLLOUT.md FLEET_SECURITY.md ROLLOUT_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME dashboard_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/dashboard_test.py")
install(FILES DASHBOARD.md MONITORING_API.md UI_ARCHITECTURE.md DESTINATION "${FLOWCORE_DATA}/docs")
install(DIRECTORY monitoring/dashboard DESTINATION "${FLOWCORE_DATA}/monitoring")
install(FILES monitoring/dashboard-nginx.conf DESTINATION "${FLOWCORE_DATA}/monitoring")
install(FILES packaging/flowcore-dashboard.service DESTINATION lib/systemd/system)
install(FILES packaging/flowcore-dashboard.conf DESTINATION lib/sysusers.d)

option(FLOWCORE_FLOWID "Build the experimental FLOWID C++ SDK" ON)
if(FLOWCORE_FLOWID)
    find_package(OpenSSL 3 QUIET)
    if(OpenSSL_FOUND)
        add_library(flowcore_flowid SHARED src/flowid.cpp)
        target_link_libraries(flowcore_flowid PRIVATE OpenSSL::SSL OpenSSL::Crypto Threads::Threads)
        target_include_directories(flowcore_flowid PUBLIC
            $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
            $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>)
        target_compile_features(flowcore_flowid PUBLIC cxx_std_23)
        target_compile_options(flowcore_flowid PRIVATE -Wall -Wextra -Wpedantic -Werror)
        set_target_properties(flowcore_flowid PROPERTIES EXPORT_NAME flowid)
        add_library(flowcore::flowid ALIAS flowcore_flowid)
        add_executable(flowid-client examples/flowid_client.cpp)
        target_link_libraries(flowid-client PRIVATE flowcore_flowid flowcore_sdk)
        target_compile_options(flowid-client PRIVATE -Wall -Wextra -Wpedantic -Werror)
        set_target_properties(flowid-client PROPERTIES INSTALL_RPATH "$ORIGIN/../${CMAKE_INSTALL_LIBDIR}")
        install(TARGETS flowcore_flowid EXPORT FlowcoreTargets LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}")
        install(TARGETS flowid-client RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
        install(FILES include/flowid/session.hpp DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/flowid")
        set(FLOWID_TEST_CLIENT --client "$<TARGET_FILE:flowid-client>")
    else()
        message(WARNING "OpenSSL development files unavailable; FLOWID C++ SDK omitted")
    endif()
endif()

add_test(NAME flowid_session COMMAND /usr/bin/python3 -B
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/flowid_test.py" ${FLOWID_TEST_CLIENT})
set_tests_properties(flowid_session PROPERTIES TIMEOUT 45)
install(FILES FLOWID_PROTOCOL.md FLOWID_SECURITY.md FLOWID_EXPERIMENTS.md FLOWID_RESULTS.md
    DESTINATION "${FLOWCORE_DATA}/docs")


add_library(flowcore_udp_intent_lab SHARED tests/udp_intent_lab_bridge.cpp)
target_link_libraries(flowcore_udp_intent_lab PRIVATE flowcore_sdk)
target_compile_options(flowcore_udp_intent_lab PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_executable(udp_intent_native_test tests/udp_intent_native.cpp)
target_link_libraries(udp_intent_native_test PRIVATE flowcore_sdk)
target_compile_options(udp_intent_native_test PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_executable(flowcore-udp-intent-demo examples/udp_intent_demo.cpp)
target_link_libraries(flowcore-udp-intent-demo PRIVATE flowcore_sdk)
target_compile_options(flowcore-udp-intent-demo PRIVATE -Wall -Wextra -Wpedantic -Werror)
install(TARGETS flowcore-udp-intent-demo RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
add_executable(udp_ancillary_kernel tests/udp_ancillary_kernel.cpp)
target_compile_options(udp_ancillary_kernel PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_test(NAME udp_ancillary_kernel COMMAND udp_ancillary_kernel)
add_test(NAME udp_intent_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/udp_intent_test.py")
add_test(NAME udp_intent_live COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/udp_intent_test.py" --live)
set_tests_properties(udp_intent_live PROPERTIES SKIP_RETURN_CODE 77)
set_tests_properties(udp_intent_unit udp_intent_live PROPERTIES TIMEOUT 40
    ENVIRONMENT "FLOWCORE_UDP_INTENT_BUILD=${CMAKE_CURRENT_BINARY_DIR}")

add_test(NAME scheduler_research COMMAND /usr/bin/python3 -B
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/scheduler_experiment_test.py")
set_tests_properties(scheduler_research PROPERTIES TIMEOUT 15)
install(FILES KERNEL_SCHEDULER_DESIGN.md SCHEDULER_PROTOTYPE.md SCHEDULER_BENCHMARKS.md
    KERNEL_UPSTREAM_PLAN.md PER_MESSAGE_INTENTS.md UDP_INTENT_SDK.md MESSAGE_SCHEDULING_RESULTS.md
    DESTINATION "${FLOWCORE_DATA}/docs")

add_library(flowcore_pacing_lab SHARED src/pacing_lab_c.cpp src/hybrid.cpp)
target_link_libraries(flowcore_pacing_lab PRIVATE flowcore_sdk)
target_compile_options(flowcore_pacing_lab PRIVATE -Wall -Wextra -Wpedantic -Werror)
install(TARGETS flowcore_pacing_lab LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}")
file(RELATIVE_PATH FLOWCORE_LIB_FROM_DATA "${CMAKE_INSTALL_FULL_DATADIR}/flowcore" "${CMAKE_INSTALL_FULL_LIBDIR}")
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/installed-lib-layout" "${FLOWCORE_LIB_FROM_DATA}\n")
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/installed-lib-layout" DESTINATION "${FLOWCORE_DATA}")

add_executable(socket_pacing_test tests/socket_pacing_test.cpp)
target_link_libraries(socket_pacing_test PRIVATE flowcore_sdk)
target_compile_options(socket_pacing_test PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_test(NAME socket_pacing_native COMMAND socket_pacing_test)
set_tests_properties(socket_pacing_native PROPERTIES TIMEOUT 15)
install(FILES TCP_PACING_ENGINE.md SOCKET_INTELLIGENCE.md TCP_PACING_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME ecn_feedback COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/ecn_test.py")
set_tests_properties(ecn_feedback PROPERTIES TIMEOUT 15
    ENVIRONMENT "FLOWCORE_ECN_PACING_BRIDGE=$<TARGET_FILE:flowcore_pacing_lab>")
install(FILES ECN_ENGINE.md CONGESTION_FEEDBACK.md ECN_BENCHMARKS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_executable(deadline_test tests/deadline_test.cpp)
target_link_libraries(deadline_test PRIVATE flowcore_sdk)
target_compile_options(deadline_test PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_test(NAME deadline_native COMMAND deadline_test)
set_tests_properties(deadline_native PROPERTIES TIMEOUT 15)
install(FILES DEADLINE_ENGINE.md ADMISSION_CONTROL.md DEADLINE_EXPERIMENTS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME cooperative_slo COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/cooperative_slo_test.py")
set_tests_properties(cooperative_slo PROPERTIES TIMEOUT 20)
install(FILES include/flowcore/cooperative_slo.hpp DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/flowcore")
install(FILES DISTRIBUTED_SLO.md REQUEST_COORDINATION.md COOPERATIVE_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME strategy_engine COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/strategy_test.py")
set_tests_properties(strategy_engine PROPERTIES TIMEOUT 20)
install(FILES STRATEGY_ENGINE.md ONLINE_EXPERIMENTATION.md STRATEGY_RESULTS.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME nextgen_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/nextgen_test.py")
install(FILES NEXTGEN_ARCHITECTURE.md INTEGRATED_ENGINE.md NEXTGEN_BENCHMARKS.md INNOVATION_REPORT.md
    DESTINATION "${FLOWCORE_DATA}/docs")

install(FILES PACING_ROOT_CAUSE_V2.md PACING_REMEDIATION.md PACING_REGRESSION_RESULTS.md
    DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME hybrid_v3_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/hybrid_test.py")
set_tests_properties(hybrid_v3_unit PROPERTIES
    ENVIRONMENT "FLOWCORE_HYBRID_TEST_BUILD=$<TARGET_FILE_DIR:flowcore_pacing_lab>")
install(FILES HYBRID_CONTROLLER_V3.md ACTUATOR_COORDINATION.md HYBRID_V3_RESULTS.md
    DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME slo_optimization_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/slo_test.py")
set_tests_properties(slo_optimization_unit PROPERTIES TIMEOUT 15
    ENVIRONMENT "FLOWCORE_SLO_TEST_BUILD=$<TARGET_FILE_DIR:flowcore_intelligence>")
install(FILES MULTIOBJECTIVE_OPTIMIZATION.md PARETO_ANALYSIS.md SLO_OPTIMIZATION_RESULTS.md
    DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME supply_chain_unit COMMAND /usr/bin/python3 -B
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/supply_chain_test.py")
add_test(NAME evidence_bundle COMMAND /usr/bin/python3 -B
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/evidence_bundle_test.py")
set_tests_properties(evidence_bundle PROPERTIES TIMEOUT 40)
install(FILES EVIDENCE_FORMAT.md REPRODUCIBILITY_GUIDE.md PUBLIC_BUNDLE_REVIEW.md
    DESTINATION "${FLOWCORE_DATA}/docs")
install(PROGRAMS packaging/evidence_bundle.py
    DESTINATION "${CMAKE_INSTALL_LIBEXECDIR}/flowcore")
install(FILES packaging/evidence-release-metadata.json
    DESTINATION "${FLOWCORE_DATA}/evidence")
