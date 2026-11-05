import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

PROXY = Path(__file__).resolve().parents[1] / 'plugin/feedback_proxy.py'
spec = importlib.util.spec_from_file_location('feedback_proxy', PROXY)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.child = SimpleNamespace(stdin=io.StringIO())
        self.proxy = module.Proxy(self.child)
        self.output = []
        self.proxy.output = self.output.append

    def message(self, action='act', arguments=None, request_id=1):
        return {'jsonrpc': '2.0', 'id': request_id, 'method': 'tools/call', 'params': {
            'name': 'dispatch', 'arguments': {'action': action, 'arguments': arguments or {}}}}

    def handle(self, message):
        self.proxy.handle(message, json.dumps(message) + '\n')

    def join(self):
        for thread in self.proxy.workers:
            thread.join(2)
            self.assertFalse(thread.is_alive())

    def test_unavailable_indicator_preserves_request_and_response(self):
        message = self.message(arguments={'operation': {'type': 'paste', 'text': 'private'}})
        with patch.object(module, 'feedback', side_effect=OSError('absent')):
            self.handle(message)
            self.join()
            response = json.dumps({'id': 1, 'result': {'content': [{'type': 'text', 'text': 'unchanged'}]}}) + '\n'
            self.proxy.receive(response)
        self.assertEqual(json.loads(self.child.stdin.getvalue()), message)
        self.assertEqual(self.output, [response])
        self.assertEqual(self.proxy.pending, {})

    def test_cancel_during_warning_never_dispatches(self):
        started = threading.Event()
        def feedback(op, *_):
            if op == 'begin':
                started.set()
                return {'wait_ms': 2000}
            return {'ok': True}
        with patch.object(module, 'feedback', feedback):
            self.handle(self.message())
            self.assertTrue(started.wait(1))
            self.handle({'method': 'notifications/cancelled', 'params': {'requestId': 1}})
            self.join()
        self.assertEqual(self.child.stdin.getvalue(), '')
        self.assertTrue(json.loads(self.output[0])['result']['isError'])

    def test_explicit_pause_is_respected(self):
        with patch.object(module, 'feedback', return_value={'error': 'paused by user'}):
            self.handle(self.message())
            self.join()
        self.assertEqual(self.child.stdin.getvalue(), '')
        self.assertIn('paused by user', self.output[0])

    def test_takeover_during_warning_blocks_dispatch(self):
        def feedback(op, *_):
            return {'error': 'physical activity'} if op == 'ready' else {'wait_ms': 0}
        with patch.object(module, 'feedback', feedback):
            self.handle(self.message())
            self.join()
        self.assertEqual(self.child.stdin.getvalue(), '')
        self.assertIn('physical activity', self.output[0])

    def test_background_target_retains_route_without_desktop_selector(self):
        with patch.object(module, 'feedback') as feedback:
            self.handle(self.message('list_desktop', {'desktop': 'background', 'scope': 'windows'}))
            self.proxy.receive(json.dumps({'id': 1, 'result': {'structuredContent': {
                'desktop': 'background', 'target': {'app_instance_id': 'app-123', 'window_instance_id': 'win-123'}}}}) + '\n')
            self.handle(self.message('observe', {'target': {'app_instance_id': 'app-123', 'window_instance_id': 'win-123'}}, 2))
            feedback.assert_not_called()
        self.assertEqual(len(self.child.stdin.getvalue().splitlines()), 2)

    def test_old_protocol_text_routes_are_supported(self):
        with patch.object(module, 'feedback') as feedback:
            self.handle(self.message('list_desktop', {'desktop': 'background', 'scope': 'windows'}))
            self.proxy.receive(json.dumps({'id': 1, 'result': {'content': [{'type': 'text',
                'text': 'Desktop: background session=session-123\napp-123 win-123 obs-123 e-123'}]}}) + '\n')
            self.handle(self.message('act', {'observation': {'observation_id': 'obs-123'}, 'operation': {'element_id': 'e-123'}}, 2))
            feedback.assert_not_called()

    def test_notification_does_not_consume_pending_call(self):
        with patch.object(module, 'feedback', side_effect=OSError('absent')):
            self.handle(self.message())
            self.join()
            self.proxy.receive('{"method":"notifications/message","params":{}}\n')
        self.assertIn('1', self.proxy.pending)

    def test_post_dispatch_cancel_is_forwarded(self):
        with patch.object(module, 'feedback', side_effect=OSError('absent')):
            self.handle(self.message())
            self.join()
            self.handle({'method': 'notifications/cancelled', 'params': {'requestId': 1}})
        self.assertEqual(len(self.child.stdin.getvalue().splitlines()), 2)

    def test_feedback_labels_exclude_user_text(self):
        with patch.object(module, 'feedback', return_value={'wait_ms': 0}) as feedback:
            self.handle(self.message(arguments={'operation': {'type': 'paste', 'text': 'secret'}}))
            self.join()
        self.assertNotIn('secret', str(feedback.call_args_list))

    def test_result_arrives_before_slow_halo_end_acknowledgement(self):
        delivered = threading.Event()
        release = threading.Event()
        waiting = threading.Event()
        self.proxy.output = lambda line: delivered.set()
        self.proxy.pending['1'] = {'call': 'call-1', 'route': 'foreground'}
        def feedback(*args):
            waiting.set()
            release.wait(2)
            return {'ok': True}
        with patch.object(module, 'feedback', feedback):
            response = json.dumps({'id': 1, 'result': {'content': []}}) + '\n'
            worker = threading.Thread(target=self.proxy.receive, args=(response,))
            worker.start()
            try:
                self.assertTrue(waiting.wait(1))
                self.assertTrue(delivered.is_set(), 'halo end must not delay delivery')
            finally:
                release.set()
                worker.join(2)

    def test_stdio_and_exit_status_without_halo(self):
        child_code = 'import sys; [print(line.strip(), flush=True) for line in sys.stdin]; sys.exit(7)'
        result = subprocess.run([sys.executable, str(PROXY), sys.executable, '-u', '-c', child_code],
            input='{"id":1,"method":"initialize"}\n', text=True, capture_output=True,
            env={**os.environ, 'COMPUTER_USE_MCP_FEEDBACK': '0'}, timeout=5)
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(result.stdout, '{"id":1,"method":"initialize"}\n')

    def test_native_exit_closes_proxy_while_host_stdin_remains_open(self):
        child = subprocess.Popen([sys.executable, str(PROXY), sys.executable, '-c', 'raise SystemExit(9)'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env={**os.environ, 'COMPUTER_USE_MCP_FEEDBACK': '0'})
        try:
            self.assertEqual(child.wait(timeout=3), 9)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdin.close()
            child.stdout.close()
            child.stderr.close()


if __name__ == '__main__':
    unittest.main()
