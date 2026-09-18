"""Bounded local transport for the Codex preflight. No model service is contacted."""
import hashlib
import json
import os
import resource
import selectors
import signal
import socket
import subprocess
import threading
import time

FRAME_LIMIT = 1024 * 1024
TOTAL_LIMIT = 16 * FRAME_LIMIT
DEADLINE_SECONDS = 60


class ProbeError(ValueError):
    """A public diagnostic code, never subprocess output or request text."""


def json_value(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ProbeError('duplicate_json_key')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ProbeError('invalid_json')))
    except (ValueError, RecursionError, UnicodeError) as error:
        raise ProbeError('invalid_json') from error


class Budget:
    def __init__(self, seconds=DEADLINE_SECONDS, total=TOTAL_LIMIT):
        self.deadline = time.monotonic() + seconds
        self.limit = total
        self.used = 0
        self.lock = threading.Lock()

    def remaining(self):
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise ProbeError('deadline_exceeded')
        return left

    def add(self, count):
        self.remaining()
        with self.lock:
            self.used += count
            if self.used > self.limit:
                raise ProbeError('capture_limit_exceeded')


def child_limits():
    signal.pthread_sigmask(signal.SIG_UNBLOCK, [signal.SIGINT, signal.SIGTERM])
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (TOTAL_LIMIT, TOTAL_LIMIT))


class Child:
    def __init__(self, args, cwd, env, budget):
        self.budget = budget
        self.buffer = bytearray()
        self.stderr_hash = hashlib.sha256()
        self.counts = {'stdout': 0, 'stderr': 0}
        self.proc = None
        self.selector = selectors.DefaultSelector()
        try:
            # Keep ownership available before a cancellation can unwind startup.
            previous = signal.pthread_sigmask(signal.SIG_BLOCK, [signal.SIGINT, signal.SIGTERM])
            try:
                self.proc = subprocess.Popen(args, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             start_new_session=True, preexec_fn=child_limits)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous)
            for stream, name in [(self.proc.stdout, 'stdout'), (self.proc.stderr, 'stderr')]:
                os.set_blocking(stream.fileno(), False)
                self.selector.register(stream, selectors.EVENT_READ, name)
            os.set_blocking(self.proc.stdin.fileno(), False)
        except BaseException:
            if self.proc is not None:
                self.close()
            else:
                self.selector.close()
            raise

    def send(self, value):
        data = json.dumps(value, separators=(',', ':')).encode() + b'\n'
        if len(data) > FRAME_LIMIT:
            raise ProbeError('rpc_frame_limit_exceeded')
        with selectors.DefaultSelector() as writer:
            writer.register(self.proc.stdin, selectors.EVENT_WRITE)
            while data:
                if writer.select(min(0.05, self.budget.remaining())):
                    try:
                        count = os.write(self.proc.stdin.fileno(), data)
                    except BrokenPipeError as error:
                        raise ProbeError('client_exited') from error
                    data = data[count:]

    def line(self):
        while True:
            newline = self.buffer.find(b'\n')
            if newline >= 0:
                if newline + 1 > FRAME_LIMIT:
                    raise ProbeError('rpc_frame_limit_exceeded')
                line = bytes(self.buffer[:newline])
                del self.buffer[:newline + 1]
                return line
            if len(self.buffer) > FRAME_LIMIT:
                raise ProbeError('rpc_frame_limit_exceeded')
            if not self.selector.get_map():
                raise ProbeError('client_exited')
            for key, _ in self.selector.select(min(0.05, self.budget.remaining())):
                data = os.read(key.fileobj.fileno(), 65536)
                if not data:
                    self.selector.unregister(key.fileobj)
                    continue
                self.budget.add(len(data))
                self.counts[key.data] += len(data)
                if key.data == 'stdout':
                    self.buffer.extend(data)
                else:
                    self.stderr_hash.update(data)
                    if self.counts['stderr'] > FRAME_LIMIT:
                        raise ProbeError('stderr_limit_exceeded')

    def receive(self, request_id=None, method=None):
        while True:
            value = json_value(self.line())
            if not isinstance(value, dict):
                raise ProbeError('invalid_rpc_frame')
            if 'id' in value and 'method' in value:
                raise ProbeError('unexpected_client_tool_or_approval_request')
            if request_id is not None and value.get('id') == request_id:
                if 'error' in value:
                    raise ProbeError('rpc_request_rejected')
                return value.get('result', {})
            if method is not None and value.get('method') == method:
                return value.get('params', {})

    def close(self):
        # The group can outlive its leader, so signal it even after client exit.
        try:
            os.killpg(self.proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self.proc.wait(timeout=2)
        self.selector.close()
        for stream in [self.proc.stdin, self.proc.stdout, self.proc.stderr]:
            stream.close()


class ResponsesStub:
    """Accept exactly one bounded request, then return fixed SSE without tool calls."""
    def __init__(self, budget, responder=None, observe=None):
        self.budget = budget
        self.responder = responder
        self.observe = observe
        self.exchanges = 0
        self.request = None
        self.error = None
        self.credentials_present = False
        self.request_bytes = 0
        self.connection = None
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.listener.listen(1)
        self.listener.settimeout(0.05)
        self.url = f'http://127.0.0.1:{self.listener.getsockname()[1]}/v1'
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self.serve, daemon=True)

    def start(self):
        self.thread.start()

    def serve(self):
        while not self.stopped.is_set():
            self.exchange()
            if self.error or self.responder is None:
                return

    def exchange(self):
        try:
            self.connection = None
            while not self.stopped.is_set():
                self.budget.remaining()
                try:
                    self.connection, _ = self.listener.accept()
                    break
                except socket.timeout:
                    continue
            if self.connection is None:
                return
            with self.connection as conn:
                conn.settimeout(min(2, self.budget.remaining()))
                data = bytearray()
                while b'\r\n\r\n' not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        raise ProbeError('incomplete_http_headers')
                    self.budget.add(len(chunk))
                    data.extend(chunk)
                    if len(data) > 16384:
                        raise ProbeError('http_header_limit_exceeded')
                headers, body = bytes(data).split(b'\r\n\r\n', 1)
                lines = headers.decode('ascii').split('\r\n')
                if lines[0] != 'POST /v1/responses HTTP/1.1':
                    raise ProbeError('unexpected_http_request')
                fields = {}
                for line in lines[1:]:
                    key, value = line.split(':', 1)
                    key = key.lower()
                    if key in fields:
                        raise ProbeError('duplicate_http_header')
                    fields[key] = value.strip()
                self.credentials_present = any(
                    key in fields for key in ['authorization', 'cookie', 'proxy-authorization',
                                              'x-api-key', 'x-openai-actor-authorization',
                                              'chatgpt-account-id'])
                if self.credentials_present:
                    raise ProbeError('credentials_in_local_request')
                if 'transfer-encoding' in fields or fields.get('content-encoding', 'identity') != 'identity':
                    raise ProbeError('unsupported_http_encoding')
                length = int(fields['content-length'])
                if length < 0 or length + len(headers) + 4 > FRAME_LIMIT:
                    raise ProbeError('http_frame_limit_exceeded')
                while len(body) < length:
                    conn.settimeout(min(2, self.budget.remaining()))
                    chunk = conn.recv(min(65536, length - len(body)))
                    if not chunk:
                        raise ProbeError('incomplete_http_body')
                    self.budget.add(len(chunk))
                    body += chunk
                if len(body) != length:
                    raise ProbeError('unexpected_http_bytes')
                request = json_value(body)
                if not isinstance(request, dict):
                    raise ProbeError('invalid_responses_request')
                self.request = request
                self.request_bytes = length
                self.exchanges += 1
                if self.exchanges > 100:
                    raise ProbeError('responses_request_limit_exceeded')
                if self.observe:
                    self.observe(request)
                item = {'id': 'msg_preflight', 'type': 'message', 'role': 'assistant',
                        'status': 'completed', 'content': [{'type': 'output_text',
                        'text': 'preflight-complete', 'annotations': []}]}
                items = self.responder(request) if self.responder else [item]
                response = {'id': f'resp_preflight_{self.exchanges}', 'object': 'response', 'status': 'completed',
                            'output': items, 'usage': {'input_tokens': 0, 'output_tokens': 0,
                            'total_tokens': 0, 'input_tokens_details': {'cached_tokens': 0}}}
                events = [
                    {'type': 'response.created', 'response': dict(response, status='in_progress', output=[])},
                    *[{'type': 'response.output_item.done', 'output_index': i, 'item': value}
                      for i, value in enumerate(items)],
                    {'type': 'response.completed', 'response': response},
                ]
                payload = ''.join(f'event: {e["type"]}\ndata: {json.dumps(e)}\n\n' for e in events).encode()
                if len(payload) > FRAME_LIMIT:
                    raise ProbeError('responses_event_limit_exceeded')
                if self.responder:
                    self.budget.add(len(payload))
                conn.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n'
                             + f'Content-Length: {len(payload)}\r\n\r\n'.encode() + payload)
        except ProbeError as error:
            self.error = str(error)
        except (OSError, ValueError, KeyError, UnicodeError):
            if not self.stopped.is_set():
                self.error = 'local_http_failure'

    def close(self):
        self.stopped.set()
        if self.connection is not None:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.listener.close()
        if self.thread.ident is not None:
            self.thread.join(timeout=2)
            if self.thread.is_alive():
                raise ProbeError('stub_cleanup_failed')
