"""Unprivileged tests for the application adapters, bounded OTLP bridge, and HTTP demo."""
import base64
import http.client
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
APPS = ROOT / 'examples' / 'real_apps'
sys.path.insert(0, str(APPS))

from flowcore_adapter import FlowcoreAdapter
from metrics_bridge import Handler, Store
from otel_client import OtlpMetrics


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def test_otlp():
    metrics = OtlpMetrics('api-critical')
    metrics.observe('flowcore.http.request.duration', .012,
                    {'flowcore.traffic_class': 'critical'})
    metrics.add_counter('flowcore.http.requests', 1,
                        {'flowcore.traffic_class': 'critical'})
    doc = metrics.payload()
    store = Store(['api-critical'])
    answer = store.ingest(doc)
    assert answer['accepted'] == 2
    before = store.prometheus()
    assert 'flowcore_app_http_request_duration_seconds_count' in before
    assert 'flowcore_app_http_requests_total' in before
    store.ingest(doc)
    assert store.prometheus().count('flowcore_app_http_requests_total{') == 1
    assert 'flowcore_app_http_requests_total{flowcore.traffic_class="critical",service="api-critical"} 1.0' in store.prometheus()
    try:
        Store(['somebody-else']).ingest(doc)
    except PermissionError:
        pass
    else:
        raise AssertionError('unapproved service identity was accepted')
    bad = json.loads(json.dumps(doc))
    bad['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]['name'] = 'arbitrary_cardinality_metric'
    try:
        store.ingest(bad)
    except ValueError:
        pass
    else:
        raise AssertionError('nonallowlisted metric was accepted')
    malformed = json.loads(json.dumps(doc))
    malformed['resourceMetrics'][0]['resource'] = 7
    try:
        store.ingest(malformed)
    except ValueError:
        pass
    else:
        raise AssertionError('malformed resource shape was accepted')
    duplicate = json.loads(json.dumps(doc))
    attrs_list = duplicate['resourceMetrics'][0]['resource']['attributes']
    attrs_list.append(dict(attrs_list[0]))
    try:
        store.ingest(duplicate)
    except ValueError:
        pass
    else:
        raise AssertionError('duplicate resource identity was accepted')
    duplicate_series = json.loads(json.dumps(doc))
    first_metric = duplicate_series['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]
    first_metric['histogram']['dataPoints'].append(
        dict(first_metric['histogram']['dataPoints'][0]))
    try:
        store.ingest(duplicate_series)
    except ValueError:
        pass
    else:
        raise AssertionError('duplicate time series was accepted')
    trace = base64.b64encode(bytes(range(16))).decode()
    exemplar_doc = json.loads(json.dumps(doc))
    histogram = exemplar_doc['resourceMetrics'][0]['scopeMetrics'][0]['metrics'][0]['histogram']
    histogram['dataPoints'][0]['exemplars'] = [{'traceId': trace, 'spanId': base64.b64encode(b'12345678').decode()}]
    restricted = Store(['api-critical'], allow_trace_ids=True)
    restricted.ingest(exemplar_doc, trace_authorized=False)
    assert not restricted.trace_snapshot()
    restricted.ingest(exemplar_doc, trace_authorized=True)
    assert restricted.trace_snapshot()[0]['trace_id'] == bytes(range(16)).hex()


def test_adapter_fallback():
    adapter = FlowcoreAdapter('api-critical', enabled=False)
    assert adapter.status()['enabled'] is False
    with adapter.request(deadline_ms=10) as request:
        assert request is None
    adapter.close()


def test_http_service():
    with tempfile.TemporaryDirectory(prefix='flowcore-real-app-') as directory:
        root = Path(directory)
        (root / 'backup.bin').write_bytes(b'backup-fixture' * 4096)
        (root / 'secret.bin').symlink_to(root / 'backup.bin')
        ports = [free_port() for _ in range(3)]
        while len(set(ports)) != 3:
            ports = [free_port() for _ in range(3)]
        process = subprocess.Popen([sys.executable, str(APPS / 'http_service.py'),
            '--bind', '127.0.0.1', '--critical-port', str(ports[0]),
            '--background-port', str(ports[1]), '--database-port', str(ports[2]),
            '--artifact-dir', directory, '--max-workers', '4'],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            connection = None
            while time.monotonic() < deadline:
                try:
                    connection = http.client.HTTPConnection('127.0.0.1', ports[0], timeout=1)
                    connection.request('GET', '/health')
                    response = connection.getresponse()
                    if response.status == 200:
                        response.read()
                        break
                except OSError:
                    time.sleep(.03)
                finally:
                    if connection:
                        connection.close()
            assert connection is not None and process.poll() is None
            connection = http.client.HTTPConnection('127.0.0.1', ports[0], timeout=2)
            connection.request('GET', '/api/critical?work_ms=0')
            response = connection.getresponse()
            assert response.status == 200 and json.loads(response.read())['ok']
            connection.close()
            for path, expected in (('/artifact/backup.bin', 14 * 4096),
                                   ('/transfer?bytes=131072', 131072)):
                connection = http.client.HTTPConnection('127.0.0.1', ports[1], timeout=2)
                connection.request('GET', path)
                response = connection.getresponse()
                assert response.status == 200
                count = 0
                while chunk := response.read(8192):
                    count += len(chunk)
                assert count == expected
                connection.close()
            for path in ('/artifact/%2e%2e', '/artifact/secret.bin'):
                connection = http.client.HTTPConnection('127.0.0.1', ports[1], timeout=2)
                connection.request('GET', path)
                response = connection.getresponse()
                assert response.status != 200
                response.read()
                connection.close()
            connection = http.client.HTTPConnection('127.0.0.1', ports[1], timeout=2)
            connection.request('GET', '/transfer?bytes=999999999')
            response = connection.getresponse()
            assert response.status == 400
            response.read()
            connection.close()
            upload = root / 'upload.bin'
            upload.write_bytes(b'backup-payload' * 4096)
            output = root / 'client.json'
            subprocess.run([sys.executable, str(APPS / 'http_benchmark.py'),
                '--host', '127.0.0.1', '--port', str(ports[0]),
                '--background-port', str(ports[1]), '--rate', '2', '--duration', '1',
                '--concurrency', '1', '--deadline-ms', '500', '--work-ms', '0',
                '--background-bytes', str(upload.stat().st_size),
                '--background-upload-file', str(upload), '--no-flowcore',
                '--output', str(output)], check=True, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, timeout=10)
            client = json.loads(output.read_text())
            assert client['background_transfer']['direction'] == 'upload'
            assert client['background_transfer']['bytes_received'] == upload.stat().st_size
            assert client['background_transfer']['error'] is None
        finally:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            if process.stderr:
                process.stderr.close()


def test_grpc_example():
    try:
        import grpc  # noqa: F401
        import grpc_tools  # noqa: F401
    except ImportError:
        print('gRPC example test skipped: optional dependencies unavailable')
        return
    with tempfile.TemporaryDirectory(prefix='flowcore-grpc-test-') as directory:
        generated = Path(directory) / 'generated'
        generated.mkdir()
        subprocess.run([sys.executable, '-m', 'grpc_tools.protoc',
                        '-I', str(APPS), '--python_out', str(generated),
                        '--grpc_python_out', str(generated),
                        str(APPS / 'grpc_app.proto')], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        port = free_port()
        env = dict(os.environ, FLOWCORE_GRPC_PROTO_DIR=str(generated))
        server = subprocess.Popen([sys.executable, str(APPS / 'grpc_server.py'),
            '--bind', f'127.0.0.1:{port}', '--workers', '2'],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            output = Path(directory) / 'grpc.json'
            subprocess.run([sys.executable, str(APPS / 'grpc_benchmark.py'),
                '--target', f'127.0.0.1:{port}', '--rate', '2', '--duration', '1',
                '--concurrency', '1', '--deadline-ms', '250', '--work-ms', '1',
                '--no-flowcore', '--output', str(output)], env=env, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
            result = json.loads(output.read_text())
            assert result['summary']['attempted'] == 2
            assert result['summary']['completed'] == 2
            assert result['transport']['connection_reuse'] is True
            assert result['transport']['socket_cookie'].startswith('unavailable')
            timeout_output = Path(directory) / 'grpc-timeout.json'
            subprocess.run([sys.executable, str(APPS / 'grpc_benchmark.py'),
                '--target', f'127.0.0.1:{port}', '--rate', '1', '--duration', '1',
                '--concurrency', '1', '--deadline-ms', '5', '--work-ms', '100',
                '--no-flowcore', '--output', str(timeout_output)], env=env, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=15)
            timed = json.loads(timeout_output.read_text())
            assert timed['summary']['attempted'] == 1
            assert timed['summary']['timeouts'] == 1
            assert timed['requests'][0]['grpc_status'] == 'DEADLINE_EXCEEDED'
        finally:
            server.terminate()
            try:
                server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=3)
            if server.stderr:
                server.stderr.close()


def test_receiver_token_fd():
    port = free_port()
    read_fd, write_fd = os.pipe()
    token = 't' * 40
    process = subprocess.Popen([sys.executable, str(APPS / 'metrics_bridge.py'),
        '--bind', '127.0.0.1', '--port', str(port), '--token-fd', str(read_fd)],
        pass_fds=(read_fd,), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    os.close(read_fd)
    os.write(write_fd, token.encode('ascii'))
    os.close(write_fd)
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and process.poll() is None:
            try:
                connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.5)
                connection.request('GET', '/metrics', headers={'Authorization': 'Bearer ' + token})
                response = connection.getresponse()
                assert response.status == 200
                response.read()
                connection.close()
                return
            except OSError:
                time.sleep(.03)
        raise AssertionError('metrics bridge did not start with the inherited token descriptor')
    finally:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)
        if process.stderr:
            process.stderr.close()


def test_receiver_http():
    store = Store(['api-critical'])
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.store, server.token = store, 'test-token'
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        metrics = OtlpMetrics('api-critical')
        metrics.add_counter('flowcore.http.requests', 3)
        body = json.dumps(metrics.payload()).encode()
        connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
        connection.request('POST', '/v1/metrics', body,
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == 401
        response.read()
        connection.request('POST', '/v1/metrics', body,
                           {'Content-Type': 'application/json',
                            'Authorization': 'Bearer test-token'})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        for malformed in (b'[]', b'{"resourceMetrics":[{"resource":7,"scopeMetrics":[]}]}'):
            connection.request('POST', '/v1/metrics', malformed,
                               {'Content-Type': 'application/json',
                                'Authorization': 'Bearer test-token'})
            response = connection.getresponse()
            assert response.status == 400
            response.read()
        connection.request('GET', '/metrics', headers={'Authorization': 'Bearer test-token'})
        response = connection.getresponse()
        assert response.status == 200
        assert 'flowcore_app_http_requests_total' in response.read().decode()
        connection.close()
    finally:
        server.shutdown()
        server.server_close()


def main():
    test_otlp()
    test_adapter_fallback()
    test_http_service()
    test_grpc_example()
    test_receiver_token_fd()
    test_receiver_http()
    print('real-app unit tests passed')


if __name__ == '__main__':
    main()
