"""S2 acceptance/isolation/budget tests use synthetic inputs and a fake CLI."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

import records
import s2
from test_records import sample


def model_record(key='one'):
    item = sample()
    item['record_id'] = key
    for name in records.CODE_FIELDS:
        item.pop(name)
    return item


def output(items):
    return dict(schema_version=records.VERSION, records=items,
        coverage=[dict(item_id='T01', status='已抽', record_ids=[item['record_id'] for item in items],
                       checked_pages=[2], basis='Synthetic revenue table')])


def accept(value, **extra):
    return records.accept_model_output(value, source_id='synthetic-report', disclosed_on='2026-03-01',
        extraction_version='test-s2', prefix='chunk',
        pages={('synthetic-report', 2): 'Revenue 1,250.50'}, checklist=dict(items=[dict(id='T01')]), **extra)


class S2RecordAcceptanceTests(unittest.TestCase):
    def test_projection_and_transmission_derive_from_one_contract(self):
        schema = records.model_output_schema()
        full = records.extract_output_schema()['properties']['records']['items']['anyOf']
        for small, large in zip(schema['properties']['records']['items']['anyOf'], full):
            self.assertEqual(set(small['properties']), set(large['properties'])-records.CODE_FIELDS)
        self.assertIn('scope:', records.field_guide())
        self.assertNotIn('uniqueItems', json.dumps(records.model_output_schema(True)))
        self.assertIn('uniqueItems', json.dumps(schema))

    def test_one_bad_record_never_discards_good_record(self):
        good, bad = model_record('one'), model_record('two')
        bad['numbers'][0]['value'] = '1250600'
        result = accept(output([good, bad]))
        self.assertEqual([r['record_id'] for r in result['records']], ['chunk-one'])
        self.assertEqual(len(result['rejected']), 1)
        self.assertEqual(result['coverage'][0]['status'], '未通过核验')
        self.assertEqual(result['coverage'][0]['record_ids'], ['chunk-one', 'chunk-two'])
        self.assertEqual(result['records'][0]['verification']['status'], '代码通过')

    def test_bad_record_field_type_is_individual_rejection(self):
        good, bad = model_record('one'), model_record('two')
        bad['supersedes'] = None
        result = accept(output([good, bad]))
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(len(result['rejected']), 1)

    def test_bad_record_identifier_is_individual_when_coverage_is_unambiguous(self):
        good, bad = model_record('one'), model_record('two')
        value = output([good])
        bad.pop('record_id')
        value['records'].append(bad)
        result = accept(value)
        self.assertEqual(len(result['records']), 1)
        self.assertEqual(len(result['rejected']), 1)
        self.assertEqual(result['coverage'][0]['status'], '已抽')

    def test_overall_invalid_and_unknown_coverage_id_fail_block(self):
        for value in ([], {'schema_version': records.VERSION, 'records': [], 'coverage': []}):
            with self.assertRaises(ValueError):
                accept(value)
        value = output([model_record()])
        value['coverage'][0]['record_ids'] = ['nonexistent']
        with self.assertRaises(ValueError):
            accept(value)

    def test_context_only_discard_and_supplied_span_guard(self):
        result = accept(output([model_record()]), context_pages=[2])
        self.assertFalse(result['records'])
        self.assertEqual(len(result['discarded']), 1)
        result = accept(output([model_record()]), supplied_spans={2: ['Unrelated header']})
        self.assertFalse(result['records'])
        self.assertIn('outside supplied', result['rejected'][0]['errors'][0])

    def test_model_cannot_supply_code_owned_fields(self):
        item = model_record()
        item['verification'] = dict(status='人工复核通过', note='invented')
        result = accept(output([item]))
        self.assertFalse(result['records'])
        self.assertTrue(any('unknown field' in e for e in result['rejected'][0]['errors']))


class S2RunnerTests(unittest.TestCase):
    def test_call_budget_counts_reserved_failures_and_does_not_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for i in range(40):
                self.assertEqual(s2.reserve_call(root, 'probe-failed'), i+1)
            with self.assertRaisesRegex(RuntimeError, 'limit 40'):
                s2.reserve_call(root, 'retry')
            self.assertEqual(len((root / 'calls.jsonl').read_text().splitlines()), 40)

    def test_prompt_allowlist_and_no_embedded_schema(self):
        prompt = s2.render_prompt(dict(id='SYN', source_id='synthetic', date='2026-01-01',
            path='DO_NOT_INCLUDE', records='DO_NOT_INCLUDE', p1_notes='DO_NOT_INCLUDE'),
            '[PAGE: 2] [MODE: EXTRACT]\nRevenue125', dict(items=[]))
        self.assertNotIn('DO_NOT_INCLUDE', prompt)
        self.assertNotIn('"additionalProperties"', prompt)
        self.assertNotIn('PDF PAGE 1', prompt)
        self.assertIn('Revenue125', prompt)
        self.assertIn('2026H1', prompt)


if __name__ == '__main__':
    unittest.main()
