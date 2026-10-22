"""S2 structural chunking tests use synthetic text only."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import s2_chunks as chunks


def page(number, text):
    return {'page': number, 'text': text,
            'text_sha256': hashlib.sha256(text.encode()).hexdigest()}


def report_text():
    text = '封面\n第二节 公司简介和主要财务指标 ........ 5\n'
    for number, title in zip(chunks.CHINESE_NUMBERS, chunks.SECTION_NAMES):
        text += '第%s节 %s\n合成内容。\n' % (number, title)
    for i, (number, title) in enumerate(zip(chunks.CHINESE_NUMBERS, chunks.NOTE_NAMES)):
        text += '%s、%s\n' % (number, title)
        if i == 1:
            text += '一、营业总收入 100 200\n三、营业利润 30 40\n'
        elif i == 6:
            text += '1、 货币资金\n项目 期末余额 期初余额\n合成金额 123 456\n'
            text += '2、 其他\n合成内容\n'
        else:
            text += '合成内容。\n'
    return text


class StructuralHeadingTests(unittest.TestCase):
    def test_exact_titles_ignore_contents_and_table_rows(self):
        text = report_text()
        units, excluded = chunks.periodic_units([page(1, text)])
        self.assertEqual(len(excluded), 1)
        omitted = ''.join(f['text'] for f in excluded[0]['fragments'])
        self.assertTrue(omitted.startswith('二、财务报表'))
        self.assertNotIn('三、公司基本情况', omitted)
        self.assertEqual([u['note_number'] for u in units if 'note_number' in u], [1, 2])
        ranges = sorted([(u['start'], u['end']) for u in units] +
                        [(f['start'], f['end']) for f in excluded[0]['fragments']])
        self.assertEqual(ranges[0][0], 0)
        self.assertEqual(ranges[-1][1], len(text))
        self.assertTrue(all(a[1] == b[0] for a, b in zip(ranges, ranges[1:])))

    def test_missing_or_duplicate_boundary_stops(self):
        for text in (report_text().replace('六、税项\n', '税项\n'),
                     report_text() + '\n六、税项\n'):
            with self.assertRaises(chunks.BoundaryError):
                chunks.periodic_units([page(1, text)])

    def test_discontinuous_numbered_notes_stop(self):
        text = report_text().replace('2、 其他', '3、 其他')
        with self.assertRaisesRegex(chunks.BoundaryError, 'Discontinuous'):
            chunks.periodic_units([page(1, text)])

    def test_boundary_inside_page_preserves_only_allowed_span(self):
        text = report_text()
        boundary = text.index('二、财务报表') + 5
        pages = [page(1, text[:boundary]), page(2, text[boundary:])]
        units, excluded = chunks.periodic_units(pages)
        self.assertEqual(excluded[0]['fragments'][0]['page'], 1)
        self.assertEqual(excluded[0]['fragments'][1]['page'], 2)
        self.assertEqual(excluded[0]['fragments'][0]['text'], '二、财务报表'[:5])
        # This fixture intentionally breaks a heading across physical pages;
        # the page text flattening still retains the exact frozen sequence.
        self.assertTrue(any(u['title'].startswith('三、') for u in units))


class PackingTests(unittest.TestCase):
    def source(self, root, texts, source_id='S46'):
        records = []
        for n, text in enumerate(texts, 1):
            path = root / ('page%d.txt' % n)
            path.write_bytes(text.encode())
            records.append({'page': n, 'corpus_path': str(path), 'chars': len(text),
                            'text_sha256': hashlib.sha256(text.encode()).hexdigest()})
        return {'id': source_id, 'source_id': 'synthetic-source', 'date': '2026-01-01',
                'pdf_sha256': 'a' * 64, 'pdf_page_count': len(records),
                'chars': sum(len(text) for text in texts),
                'page_records': records, 'pages': list(range(1, len(records) + 1))}

    def test_overlong_item_splits_on_page_and_carries_header(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            texts = ['项目 期末余额 期初余额\n123\n', '456\n' * 5, '789\n' * 5]
            source = self.source(root, texts)
            result = chunks.build_chunks({'sources': [source]}, root, max_chars=25)
            self.assertEqual(len(result), 3)
            self.assertEqual(result[1]['context_pages'], [1])
            self.assertEqual(result[2]['context_pages'], [1])
            self.assertEqual(result[1]['context_reasons'], ['verified_continuing_table_header'])
            self.assertEqual(''.join(f['text'] for c in result for f in c['fragments']), ''.join(texts))
            self.assertTrue(all(c['chars'] <= 25 for c in result))

    def test_single_overlong_page_is_explicit_not_cut(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root, ['合成文字' * 20])
            result = chunks.build_chunks({'sources': [source]}, root, max_chars=20)
            self.assertTrue(result[0]['single_page_over_limit'])
            self.assertEqual(len(result), 1)
            with self.assertRaises(chunks.BoundaryError):
                chunks.split_chunk(result[0], source, root)

    def test_retry_bisects_saved_structure_then_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root, ['header 项目 本期\n123\n', '456\n', '789\n'])
            original = chunks.build_chunks({'sources': [source]}, root, max_chars=200)[0]
            first, second = chunks.split_chunk(original, source, root)
            self.assertEqual(first['split_boundary_kind'], 'page_boundary')
            self.assertEqual(second['context_pages'], [1])
            self.assertNotEqual(first['sha256'], original['sha256'])
            # Saved structures take precedence over character/page midpoint.
            original['units'] = [{**original['units'][0], 'fragments': original['fragments'][:1]},
                                 {**original['units'][0], 'fragments': original['fragments'][1:]}]
            first, second = chunks.split_chunk(original, source, root)
            self.assertEqual(first['split_boundary_kind'], 'saved_structure_boundary')
            self.assertEqual(first['core_pages'], [1])

    def test_changed_frozen_page_stops_before_chunking(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.source(root, ['synthetic'])
            Path(source['page_records'][0]['corpus_path']).write_text('changed')
            with self.assertRaisesRegex(ValueError, 'hash changed'):
                chunks.build_chunks({'sources': [source]}, root)

    def test_narrative_boundary_does_not_borrow_old_table_header(self):
        texts = ['项目  本期  上期\n金额  123  456\n这是已经离开表格的合成说明。\n',
                 '这是继续的合成说明。\n']
        before = [{'page': 1, 'start': 0, 'end': len(texts[0]), 'text': texts[0]}]
        after = {'page': 2, 'text': texts[1]}
        context, reason = chunks._continuation_header(before, after, [page(1, texts[0]), page(2, texts[1])])
        self.assertEqual(context, [])
        self.assertEqual(reason, 'boundary_not_inside_table')

    def test_new_header_on_core_page_needs_no_unrelated_context(self):
        texts = ['项目  本期  上期\n金额  123  456\n',
                 'Investment rating  Company  BofA Ticker  Bloomberg symbol  Analyst\nBUY  Example  EXAM  EX CH  Author\n']
        before = [{'page': 1, 'start': 0, 'end': len(texts[0]), 'text': texts[0]}]
        context, reason = chunks._continuation_header(before, {'page': 2, 'text': texts[1]},
                                                     [page(1, texts[0]), page(2, texts[1])])
        self.assertEqual(context, [])
        self.assertEqual(reason, 'next_core_contains_table_header')

    def test_unverifiable_table_continuation_stops(self):
        text = '甲  123  456\n'
        before = [{'page': 1, 'start': 0, 'end': len(text), 'text': text}]
        with self.assertRaisesRegex(chunks.BoundaryError, 'lacks an unbroken'):
            chunks._continuation_header(before, {'page': 2, 'text': '乙  789  100\n'},
                                        [page(1, text), page(2, '乙  789  100\n')])

    def test_adaptive_page_split_carries_whole_header_page(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            header_page = '合成前置文字\n项目 期末余额 期初余额\n123\n'
            source = self.source(root, [header_page, '456\n'])
            original = chunks.build_chunks({'sources': [source]}, root, max_chars=200)[0]
            first_fragment = original['units'][0]['fragments'][0]
            first_fragment['start'] = header_page.index('123')
            first_fragment['text'] = '123\n'
            _, second = chunks.split_chunk(original, source, root)
            context = second['context_fragments'][0]
            self.assertEqual(context['page'], 1)
            self.assertEqual(context['start'], 0)
            self.assertEqual(context['text'], header_page)
            self.assertEqual(context['end'], len(header_page))

    def test_page_comparison_detects_missing_or_changed_page(self):
        with tempfile.TemporaryDirectory() as directory:
            source = self.source(Path(directory), ['one', 'two'])
            original = {'sources': [source]}
            changed = copy.deepcopy(original)
            self.assertEqual(chunks.compare_pages(original, original)['pages'], 2)
            changed['sources'][0]['page_records'][1]['text_sha256'] = 'x' * 64
            with self.assertRaisesRegex(ValueError, 'per-page hashes differ'):
                chunks.compare_pages(changed, original)

    def test_rechunk_preserves_prior_manifests_and_chunk_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / 'runs/s2-synthetic'
            root.mkdir(parents=True)
            source = self.source(root, ['项目  本期  上期\n甲  1  2\n', '乙  3  4\n'])
            manifest = {'sources': [source]}
            (root / 'corpus-manifest.json').write_text(json.dumps(manifest))
            (root / 'source-registry.json').write_text('{}')
            old = chunks.build_chunks(manifest, root, max_chars=25)
            old_manifest = (root / 'chunks.json').read_bytes()
            old_hash = hashlib.sha256(old_manifest).hexdigest()
            old_file = Path(old[0]['path']).read_bytes()
            with mock.patch.object(chunks, 'HERE', base), mock.patch.object(chunks.corpus, 'verify_prepared'):
                new = chunks.rechunk(root, max_chars=80, reason='synthetic cap revision', protected_ids=())
            self.assertEqual(len(new), 1)
            history = root / 'chunk-history' / old_hash
            self.assertEqual((history / 'chunks.json').read_bytes(), old_manifest)
            self.assertEqual((history / 'corpus/chunks/S46.s01.md').read_bytes(), old_file)
            self.assertEqual(json.loads((root / 'RECHUNK-REPORT.json').read_text())['status'], 'installed')

    def test_rechunk_stops_before_changing_a_pilot_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / 'runs/s2-synthetic'
            root.mkdir(parents=True)
            source = self.source(root, ['项目  本期  上期\n甲  1  2\n', '乙  3  4\n'])
            manifest = {'sources': [source]}
            (root / 'corpus-manifest.json').write_text(json.dumps(manifest))
            (root / 'source-registry.json').write_text('{}')
            old = chunks.build_chunks(manifest, root, max_chars=25)
            old_manifest = (root / 'chunks.json').read_bytes()
            old_file = Path(old[0]['path']).read_bytes()
            with mock.patch.object(chunks, 'HERE', base), mock.patch.object(chunks.corpus, 'verify_prepared'):
                with self.assertRaisesRegex(chunks.BoundaryError, 'pilot input hashes'):
                    chunks.rechunk(root, max_chars=80, reason='synthetic cap revision', protected_ids=('S46.s01',))
            self.assertEqual((root / 'chunks.json').read_bytes(), old_manifest)
            self.assertEqual(Path(old[0]['path']).read_bytes(), old_file)
            self.assertEqual(json.loads((root / 'rechunk-attempts/attempt-001/RECHUNK-REPORT.json').read_text())['status'], 'rejected')

    def test_rechunk_refuses_frozen_run(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(chunks, 'HERE', Path(directory)):
                with self.assertRaisesRegex(ValueError, 'runs/s2'):
                    chunks.rechunk(Path(directory) / 'runs/p1-synthetic', reason='synthetic')

    def test_preview_rechunk_preserves_active_inputs_without_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            root = base / 'runs/s2-synthetic'
            root.mkdir(parents=True)
            source = self.source(root, ['项目  本期  上期\n甲  1  2\n', '乙  3  4\n'])
            manifest = {'sources': [source]}
            (root / 'corpus-manifest.json').write_text(json.dumps(manifest))
            (root / 'source-registry.json').write_text('{}')
            old = chunks.build_chunks(manifest, root, max_chars=25)
            old_manifest = (root / 'chunks.json').read_bytes()
            old_file = Path(old[0]['path']).read_bytes()
            with mock.patch.object(chunks, 'HERE', base), mock.patch.object(chunks.corpus, 'verify_prepared'):
                new = chunks.preview_rechunk(root, max_chars=80, reason='synthetic preview', protected_ids=())
            self.assertEqual(len(new), 1)
            self.assertEqual((root / 'chunks.json').read_bytes(), old_manifest)
            self.assertEqual(Path(old[0]['path']).read_bytes(), old_file)
            self.assertFalse((root / 'chunk-history').exists())
            self.assertFalse((root / 'RECHUNK-REPORT.json').exists())
            report = json.loads((root / 'rechunk-attempts/attempt-001/RECHUNK-REPORT.json').read_text())
            self.assertEqual(report['status'], 'previewed')
            self.assertTrue(report['active_manifest_unchanged'])
            self.assertNotEqual(new[0]['path'], old[0]['path'])


if __name__ == '__main__':
    unittest.main()
