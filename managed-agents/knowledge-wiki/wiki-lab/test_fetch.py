"""Synthetic SDK responses only; never log in during tests."""
import datetime
import json
from pathlib import Path
import tempfile
import types
import unittest

from corpus import sha256
from fetch import active_terminal_stop, pack, run_capture, read_rows, validate_requests


def response(code=0):
    return types.SimpleNamespace(ErrorCode=code, ErrorMsg='synthetic', Codes=[], Indicators=['VALUE'], Dates=[], Data={'0': [7]})


class FakeSDK:
    def __init__(self, login=0, query=0, exception=False, logout=0, empty=False):
        self.login, self.query, self.exception = login, query, exception
        self.logout, self.empty = logout, empty
        self.calls, self.stops, self.starts = 0, 0, 0
    def start(self):
        self.starts += 1
        return response(self.login)
    def stop(self):
        self.stops += 1
        return response(self.logout)
    def ctr(self, *args):
        self.calls += 1
        if self.exception:
            raise RuntimeError('synthetic failure')
        result = response(self.query)
        if self.empty:
            result.Data = {'0': [None]}
        return result


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.requests = self.root / 'requests.json'
        self.out = self.root / 'fetch'
        self.doc = {'schema': 'wiki-lab-requests/1', 'source_slug': 'synthetic', 'requests': [
            {'id': 'test-1', 'purpose': 'synthetic test', 'function': 'ctr', 'args': ['BalanceStatementSHSZ', '',
             'SecuCode=000001.SZ,ReportDate=2025-12-31,ReportType=1,Ispandas=0'], 'kwargs': {}}]}
    def tearDown(self):
        self.tmp.cleanup()
    def run_fake(self, sdk):
        self.requests.write_text(json.dumps(self.doc))
        return run_capture(self.requests, self.out, sdk=sdk)
    def test_full_payload_manifest_hash_and_replay_rejection(self):
        sdk = FakeSDK()
        self.assertEqual(self.run_fake(sdk)['status'], 'completed')
        manifest = read_rows(self.out / 'manifests/sources.jsonl')[0]
        self.assertEqual(sha256(self.out / manifest['path']), manifest['sha256'])
        raw = json.loads((self.out / manifest['path']).read_text())
        self.assertEqual(raw['queries'][0]['result']['Data']['0']['items'], [7])
        self.assertIn('logout', raw)
        self.doc['source_slug'] = 'another'
        self.doc['requests'][0]['id'] = 'another-id'
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            self.run_fake(sdk)
        self.assertEqual(sdk.calls, 1)
    def test_failed_query_counts_and_stops(self):
        sdk = FakeSDK(query=10003003)
        self.assertEqual(self.run_fake(sdk)['attempted'], 1)
        self.assertEqual(sdk.stops, 1)
        rows = read_rows(self.out / 'calls.jsonl')
        self.assertEqual(sum(r['event'] == 'call_started' for r in rows), 1)
    def test_login_terminal_no_query_no_logout_no_retry(self):
        for code in (10001009, 10001019, 10001020, 10002002):
            with self.subTest(code=code):
                self.out = self.root / str(code)
                sdk = FakeSDK(login=code)
                self.assertEqual(self.run_fake(sdk)['attempted'], 0)
                self.assertEqual((sdk.calls, sdk.stops), (0, 0))
                with self.assertRaisesRegex(ValueError, 'terminal'):
                    self.run_fake(sdk)
                self.assertEqual(sdk.starts, 1)
    def test_authorized_network_resume_keeps_failure_and_budget(self):
        self.run_fake(FakeSDK(login=10002002))
        log = self.out / 'calls.jsonl'
        old_bytes = log.read_bytes()
        old_manifest = read_rows(self.out / 'manifests/sources.jsonl')[0]
        old_hash = sha256(self.out / old_manifest['path'])
        budget_bytes = (self.out / 'budget.json').read_bytes()
        self.doc['source_slug'] = 'resumed'
        self.requests.write_text(json.dumps(self.doc))
        sdk = FakeSDK()
        result = run_capture(self.requests, self.out, sdk=sdk,
                             resume_reason='Synthetic explicit user network restoration')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(sdk.calls, 1)
        self.assertTrue(log.read_bytes().startswith(old_bytes))
        self.assertEqual(sha256(self.out / old_manifest['path']), old_hash)
        self.assertEqual((self.out / 'budget.json').read_bytes(), budget_bytes)
        self.assertIsNone(active_terminal_stop(read_rows(log)))
        self.assertEqual(sum(r['event'] == 'call_started' for r in read_rows(log)), 1)
    def test_network_resume_cannot_clear_account_failure(self):
        self.run_fake(FakeSDK(login=10001009))
        self.doc['source_slug'] = 'resumed'
        self.requests.write_text(json.dumps(self.doc))
        sdk = FakeSDK()
        with self.assertRaisesRegex(ValueError, 'cannot clear account'):
            run_capture(self.requests, self.out, sdk=sdk, resume_reason='Synthetic network restoration')
        self.assertEqual(sdk.starts, 0)
    def test_exception_still_logs_out(self):
        sdk = FakeSDK(exception=True)
        self.assertEqual(self.run_fake(sdk)['status'], 'failed')
        self.assertEqual(sdk.stops, 1)
        self.doc['source_slug'] = 'different'
        self.doc['requests'][0]['id'] = 'different'
        self.doc['requests'][0]['args'][0] = 'IncomeStatementSHSZ'
        with self.assertRaisesRegex(ValueError, 'terminal'):
            self.run_fake(sdk)
        self.assertEqual(sdk.starts, 1)
    def test_logout_error_is_terminal(self):
        sdk = FakeSDK(logout=10002002)
        self.assertEqual(self.run_fake(sdk)['status'], 'failed')
        with self.assertRaisesRegex(ValueError, 'terminal'):
            self.run_fake(sdk)
        self.assertEqual(sdk.starts, 1)
    def test_empty_data_is_explicit_and_not_retried(self):
        sdk = FakeSDK(empty=True)
        summary = self.run_fake(sdk)
        self.assertEqual(summary['status'], 'no_data')
        self.assertEqual(summary['request_results'][0]['content_status'], 'no_data')
        self.assertTrue(summary['request_results'][0]['reason'])
        with self.assertRaisesRegex(ValueError, 'already attempted'):
            self.run_fake(sdk)
        self.assertEqual(sdk.calls, 1)
    def test_budget_cannot_grow(self):
        self.run_fake(FakeSDK())
        with self.assertRaisesRegex(ValueError, 'budget'):
            run_capture(self.requests, self.out, max_calls=7, sdk=FakeSDK())
    def test_budget_is_cumulative_including_failure_across_batches(self):
        self.requests.write_text(json.dumps(self.doc))
        failed = run_capture(self.requests, self.out, max_calls=2, sdk=FakeSDK(query=10003003))
        self.assertEqual(failed['attempted'], 1)
        self.doc['source_slug'] = 'second'
        self.doc['requests'][0].update(id='test-2')
        self.doc['requests'][0]['args'][0] = 'IncomeStatementSHSZ'
        self.requests.write_text(json.dumps(self.doc))
        self.assertEqual(run_capture(self.requests, self.out, max_calls=2, sdk=FakeSDK())['attempted'], 1)
        self.doc['source_slug'] = 'third'
        self.doc['requests'][0].update(id='test-3')
        self.doc['requests'][0]['args'][0] = 'CashFlowStatementSHSZ'
        self.requests.write_text(json.dumps(self.doc))
        sdk = FakeSDK()
        with self.assertRaisesRegex(ValueError, 'remaining call budget'):
            run_capture(self.requests, self.out, max_calls=2, sdk=sdk)
        self.assertEqual(sdk.starts, 0)
    def test_network_query_error_stops_later_calls_and_new_batches(self):
        second = dict(self.doc['requests'][0], id='test-2', args=['IncomeStatementSHSZ', '', self.doc['requests'][0]['args'][-1]])
        self.doc['requests'].append(second)
        sdk = FakeSDK(query=10002002)
        summary = self.run_fake(sdk)
        self.assertEqual(summary['attempted'], 1)
        self.assertEqual(summary['unattempted_request_ids'], ['test-2'])
        self.assertEqual(sdk.stops, 1)
        self.doc['source_slug'] = 'next'
        self.doc['requests'] = [second]
        with self.assertRaisesRegex(ValueError, 'terminal'):
            self.run_fake(sdk)
        self.assertEqual(sdk.starts, 1)
    def test_invalid_reporttype_and_ispandas_rejected(self):
        for fragment in ('ReportType=2,Ispandas=0', 'ReportType=1,Ispandas=1'):
            self.doc['requests'][0]['args'][-1] = fragment
            with self.assertRaises(ValueError):
                validate_requests(self.doc)
    def test_pack_preserves_types(self):
        self.assertEqual(pack(b'hi')['hex'], '6869')
        self.assertEqual(pack((1, 2))['__type__'], 'tuple')
        self.assertEqual(pack(datetime.date(2026, 1, 1))['isoformat'], '2026-01-01')


if __name__ == '__main__':
    unittest.main()
