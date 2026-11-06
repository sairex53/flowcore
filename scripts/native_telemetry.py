"""Versioned, optional stateless native histogram summary binding."""
import ctypes
import os
from paths import BINDIR

MODE=os.environ.get('FLOWCORE_HISTOGRAM_BACKEND','python')
if MODE not in ('python','native','auto'):
    raise ValueError('FLOWCORE_HISTOGRAM_BACKEND must be auto, python or native')
FUNCTION=None
LIBRARY=None
if MODE=='native' or (MODE=='auto' and (BINDIR/'libflowcore_telemetry.so').is_file()):
    LIBRARY=ctypes.CDLL(str(BINDIR/'libflowcore_telemetry.so'))
    LIBRARY.flowcore_telemetry_abi.restype=ctypes.c_uint
    if LIBRARY.flowcore_telemetry_abi()!=1:
        raise RuntimeError('unsupported native telemetry ABI')
    FUNCTION=LIBRARY.flowcore_histogram_summary_v1
    FUNCTION.argtypes=[ctypes.POINTER(ctypes.c_uint16),ctypes.POINTER(ctypes.c_uint64),
                       ctypes.c_size_t,ctypes.c_uint64,ctypes.POINTER(ctypes.c_uint64)]
    FUNCTION.restype=ctypes.c_int


def summary(histogram):
    if FUNCTION is None or not 0<=histogram.count<2**53:
        return None
    indices=histogram.occupied
    # Preserve arbitrary-precision Python semantics for unrepresentable counters.
    values=[histogram.counts[index] for index in indices]
    if any(not 0<=value<2**64 for value in values):
        return None
    n=len(indices)
    output=(ctypes.c_uint64*3)()
    status=FUNCTION((ctypes.c_uint16*n)(*indices),(ctypes.c_uint64*n)(*values),
                    n,histogram.count,output)
    if status:
        raise ValueError('native histogram rejected inconsistent bins')
    return [None if value==2**64-1 else value for value in output]
