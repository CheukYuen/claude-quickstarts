"""Synthetic documentation of current failed-call split leaves."""
from copy import deepcopy
import unittest

import s2
import s2_report
from run import sha, write_json
import test_s2_collect


class SplitChunksDocumentTests(unittest.TestCase):
    setUp = test_s2_collect.CollectionProvenanceTests.setUp
    make_chunk = test_s2_collect.CollectionProvenanceTests.make_chunk
    make_accepted = test_s2_collect.CollectionProvenanceTests.make_accepted

    def split(self, parent, children, reason):
        write_json(self.root / 'splits' / (parent['id'] + '.json'),
            dict(parent=parent, children=children, reason=reason,
                 prompt_template_sha256=sha(s2.HERE / 'prompts/extract-records.md')))

    def test_nested_current_leaves_metadata_and_idempotency_preserve_initial_plan(self):
        initial = '# Synthetic initial plan\n\nOriginal parents and exclusions remain here.\n'
        document = self.root / 'CHUNKS.md'
        document.write_text(initial)
        plan_bytes = (self.root / 'chunks.json').read_bytes()
        first = self.make_chunk(self.chunk['id'] + '.a', [self.fragment])
        second = self.make_chunk(self.chunk['id'] + '.b', [self.fragment])
        second['context_pages'] = [1]
        second['sections'] = ['Synthetic | section\ncontinued']
        nested_a = self.make_chunk(first['id'] + '.a', [self.fragment])
        nested_b = self.make_chunk(first['id'] + '.b', [self.fragment])
        self.split(self.chunk, [first, second], 'timed_out')
        self.split(first, [nested_a, nested_b], 'suspected_truncation')
        # Unreachable historical receipts and request inputs are not structural
        # leaves, even when their IDs or metadata look like real split rows.
        orphan = self.make_chunk('S46.orphan', [self.fragment])
        self.split(orphan, [deepcopy(second)], 'historical_only')
        request = self.make_chunk('S46.request', [self.fragment])
        write_json(self.root / 'request-plans' / 'one.json',
                   dict(chunk=request, request='Synthetic explicit request'))
        result = s2_report.update_split_chunks_markdown(self.root)
        rows = result['leaves']
        self.assertEqual([row['id'] for row in rows], [nested_a['id'], nested_b['id'], second['id']])
        self.assertEqual(rows[-1], dict(id=second['id'], source='S46', core_pages=[2], context_pages=[1],
            sections=second['sections'], chars=second['chars'], sha256=second['sha256'],
            split_parent=self.chunk['id'], reason='timed_out', receipt='splits/' + self.chunk['id'] + '.json'))
        self.assertEqual(rows[0]['split_parent'], first['id'])
        self.assertEqual(rows[0]['reason'], 'suspected_truncation')
        rendered = document.read_bytes()
        self.assertTrue(rendered.startswith(initial.encode()))
        text = rendered.decode()
        self.assertIn('不再用于有效抽取', text)
        self.assertIn('Synthetic &#124; section<br>continued', text)
        self.assertIn('[JSON](splits/' + first['id'] + '.json)', text)
        self.assertNotIn('historical_only', text)
        self.assertNotIn(request['id'], text)
        self.assertEqual(text.count(s2_report.SPLIT_SECTION_START), 1)
        self.assertEqual(text.count('## 失败后结构拆分'), 1)
        self.assertEqual(s2_report.update_split_chunks_markdown(self.root), result)
        self.assertEqual(document.read_bytes(), rendered)
        self.assertEqual((self.root / 'chunks.json').read_bytes(), plan_bytes)

    def test_later_split_replaces_previous_leaf_rows_in_same_section(self):
        document = self.root / 'CHUNKS.md'
        document.write_text('Initial plan without trailing newline')
        first = self.make_chunk(self.chunk['id'] + '.a', [self.fragment])
        second = self.make_chunk(self.chunk['id'] + '.b', [self.fragment])
        self.split(self.chunk, [first, second], 'timed_out')
        s2_report.update_split_chunks_markdown(self.root)
        nested = [self.make_chunk(second['id'] + suffix, [self.fragment]) for suffix in ('.a', '.b')]
        self.split(second, nested, 'timed_out')
        result = s2_report.update_split_chunks_markdown(self.root)
        self.assertEqual([row['id'] for row in result['leaves']], [first['id']] + [row['id'] for row in nested])
        text = document.read_text()
        self.assertEqual(text.count('## 失败后结构拆分'), 1)
        self.assertNotIn('\n|' + second['id'] + '|', text)
        self.assertIn('父块`' + second['id'] + '`', text)
        self.assertTrue(text.startswith('Initial plan without trailing newline\n\n'))

    def test_no_split_is_explicit_and_does_not_invent_an_initial_document(self):
        result = s2_report.update_split_chunks_markdown(self.root)
        self.assertFalse(result['chunks_document_updated'])
        self.assertFalse((self.root / 'CHUNKS.md').exists())
        document = self.root / 'CHUNKS.md'
        document.write_text('Initial synthetic plan\n')
        result = s2_report.update_split_chunks_markdown(self.root)
        self.assertTrue(result['chunks_document_updated'])
        self.assertEqual(result['leaves'], [])
        self.assertIn('暂无失败后的结构拆分', document.read_text())
        original = document.read_bytes()
        s2_report.update_split_chunks_markdown(self.root)
        self.assertEqual(document.read_bytes(), original)

    def test_malformed_owned_markers_fail_without_rewriting_initial_plan(self):
        document = self.root / 'CHUNKS.md'
        for original in ('Initial\n' + s2_report.SPLIT_SECTION_START,
                         s2_report.SPLIT_SECTION_END + '\nInitial\n' + s2_report.SPLIT_SECTION_START):
            document.write_text(original)
            with self.subTest(original=original), self.assertRaisesRegex(ValueError, 'Malformed'):
                s2_report.update_split_chunks_markdown(self.root)
            self.assertEqual(document.read_text(), original)


if __name__ == '__main__':
    unittest.main()
