from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import unittest

from corpus import dump, normalized_sha256, sha256
from s1 import rejection_history
from s1_review import (apply_manual_review, archive_sample_rejection, build_samples, expand_metric_layout, mapping,
                       note_items, review_groups, select_excerpt)


class S1ReviewTests(unittest.TestCase):
    def test_counts_rows_not_cells_and_inherits_caption(self):
        rows = note_items('# Synthetic\n\n下表为模拟资料[S1 p1]。\n\n|项目|本期|上期|\n|---|---:|---:|\n|收入|123|98|\n\n- 一个模拟说明。\n')
        self.assertEqual(len(rows), 3)
        self.assertIn('下表', rows[0]['text'])
        self.assertEqual(rows[1]['columns'][1], {'name': '本期', 'text': '123'})
        self.assertIn('模拟资料', rows[1]['context'][0]['text'])

    def test_caption_with_independent_fact_is_inventoried(self):
        rows = note_items('下表中的模拟股份均无限售，冻结数量为0。\n\n|名称|数量|\n|---|---|\n|甲|20|')
        self.assertEqual(len(rows), 2)
        self.assertIn('冻结数量为0', rows[0]['text'])
        self.assertEqual(rows[0]['form'], 'paragraph')

    def test_absent_note_is_not_issuer_fact(self):
        item = note_items('- 本块所选页未见其他事项。')[0]
        self.assertEqual(mapping(item)['fit'], 'non_l1')

    def test_adjacent_bullets_are_separate_items(self):
        rows = note_items('- 模拟第一项\n- 模拟第二项\n- 模拟第三项\n')
        self.assertEqual(len(rows), 3)
        self.assertEqual([x['line'] for x in rows], [1, 2, 3])

    def test_exact_selection_preserves_original_whitespace(self):
        self.assertEqual(select_excerpt('before a\n  b after', {'start': 'a', 'end': 'b'}), 'a\n  b')
        with self.assertRaises(ValueError):
            select_excerpt('abc', {'start': 'missing'})
        with self.assertRaisesRegex(ValueError, 'whole_page'):
            select_excerpt('abc', {'whole_page': True})

    def review_payload(self):
        item = note_items('|指标|本期|\n|---|---|\n|模拟阈值|5|')[0]
        item.update(item_id='synthetic.I0001', note='synthetic.md', mapping=mapping(item))
        return dict(files=[dict(path='synthetic.md', sha256='synthetic-sha')],
                    items=[item], review_groups=review_groups([item]))

    def test_review_defaults_pending_and_manual_type_changes_route_fields(self):
        original = self.review_payload()
        pending = deepcopy(original)
        apply_manual_review(pending, None)
        self.assertEqual(pending['manual_review']['conclusion'], 'pending')
        self.assertEqual(pending['items'][0]['manual_review']['status'], 'unreviewed')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'review.json'
            manual = dict(note_sha256={'synthetic.md': 'synthetic-sha'},
                          review_groups_sha256=normalized_sha256(original['review_groups']),
                          reviewer='synthetic reviewer', review_id='synthetic',
                          decisions=[dict(groups=['G001'], decision='fits',
                                          reason='Policy threshold is a definition, not an actual balance',
                                          record_types=['statement'], fields=['detail.manual_extension'])])
            path.write_text(json.dumps(manual))
            apply_manual_review(original, path)
            fields = original['items'][0]['mapping']['fields']
            self.assertIn('detail.speaker', fields)
            self.assertIn('detail.content', fields)
            self.assertIn('detail.manual_extension', fields)
            self.assertNotIn('detail.scope', fields)
            changed = self.review_payload()
            changed['review_groups'][0]['items_sha256'] = 'different-content'
            with self.assertRaisesRegex(ValueError, 'group definition changed'):
                apply_manual_review(changed, path)

    def test_period_columns_and_changes_have_independent_metric_keys(self):
        specs = [dict(record_id='synthetic', short='S', indicator='模拟资产',
                      number_specs=[dict(value='120', label='2030-06-30'),
                                    dict(value='100', label='2029-12-31'),
                                    dict(value='20', label='较上年末')])]
        result = expand_metric_layout(specs, dict(synthetic=dict(by_period_label=True,
                                       current_period='2030-06-30')))
        self.assertEqual([x['period'] for x in result], ['2030-06-30', '2029-12-31', '2030-06-30'])
        self.assertEqual([x.get('indicator', '模拟资产') for x in result],
                         ['模拟资产', '模拟资产', '模拟资产：较上年末'])
        self.assertTrue(all(len(x['number_specs']) == 1 for x in result))
        self.assertEqual(result[1]['as_of'], '2029-12-31')


class SampleRejectionTests(unittest.TestCase):
    def test_failure_keeps_diagnostic_and_success_archives_and_clears_it(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            p1, output = root / 'p1', root / 's1'
            page = p1 / 'p0001.txt'
            page.parent.mkdir()
            page.write_text('Synthetic revenue 1250\n')
            dump(p1 / 'corpus-manifest.json', {'sources': [dict(
                id='S1', source_id='synthetic-report', date='2026-03-01', pages=[1],
                page_records=[dict(page=1, corpus_path=str(page), text_sha256=sha256(page))])]})
            recipe_path = output / 'recipe.json'
            recipe = {'records': [dict(
                short='S1', record_id='synthetic-revenue', record_type='metric',
                subject='ExampleCo', indicator='revenue', period='2025', as_of='2025-12-31',
                selectors=[dict(page=1, start='Synthetic revenue', line=True)],
                detail=dict(scope='consolidated', scope_note='Synthetic group',
                            value_status='reported', value_note=''),
                number_specs=[dict(value='9999', unit='元')])]}
            dump(recipe_path, recipe)
            with self.assertRaisesRegex(ValueError, 'samples rejected'):
                build_samples(p1, output, recipe_path)
            diagnostic = output / 'sample-validation-errors.json'
            digest = sha256(diagnostic)
            archive = output / 'sample-rejections' / (digest + '.json')
            self.assertEqual(archive.read_bytes(), diagnostic.read_bytes())
            self.assertEqual(len(list(archive.parent.glob('*.json'))), 1)
            recipe['records'][0]['number_specs'][0]['value'] = '1250'
            dump(recipe_path, recipe)
            summary = build_samples(p1, output, recipe_path)
            self.assertEqual(summary['errors'], 0)
            self.assertEqual(summary['records'], 1)
            self.assertEqual(summary['historical_rejections'], 'sample-rejections/')
            self.assertIn('validated_at', summary)
            self.assertFalse(diagnostic.exists())
            self.assertEqual(sha256(archive), digest)
            self.assertEqual(len(list(archive.parent.glob('*.json'))), 1)
            history = rejection_history(output, summary)
            self.assertIn('synthetic-revenue', history)
            self.assertIn('value is absent from excerpts', history)
            self.assertIn('一次校验记录显示1条样例全部通过', history)

    def test_archive_deduplicates_and_preserves_old_diagnostic_time(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            diagnostic = output / 'sample-validation-errors.json'
            dump(diagnostic, {'synthetic-old': ['synthetic old reason']})
            rejected = datetime(2030, 1, 1, 2, 14, 42, tzinfo=timezone.utc).timestamp()
            os.utime(diagnostic, (rejected, rejected))
            archived = archive_sample_rejection(output)
            self.assertEqual(archived.stat().st_mtime, rejected)
            saved_time = archived.stat().st_mtime_ns
            self.assertEqual(archive_sample_rejection(output), archived)
            archive_sample_rejection(output, clear=True)
            self.assertEqual(archived.stat().st_mtime_ns, saved_time)
            self.assertFalse(diagnostic.exists())
            self.assertEqual(len(list(archived.parent.glob('*.json'))), 1)
            archived.write_text('{"synthetic-tampered": ["changed reason"]}')
            with self.assertRaisesRegex(ValueError, 'Archived sample diagnostic changed'):
                rejection_history(output, dict(records=1, errors=0,
                                               validated_at='2030-01-01T03:00:00+00:00'))

    def test_history_uses_latest_diagnostic_and_detects_later_repeat_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            diagnostic = output / 'sample-validation-errors.json'
            first_time = datetime(2030, 1, 1, 1, tzinfo=timezone.utc).timestamp()
            dump(diagnostic, {'synthetic-first': ['first synthetic reason']})
            os.utime(diagnostic, (first_time, first_time))
            first = archive_sample_rejection(output, clear=True)
            dump(diagnostic, {'synthetic-second': ['second synthetic reason']})
            os.utime(diagnostic, (first_time + 60, first_time + 60))
            archive_sample_rejection(output, clear=True)
            summary = dict(records=2, errors=0, validated_at='2030-01-01T02:00:00+00:00')
            dump(output / 'sample-validation.json', summary)
            history = rejection_history(output, summary)
            self.assertIn('synthetic-second', history)
            self.assertNotIn('synthetic-first', history)
            self.assertIn('2030-01-01T09:01:00+08:00', history)
            diagnostic.write_bytes(first.read_bytes())
            os.utime(diagnostic, (first_time + 120, first_time + 120))
            archive_sample_rejection(output, clear=True, record_attempt=True)
            history = rejection_history(output, summary)
            self.assertIn('synthetic-first', history)
            self.assertIn('2030-01-01T09:02:00+08:00', history)
            self.assertIn('attempts.jsonl', history)
            self.assertEqual(len(list(first.parent.glob('*.json'))), 2)
            dump(diagnostic, {'synthetic-current': ['still failing']})
            self.assertIn('尚无其后全部通过', rejection_history(output, summary))


if __name__ == '__main__':
    unittest.main()
