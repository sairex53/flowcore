from paths import BINDIR, RESULTS
#!/usr/bin/env python3
"""Exercise the real C++ controller, tc and rtnetlink in disposable namespaces."""
import json
import os
import select
import subprocess
import time
from benchmark import Lab, ROOT, owned_directory


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def main():
    if os.geteuid() != 0:
        raise SystemExit('run with sudo')
    output = RESULTS / f'integration-{time.time_ns()}'
    owned_directory(output)
    lab = Lab(output)
    try:
        lab.start()
        lab.configure('flowcore')
        rejected = subprocess.run([BINDIR/'flowcore', '--config', ROOT/'config/lab.conf',
                                   '--lab-netns', '/proc/self/ns/net'], capture_output=True, text=True, timeout=3)
        check(rejected.returncode == 1 and 'owned lab namespace' in rejected.stderr,
              'legacy controller accepted the host namespace')
        controller = lab.spawn_legacy_controller(stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        telemetry = []
        pending = bytearray()
        def event():
            deadline = time.monotonic() + 5
            while b'\n' not in pending:
                wait = deadline - time.monotonic()
                if wait <= 0 or not select.select([controller.stdout], [], [], wait)[0]:
                    raise RuntimeError('controller telemetry timed out')
                block = os.read(controller.stdout.fileno(), 4096)
                if not block:
                    raise RuntimeError('controller closed telemetry')
                pending.extend(block)
            line, _, rest = pending.partition(b'\n')
            pending[:] = rest
            result = json.loads(line)
            telemetry.append(result)
            return result
        def feed(value):
            controller.stdin.write(value.encode())
            controller.stdin.flush()
            return event()
        def rates():
            classes = json.loads(lab.tc('-j', 'class', 'show', 'dev', 'fc0'))
            return {row['handle']: row for row in classes}
        check(event()['event'] == 'start', 'missing start')
        check(feed('30 100\n')['state'] == 'balanced', 'triggered too early')
        check(feed('30 100\n')['state'] == 'protected', 'failed to enter protected')
        protected = rates()
        check(protected['1:10']['rate'] == 1000000, 'critical reservation was not changed to 8 Mbit/s')
        check(protected['1:20']['rate'] == 1500000, 'background reservation was not changed to 12 Mbit/s')
        check(event()['event'] == 'stale', 'lost feedback did not restore balanced policy')
        check(rates()['1:10']['rate'] == 500000, 'stale telemetry rollback failed')
        feed('30 100\n')
        check(feed('30 100\n')['state'] == 'protected', 're-entry failed')
        for _ in range(3):
            check(feed('5 100\n')['state'] == 'protected', 'recovery triggered too early')
        check(feed('5 100\n')['state'] == 'balanced', 'recovery did not happen')
        feed('30 100\n')
        feed('30 100\n')
        controller.stdin.write(b'garbage\n')
        controller.stdin.flush()
        controller.stdin.close()
        check(controller.wait(timeout=5) == 1, 'malformed input not rejected')
        check(rates()['1:10']['rate'] == 500000, 'error rollback failed')
        controller = lab.spawn_legacy_controller(stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        check(event()['event'] == 'start', 'missing restart')
        feed('30 100\n')
        check(feed('30 100\n')['state'] == 'protected', 'restart did not protect')
        controller.stdout.close()
        controller.stdin.write(b'30 100\n')
        controller.stdin.flush()
        controller.stdin.close()
        check(controller.wait(timeout=5) == 1, 'closed telemetry pipe skipped error handling')
        check(rates()['1:10']['rate'] == 500000, 'closed telemetry pipe skipped rollback')
        lab.snapshot('final')
        (output / 'telemetry.json').write_text(json.dumps(telemetry, indent=2))
        print(f'PASS: transition, real class rates, stale telemetry, recovery and error rollback. {output}')
    finally:
        lab.close()


if __name__ == '__main__':
    main()
