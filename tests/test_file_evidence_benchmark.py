"""Offline safety/measurement tests for the explicitly live benchmark runner."""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

from _decide_fakes import TempHome
from jevkit import client


def module(name):
    path = Path(__file__).resolve().parents[1] / 'scripts' / (name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


bench = module('bench_file_evidence_live')
metrics = module('summarize_file_evidence_live')


class BenchmarkGuardTests(TempHome):
    def setUp(self):
        super().setUp()
        self.folder_patch = patch.object(bench, 'RESULTS', self.home)
        self.folder_patch.start()
        self.addCleanup(self.folder_patch.stop)
        self.receipts = self.home / 'pilot-requests.jsonl'

    def test_bounds_refuse_before_network(self):
        for attr, value in [('count', 100), ('existing', 600), ('failures', 5), ('started', -1000)]:
            guard = bench.Guard(self.receipts)
            setattr(guard, attr, value)
            with patch.object(client, '_http_transport') as network:
                with self.assertRaisesRegex(RuntimeError, 'pilot_guard_stop'):
                    guard(b'{}', {}, 4)
                network.assert_not_called()
        guard = bench.Guard(self.receipts)
        with patch.object(client, '_http_transport') as network:
            with self.assertRaisesRegex(RuntimeError, 'wire_size'):
                guard(b'x' * 16001, {}, 4)
            network.assert_not_called()

    def test_receipts_count_attempts_without_auth_or_body(self):
        guard = bench.Guard(self.receipts)
        reply = b'{"usage":{"input_tokens":123},"model":"jev-1.13.0"}'
        def send(body, headers, timeout):
            self.assertEqual(bench.load_rows(self.receipts)[0]['kind'], 'reservation')
            return reply
        with patch.object(client, '_http_transport', side_effect=send):
            self.assertEqual(guard(b'{"state":"synthetic example"}', {'Authorization': 'fixture-only'}, 4), reply)
        saved = self.receipts.read_text()
        self.assertNotIn('fixture-only', saved)
        self.assertNotIn('synthetic example', saved)
        rows = bench.load_rows(self.receipts)
        self.assertEqual(rows[1]['provider_input_tokens'], 123)
        self.assertEqual(bench.Guard(self.receipts).existing, 1)

    def test_error_and_missing_usage_are_not_zero_cost_claims(self):
        guard = bench.Guard(self.receipts)
        with patch.object(client, '_http_transport', side_effect=client.JevError('timeout')):
            with self.assertRaises(client.JevError):
                guard(b'{}', {}, 4)
        self.assertEqual(bench.load_rows(self.receipts)[-1]['error'], 'timeout')
        with patch.object(client, '_http_transport', return_value=b'{"model":"jev-1.13.0"}'):
            guard(b'{}', {}, 4)
        self.assertIsNone(bench.load_rows(self.receipts)[-1]['list_usd_estimate'])
        report = metrics.summarize([], bench.load_rows(self.receipts))
        self.assertEqual(report['usage']['logical_provider_attempts'], 2)
        self.assertEqual(report['usage']['responses_missing_usage'], 2)
        self.assertFalse(report['usage']['actual_billing_verified'])

    def test_five_failures_stop_and_nearest_rank(self):
        guard = bench.Guard(self.receipts)
        for _ in range(4):
            guard.observe({'source': 'fallback'})
        with self.assertRaisesRegex(RuntimeError, 'provider_stop'):
            guard.observe({'source': 'fallback'})
        self.assertEqual(metrics.percentile([3, 1, 2], .5), 2)
        self.assertEqual(metrics.percentile([3, 1, 2], .95), 3)
