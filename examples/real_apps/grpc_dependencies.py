"""Reject known-outdated dependency floors in the optional gRPC example."""
from importlib.metadata import PackageNotFoundError, version
import re

REQUIRED = {
    'grpcio': (1, 60, 0),
    'protobuf': (6, 33, 5),
}


def require_grpc(*, build_tools=False):
    required = REQUIRED | {'grpcio-tools': (1, 60, 0)} if build_tools else REQUIRED
    for package, minimum in required.items():
        try:
            parts = re.fullmatch(r'(\d+)\.(\d+)\.(\d+)', version(package))
        except PackageNotFoundError:
            parts = None
        if parts is None or tuple(map(int, parts.groups())) < minimum:
            floor = '.'.join(map(str, minimum))
            raise RuntimeError(f'{package} requires a stable version >= {floor}. Install examples/real_apps/requirements.txt.')


if __name__ == '__main__':
    try:
        require_grpc(build_tools=True)
    except RuntimeError as error:
        raise SystemExit(str(error)) from None
