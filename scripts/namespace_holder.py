"""Keep a lab namespace alive only while its owner process is alive."""
import ctypes
import os
import signal
import sys

parent = int(sys.argv[1])
if ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGTERM, 0, 0, 0):
    raise OSError(ctypes.get_errno(), 'PR_SET_PDEATHSIG')
if os.getppid() != parent:
    sys.exit(1)
os.execv('/usr/bin/unshare', ['unshare', '--net', '--', '/usr/bin/sleep', 'infinity'])
