cmake_minimum_required(VERSION 3.20)
project(flowcore VERSION 0.2.0 LANGUAGES CXX)
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
    FLOWCORE_INSTALLED_CLI_REL="${FLOWCORE_DATA_FROM_BIN}/scripts/cli.py" FLOWCORE_VERSION="${PROJECT_VERSION}")
configure_file(scripts/flowcored.in flowcored @ONLY)
file(CHMOD "${CMAKE_CURRENT_BINARY_DIR}/flowcored"
     PERMISSIONS OWNER_READ OWNER_WRITE OWNER_EXECUTE GROUP_READ GROUP_EXECUTE WORLD_READ WORLD_EXECUTE)
configure_file(scripts/flowcored-installed.in flowcored-installed @ONLY)
install(TARGETS flowcore RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
install(PROGRAMS "${CMAKE_CURRENT_BINARY_DIR}/flowcored-installed" DESTINATION "${CMAKE_INSTALL_BINDIR}" RENAME flowcored)
install(DIRECTORY scripts config scenarios DESTINATION "${FLOWCORE_DATA}"
    FILES_MATCHING PATTERN "*.py" PATTERN "*.json" PATTERN "*.conf" PATTERN "__pycache__" EXCLUDE)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/installed-layout" "${CMAKE_CURRENT_BINARY_DIR}/VERSION"
              "${CMAKE_CURRENT_BINARY_DIR}/bpf-enabled" DESTINATION "${FLOWCORE_DATA}")
install(FILES config/service.json config/helper.json DESTINATION "${CMAKE_INSTALL_SYSCONFDIR}/flowcore")
install(FILES packaging/flowcored.service packaging/flowcore-network.service
              packaging/flowcore-network.socket DESTINATION lib/systemd/system)
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
    else()
        message(STATUS "CO-RE build prerequisites absent, INET_DIAG fallback available")
    endif()
endif()

add_test(NAME observer_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/observer_test.py")

add_library(flowcore_sdk src/client.cpp)
target_include_directories(flowcore_sdk PUBLIC $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
    $<INSTALL_INTERFACE:${CMAKE_INSTALL_INCLUDEDIR}>)
target_compile_features(flowcore_sdk PUBLIC cxx_std_23)
set_target_properties(flowcore_sdk PROPERTIES EXPORT_NAME sdk)
add_library(flowcore::sdk ALIAS flowcore_sdk)
target_compile_options(flowcore_sdk PRIVATE -Wall -Wextra -Wpedantic -Werror)
add_executable(flowcore-sdk-request examples/sdk_request.cpp)
target_link_libraries(flowcore-sdk-request PRIVATE flowcore_sdk)
install(TARGETS flowcore_sdk EXPORT FlowcoreTargets ARCHIVE DESTINATION "${CMAKE_INSTALL_LIBDIR}")
install(EXPORT FlowcoreTargets FILE flowcore-config.cmake NAMESPACE flowcore:: DESTINATION "${CMAKE_INSTALL_LIBDIR}/cmake/flowcore")
install(FILES include/flowcore/client.hpp DESTINATION "${CMAKE_INSTALL_INCLUDEDIR}/flowcore")

add_test(NAME adaptive_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/adaptive_unit.py")

add_test(NAME research_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/research_test.py")
add_executable(flowcore-container-request examples/container_request.cpp)
target_link_libraries(flowcore-container-request PRIVATE flowcore_sdk)

install(TARGETS flowcore-sdk-request flowcore-container-request RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
if(TARGET flowcore-bpf)
    install(TARGETS flowcore-bpf RUNTIME DESTINATION "${CMAKE_INSTALL_BINDIR}")
    install(FILES "${CMAKE_CURRENT_BINARY_DIR}/bpf/observe.bpf.o" DESTINATION "${FLOWCORE_DATA}/bpf")
endif()

add_test(NAME platform_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/platform_test.py")
install(FILES monitoring/flowcore-grafana.json DESTINATION "${FLOWCORE_DATA}/monitoring")
install(FILES README.md ARCHITECTURE.md PLATFORM.md SECURITY.md CLI.md RELEASE.md RESEARCH.md BENCHMARK_V2.md
    DESTINATION "${FLOWCORE_DATA}/docs")

install(FILES PLATFORM_RESULTS.md AUDIT.md AUDIT_FIXES.md REMAINING_RISKS.md
              THREAT_MODEL.md PRIVILEGES.md DESTINATION "${FLOWCORE_DATA}/docs")

add_test(NAME helper_unit COMMAND /usr/bin/python3 -B "${CMAKE_CURRENT_SOURCE_DIR}/tests/helper_test.py")
