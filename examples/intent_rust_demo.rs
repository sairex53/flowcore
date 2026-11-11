use std::ffi::{CStr, CString};
use std::os::raw::{c_char, c_int, c_uint, c_ulonglong};

#[repr(C)]
struct FcService { _private: [u8; 0] }
#[repr(C)]
struct FcRequest { _private: [u8; 0] }

#[repr(C)]
struct Options {
    version: u32,
    size: u32,
    service: *const c_char,
    priority: u32,
    target_us: u32,
    deadline_us: u32,
    desired_kbit: u32,
    minimum_kbit: u32,
    maximum_kbit: u32,
    ttl_seconds: u32,
    reliable: u32,
    isolated: u32,
}

#[repr(C)]
struct Capabilities {
    version: u32,
    size: u32,
    protocol_version: u32,
    reserved: u32,
    features: u64,
    udp_message_authorized: u32,
    reserved2: u32,
}

#[repr(C)]
struct Policy {
    version: u32,
    size: u32,
    state: u32,
    kernel_verified: u32,
    policy_generation: u64,
    priority: u32,
    target_latency_us: u32,
    deadline_us: u32,
    desired_kbit: u32,
    minimum_kbit: u32,
    maximum_kbit: u32,
    lifetime_seconds: u32,
    prefer_reliable: u32,
    class_rate_kbit: u32,
    class_ceil_kbit: u32,
}

#[link(name = "flowcore_intent")]
extern "C" {
    fn fc_v2_open(path: *const c_char, options: *const Options) -> *mut FcService;
    fn fc_v2_wait(service: *mut FcService, timeout_ms: c_uint) -> c_int;
    fn fc_v2_begin(service: *mut FcService, deadline_us: c_uint, cookie: c_ulonglong) -> *mut FcRequest;
    fn fc_v2_finish(request: *mut FcRequest, outcome: c_uint) -> c_int;
    fn fc_v2_get_capabilities(service: *mut FcService, out: *mut Capabilities) -> c_int;
    fn fc_v2_inspect_policy(service: *mut FcService, out: *mut Policy) -> c_int;
    fn fc_v2_close(service: *mut FcService);
    fn fc_v2_error() -> *const c_char;
}

fn last_error() -> String {
    unsafe {
        let message = fc_v2_error();
        if message.is_null() { "unknown FLOWCORE SDK error".into() }
        else { CStr::from_ptr(message).to_string_lossy().into_owned() }
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() != 3 {
        eprintln!("usage: flowcore-intent-rust-demo <control.sock> <service-name>");
        std::process::exit(2);
    }
    let path = CString::new(args[1].as_bytes()).expect("socket path contains NUL");
    let name = CString::new(args[2].as_bytes()).expect("service name contains NUL");
    let options = Options {
        version: 2,
        size: std::mem::size_of::<Options>() as u32,
        service: name.as_ptr(),
        priority: 0,
        target_us: 30_000,
        deadline_us: 0,
        desired_kbit: 0,
        minimum_kbit: 0,
        maximum_kbit: 0,
        ttl_seconds: 60,
        reliable: 0,
        isolated: 0,
    };
    unsafe {
        let service = fc_v2_open(path.as_ptr(), &options);
        if service.is_null() {
            eprintln!("{}", last_error());
            std::process::exit(1);
        }
        let online = fc_v2_wait(service, 250) != 0;
        if online {
            let mut caps = Capabilities {
                version: 1, size: std::mem::size_of::<Capabilities>() as u32,
                protocol_version: 0, reserved: 0, features: 0,
                udp_message_authorized: 0, reserved2: 0,
            };
            if fc_v2_get_capabilities(service, &mut caps) == 0 {
                eprintln!("capability query: {}", last_error());
            } else {
                println!("protocol={} feature-bits={:#x}", caps.protocol_version, caps.features);
                let mut policy: Policy = std::mem::zeroed();
                policy.version = 1;
                policy.size = std::mem::size_of::<Policy>() as u32;
                if fc_v2_inspect_policy(service, &mut policy) != 0 {
                    println!("policy-generation={} kernel-verified={}", policy.policy_generation,
                             policy.kernel_verified != 0);
                }
            }
        } else {
            eprintln!("FLOWCORE offline; continuing without network policy");
        }
        let request = fc_v2_begin(service, 0, 0);
        if !request.is_null() {
            // Return value can be false offline; that drops telemetry, not application work.
            let _ = fc_v2_finish(request, 0);
        }
        fc_v2_close(service);
    }
}
