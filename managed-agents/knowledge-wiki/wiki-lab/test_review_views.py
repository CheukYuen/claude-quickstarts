"""Real Git fixtures for explicit rejected-view acceptance; no model calls."""
from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

from corpus import CUTOFF, P1_IDS
import review_views
from run import git, hashes, sha, write_json


class ViewReviewTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='review-fixture-', dir=review_views.HERE / 'runs')
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.wiki = self.root / 'wiki'
        (self.wiki / 'sources').mkdir(parents=True)
        (self.wiki / 'sources/S01.c01.md').write_text('公司:yofc\n原始事实 [S01 p1]\n')
        self.sources = [{'id': f'S{index:02d}', 'source_id': sid, 'date': '2026-08-22', 'pages': [1]}
                        for index, sid in enumerate(P1_IDS, 1)]
        write_json(self.root / 'corpus-manifest.json', {'scope': 'p1', 'cutoff': CUTOFF, 'sources': self.sources})
        write_json(self.root / 'chunks.json', [])
        write_json(self.wiki / '_sources.json', self.sources)
        git(self.wiki, 'init', '-b', 'main')
        git(self.wiki, 'add', '.')
        git(self.wiki, '-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-m', 'Accepted fixture')
        self.head = git(self.wiki, 'rev-parse', 'HEAD')
        self.before = hashes(self.wiki)
        write_json(self.root / 'source-note-hashes.json', {'sources/S01.c01.md': self.before['sources/S01.c01.md']})
        write_json(self.root / 'accepted/aggregate.json', {'commit': self.head, 'outputs': self.before})
        git(self.wiki, 'branch', 'rejected/fixture-retained')

    def rejected_attempt(self, stage='company-yofc', missing_page=False, entity_count=0):
        workspace = self.root / 'work' / stage / 'attempt-01'
        shutil.copytree(self.wiki, workspace, ignore=shutil.ignore_patterns('.git'))
        input_hashes = hashes(workspace)
        allowed, required = review_views.stage_policy(stage)
        if stage == 'entities':
            for index in range(entity_count):
                slug = f'entity-{index}'
                path = workspace / 'entities' / (slug + '.md')
                metadata = {'name': slug, 'canonical': f'Entity {index}', 'type': 'company',
                            'aliases': [], 'relations': [], 'candidate_links': []}
                import json
                path.parent.mkdir(exist_ok=True)
                path.write_text('---\n' + json.dumps(metadata) + '\n---\n- Fact [S01 p1]\n')
        else:
            for relative in required:
                if missing_page and relative.endswith('metrics.md'):
                    continue
                path = workspace / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                text = '# P1范围\n公司:yofc\n事实 [S01 p1]\n'
                if relative.endswith('gaps.md'):
                    text += '无新增缺口。\n'
                if relative.endswith('overview.md') and not missing_page:
                    text += '本组不包含目标价。\n'
                path.write_text(text)
        record = {'stage': stage, 'attempt': 1, 'status': 'checks_rejected', 'exit_code': 0,
                  'completed': True, 'workspace': str(workspace), 'allowed_paths': allowed,
                  'input_hashes': input_hashes, 'output_hashes': hashes(workspace), 'path_errors': [],
                  'checks': {'ok': False, 'errors': ['Retained original rejection']}}
        record_path = self.root / 'logs' / stage / 'attempt-01/record.json'
        write_json(record_path, record)
        return workspace, record_path

    def test_accepts_exact_correction_and_preserves_original_receipts_and_sources(self):
        original, record = self.rejected_attempt()
        original_hashes, record_hash = hashes(original), sha(record)
        rejected_head = git(self.wiki, 'rev-parse', 'rejected/fixture-retained')
        candidate = review_views.apply_view_review(self.root, 'company-yofc', {
            'reason': 'Remove a nonnumeric exclusion sentence after retained rejection',
            'replace': [{'path': 'companies/yofc/overview.md', 'old': '本组不包含目标价。',
                         'new': '本组仅覆盖已列资料。'}]})
        self.assertTrue(candidate.is_dir())
        self.assertEqual(hashes(original), original_hashes)
        self.assertEqual(sha(record), record_hash)
        self.assertEqual(sha(self.wiki / 'sources/S01.c01.md'), self.before['sources/S01.c01.md'])
        self.assertEqual(git(self.wiki, 'rev-parse', 'rejected/fixture-retained'), rejected_head)
        self.assertNotEqual(git(self.wiki, 'rev-parse', 'HEAD'), self.head)
        self.assertEqual(git(self.wiki, 'status', '--porcelain'), '')
        self.assertTrue((self.root / 'accepted/company-yofc.json').is_file())
        self.assertTrue((self.root / 'accepted/review-company-yofc-01.json').is_file())
        self.assertTrue((self.root / 'reviews/review-company-yofc-01.edits.patch').is_file())

    def test_incomplete_company_candidate_never_reaches_main(self):
        self.rejected_attempt(missing_page=True)
        with self.assertRaisesRegex(ValueError, 'required stage output'):
            review_views.apply_view_review(self.root, 'company-yofc', {'reason': 'Recheck draft', 'replace': []})
        self.assertEqual(hashes(self.wiki), self.before)
        self.assertEqual(git(self.wiki, 'rev-parse', 'HEAD'), self.head)
        self.assertFalse((self.root / 'accepted/company-yofc.json').exists())
        self.assertTrue((self.root / 'reviews/review-company-yofc-01.json').is_file())

    def test_completed_view_second_review_preserves_first_and_later_accepted_changes(self):
        import json
        original, record_path = self.rejected_attempt()
        overview = original / 'companies/yofc/overview.md'
        overview.write_text(overview.read_text().replace('本组不包含目标价。', '最初范围表述。'))
        record = json.loads(record_path.read_text())
        record.update(status='completed', checks={'ok': True, 'errors': []}, output_hashes=hashes(original))
        write_json(record_path, record)
        original_hashes, record_hash = hashes(original), sha(record_path)
        allowed, _ = review_views.stage_policy('company-yofc')
        for relative in original_hashes:
            if review_views.path_allowed(relative, allowed):
                target = self.wiki / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(original / relative, target)
        git(self.wiki, 'add', '.')
        git(self.wiki, '-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-m', 'Accepted model stage')
        marker = self.root / 'accepted/company-yofc.json'
        write_json(marker, {'commit': git(self.wiki, 'rev-parse', 'HEAD'),
                           'outputs': {path: sha(self.wiki / path) for path in original_hashes
                                       if review_views.path_allowed(path, allowed)}})
        marker_bytes = marker.read_bytes()
        review_views.apply_view_review(self.root, 'company-yofc', {
            'reason': 'First bounded semantic correction',
            'replace': [{'path': 'companies/yofc/overview.md', 'old': '最初范围表述。', 'new': '第一次修正范围。'}]})
        # A later accepted resolver-style change must survive a second review.
        metrics = self.wiki / 'companies/yofc/metrics.md'
        metrics.write_text(metrics.read_text() + '后续口径修正保留 [S01 p1]\n')
        git(self.wiki, 'add', '.')
        git(self.wiki, '-c', 'user.name=fixture', '-c', 'user.email=fixture@local', 'commit', '-m', 'Later accepted correction')
        write_json(self.root / 'accepted/resolve-fixture.json', {
            'commit': git(self.wiki, 'rev-parse', 'HEAD'),
            'outputs': {'companies/yofc/metrics.md': sha(metrics)}})
        metrics_hash = sha(metrics)
        review_views.apply_view_review(self.root, 'company-yofc', {
            'reason': 'Second correction must start from the current accepted view',
            'replace': [{'path': 'companies/yofc/overview.md', 'old': '第一次修正范围。', 'new': '第二次修正范围。'}]})
        self.assertIn('第二次修正范围。', (self.wiki / 'companies/yofc/overview.md').read_text())
        self.assertEqual(sha(metrics), metrics_hash)
        self.assertEqual(marker.read_bytes(), marker_bytes)
        self.assertEqual(sha(record_path), record_hash)
        self.assertEqual(hashes(original), original_hashes)
        audit = json.loads((self.root / 'reviews/review-company-yofc-02.json').read_text())
        self.assertEqual(audit['review_base_mode'], 'accepted_current_view')
        self.assertEqual(git(self.wiki, 'status', '--porcelain'), '')

    def test_changed_model_original_is_rejected_before_copy_or_writeback(self):
        original, _ = self.rejected_attempt()
        (original / 'companies/yofc/overview.md').write_text('Changed after receipt [S01 p1]\n')
        with self.assertRaisesRegex(ValueError, 'changed after its recorded final hash'):
            review_views.apply_view_review(self.root, 'company-yofc', {'reason': 'Recheck', 'replace': []})
        self.assertEqual(hashes(self.wiki), self.before)
        self.assertEqual(git(self.wiki, 'rev-parse', 'HEAD'), self.head)

    def test_entity_review_cannot_bypass_sixteen_page_budget(self):
        self.rejected_attempt(stage='entities', entity_count=17)
        with self.assertRaisesRegex(ValueError, 'between 1 and 16'):
            review_views.apply_view_review(self.root, 'entities', {'reason': 'Recheck entities', 'replace': []})
        self.assertEqual(hashes(self.wiki), self.before)
        self.assertEqual(git(self.wiki, 'rev-parse', 'HEAD'), self.head)


if __name__ == '__main__':
    unittest.main()
