package main

/*
#cgo CFLAGS: -I${SRCDIR}/../include
#cgo LDFLAGS: -L${SRCDIR}/../build -lflowcore_intent -Wl,-rpath,${SRCDIR}/../build
#include <stdlib.h>
#include "flowcore/intent_c.h"
*/
import "C"
import (
	"fmt"
	"os"
	"runtime"
	"unsafe"
)

func main() {
	// Keep separate fc_v2_error reads on the C call's OS thread.
	runtime.LockOSThread()
	defer runtime.UnlockOSThread()
	if len(os.Args) != 2 {
		panic("provide daemon socket path")
	}
	path := C.CString(os.Args[1])
	name := C.CString("go_demo")
	defer C.free(unsafe.Pointer(path))
	defer C.free(unsafe.Pointer(name))
	options := C.fc_v2_options{version: 2, size: C.uint32_t(C.sizeof_fc_v2_options),
		service: name, target_us: 30000, ttl_seconds: 60}
	service := C.fc_v2_open(path, &options)
	if service == nil {
		panic(C.GoString(C.fc_v2_error()))
	}
	defer C.fc_v2_close(service)
	online := C.fc_v2_wait(service, 250) != 0
	if online && C.fc_v2_subscribe_notifications(service, 1) == 0 {
		panic(C.GoString(C.fc_v2_error()))
	}
	request := C.fc_v2_begin(service, 10000, 0)
	if request != nil {
		C.fc_v2_finish(request, 0)
	}
	if online && C.fc_v2_flush(service, 3000) == 0 {
		panic("flush timed out")
	}
	var capabilities C.fc_v2_capability_snapshot
	capabilities.version = 1
	capabilities.size = C.uint32_t(C.sizeof_fc_v2_capability_snapshot)
	if online && C.fc_v2_get_capabilities(service, &capabilities) == 0 {
		panic(C.GoString(C.fc_v2_error()))
	}
	var statistics C.fc_v2_statistics
	if C.fc_v2_stats(service, &statistics) == 0 {
		panic("telemetry statistics unavailable")
	}
	if !online {
		fmt.Println("FLOWCORE offline; application continues without policy telemetry")
		return
	}
	if statistics.rejected != 0 {
		panic("telemetry event rejected")
	}
	var transport C.fc_v2_transport_statistics
	if C.fc_v2_transport_stats(service, &transport) == 0 {
		panic("transport diagnostics unavailable")
	}
	fmt.Printf("sessions=%d frames=%d generation=%d\n", uint64(transport.session_connections), uint64(transport.session_requests), uint64(C.fc_v2_generation(service)))
	fmt.Printf("accepted=%d rejected=%d dropped=%d\n", uint64(statistics.accepted), uint64(statistics.rejected), uint64(statistics.dropped))
}
