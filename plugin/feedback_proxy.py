"""Portable stdio MCP proxy for the optional desktop activity halo.

Only operation labels and request IDs go to the indicator. MCP contents stay
between the host and its bundled executable. An unavailable indicator is
optional; an explicit pause refusal from a running indicator is respected.
"""
import json
import math
import os
import re
import socket
import subprocess
import sys
import threading
from pathlib import Path

ACTIONS = {'list_desktop', 'launch_application', 'activate_window', 'observe', 'act', 'wait_for'}
ID_KEYS = {'app_instance_id', 'window_instance_id', 'observation_id', 'frame_id',
           'element_id', 'cursor', 'next_cursor', 'after_observation_id', 'replacement_observation_id',
           'after_frame_id', 'replacement_frame_id', 'replacement_element_id'}


def feedback(op, call, label=None):
    root = os.environ.get('XDG_RUNTIME_DIR')
    if not root or os.environ.get('COMPUTER_USE_MCP_FEEDBACK') == '0':
        raise OSError('optional indicator is disabled or unavailable')
    request = {'op': op, 'call': call, 'label': label}
    if os.environ.get('HALO_AGENT'):
        request['agent'] = os.environ['HALO_AGENT']
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        sock.connect(str(Path(root) / 'codex-activity-halo/feedback.sock'))
        sock.sendall((json.dumps(request) + '\n').encode())
        with sock.makefile('rb') as reader:
            response = json.loads(reader.readline(4096))
        if not isinstance(response, dict):
            raise ValueError('invalid indicator response')
        return response


def operation(message):
    params = message.get('params', {})
    if not isinstance(params, dict):
        return None, {}
    args = params.get('arguments', {})
    if not isinstance(args, dict):
        return None, {}
    name = params.get('name')
    if name == 'dispatch':
        name, args = args.get('action'), args.get('arguments', {})
    return name if isinstance(name, str) else None, args if isinstance(args, dict) else {}


def label_for(action, args):
    if action == 'act':
        return 'Interaction avec le PC'
    return {'observe': 'Capture de l’écran', 'list_desktop': 'Repérage des fenêtres',
            'activate_window': 'Gestion d’une fenêtre', 'launch_application': 'Ouverture d’une application',
            'wait_for': 'Attente d’un changement'}.get(action, 'Action sur le PC')


def identities(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ID_KEYS and isinstance(item, str):
                yield item
            else:
                yield from identities(item)
    elif isinstance(value, list):
        for item in value:
            yield from identities(item)


class Proxy:
    def __init__(self, child):
        self.child = child
        self.pending = {}
        self.routes = {}  # bounded routing hints; the Rust broker remains authoritative
        self.lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.output_lock = threading.Lock()
        self.workers = []

    def output(self, line):
        with self.output_lock:
            sys.stdout.write(line)
            sys.stdout.flush()

    def forward(self, line):
        with self.write_lock:
            self.child.stdin.write(line)
            self.child.stdin.flush()

    def reject(self, message, detail):
        self.output(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': {
            'content': [{'type': 'text', 'text': 'Action not started: ' + detail}],
            'isError': True}}) + '\n')

    def prepare(self, message, line, item):
        try:
            try:
                response = feedback('begin', item['call'], item['label'])
            except (OSError, ValueError):
                response = None
            if response is not None:
                if response.get('error'):
                    raise RuntimeError(str(response['error']))
                wait_ms = response.get('wait_ms', 0)
                if not isinstance(wait_ms, (int, float)) or not math.isfinite(wait_ms):
                    wait_ms = 0
                if item['cancel'].wait(min(2000, max(0, wait_ms)) / 1000):
                    raise RuntimeError('cancelled before dispatch')
                try:
                    readiness = feedback('ready', item['call'])
                except (OSError, ValueError):
                    readiness = {}
                if readiness.get('error'):
                    raise RuntimeError(str(readiness['error']))
            with self.lock:
                if item['cancel'].is_set():
                    raise RuntimeError('cancelled before dispatch')
                self.forward(line)
                item['sent'] = True
        except (OSError, ValueError, RuntimeError) as error:
            with self.lock:
                self.pending.pop(str(message['id']), None)
            self.end(item)
            self.reject(message, str(error))

    @staticmethod
    def end(item):
        if item['call'] is not None:
            try:
                feedback('end', item['call'])
            except (OSError, ValueError):
                pass

    def handle(self, message, line):
        if message.get('method') == 'notifications/cancelled':
            key = str(message.get('params', {}).get('requestId'))
            with self.lock:
                item = self.pending.get(key)
                if item and not item['sent']:
                    item['cancel'].set()
                    return
        action, args = operation(message)
        if message.get('method') != 'tools/call' or 'id' not in message or action not in ACTIONS:
            self.forward(line)
            return
        with self.lock:
            route = args.get('desktop')
            if route is None:
                route = next((self.routes[ref] for ref in identities(args) if ref in self.routes), None)
            route = route or 'foreground'
            key = str(message['id'])
            item = {'call': f'{os.getpid()}-{key}' if route == 'foreground' else None,
                    'cancel': threading.Event(), 'sent': False, 'label': label_for(action, args), 'route': route}
            self.pending[key] = item
            if route != 'foreground':
                self.forward(line)
                item['sent'] = True
                return
            worker = threading.Thread(target=self.prepare, args=(message, line, item), daemon=True)
            self.workers = [thread for thread in self.workers if thread.is_alive()]
            self.workers.append(worker)
            worker.start()

    def receive(self, line):
        try:
            response = json.loads(line)
            if 'id' in response:
                with self.lock:
                    item = self.pending.pop(str(response['id']), None)
                    if item:
                        result = response.get('result', {})
                        structured = result.get('structuredContent') or {}
                        route = structured.get('desktop', item['route'])
                        # Older MCP versions carry routing in the leading text header.
                        for block in result.get('content', []):
                            if block.get('type') == 'text':
                                text = block.get('text', '')
                                if text.startswith('Desktop: background '):
                                    route = 'background'
                                elif text.startswith('Desktop: foreground '):
                                    route = 'foreground'
                        if route in {'background', 'foreground'}:
                            for ref in identities(structured):
                                self.routes.pop(ref, None)
                                self.routes[ref] = route
                            # Text-only IDs are routing hints too; never inspect supplied input.
                            for block in result.get('content', []):
                                if block.get('type') == 'text':
                                    for ref in re.findall(r'\b(?:app|win|obs|frame|e|cur)-[a-zA-Z0-9_-]+', block.get('text', '')):
                                        self.routes.pop(ref, None)
                                        self.routes[ref] = route
                            while len(self.routes) > 65536:
                                self.routes.pop(next(iter(self.routes)))
                if item:
                    self.end(item)
        except (ValueError, TypeError, AttributeError):
            pass
        self.output(line)

    def read_output(self):
        for line in self.child.stdout:
            self.receive(line)


def main():
    child = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             text=True, bufsize=1)
    proxy = Proxy(child)
    def close_input():
        with proxy.lock:
            for item in proxy.pending.values():
                if not item['sent']:
                    item['cancel'].set()
            workers = list(proxy.workers)
        for worker in workers:
            worker.join(timeout=3)
        with proxy.write_lock:
            child.stdin.close()

    def read_input():
        try:
            for line in sys.stdin:
                try:
                    message = json.loads(line)
                except ValueError:
                    proxy.forward(line)
                    continue
                if isinstance(message, dict):
                    proxy.handle(message, line)
                else:
                    proxy.forward(line)
        except (OSError, ValueError):
            pass  # The native server may have exited while host input is open.
        finally:
            close_input()

    # Read native stdout on the main thread: a crashed native server must close
    # the host transport immediately, even while the host keeps stdin open.
    threading.Thread(target=read_input, daemon=True).start()
    try:
        proxy.read_output()
    finally:
        close_input()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        for item in list(proxy.pending.values()):
            proxy.end(item)
    return child.returncode


if __name__ == '__main__':
    sys.exit(main())
