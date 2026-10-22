from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from corpus import normalized_sha256
from s1_review import (apply_manual_review, expand_metric_layout, mapping,
                       note_items, review_groups, select_excerpt)


class S1ReviewTests(unittest.TestCase):
    def test_counts_rows_not_cells_and_inherits_caption(self):
        rows = note_items('# Synthetic\n\n下表为模拟资料[S1 p1]。\n\n|项目|本期|上期|\n|---|---:|---:|\n|收入|123|98|\n\n- 一个模拟说明。\n')
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['columns'][1], {'name': '本期', 'text': '123'})
        self.assertIn('模拟资料', rows[0]['context'][0]['text'])

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


if __name__ == '__main__':
    unittest.main()
