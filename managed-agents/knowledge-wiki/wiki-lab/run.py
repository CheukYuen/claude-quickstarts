#!/usr/bin/env python3
"""P1 only: fresh CLI calls, resumable chunks, checked staging, independent wiki Git."""
from __future__ import annotations
import argparse
import concurrent.futures
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
# Reuse the spike's event/usage observer; do not execute its pipeline.
sys.path.insert(0, str(HERE.parent / 'codex-spike'))
_spec = importlib.util.spec_from_file_location('spike_runtime', HERE.parent / 'codex-spike/run.py')
_spike = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_spike)
from checks import check_changes, check_extraction, check_resolutions, check_wiki, path_allowed


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def load(path: Path):
    return json.loads(path.read_text())


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def hashes(root: Path) -> dict:
    result = {}
    for p in sorted(root.rglob('*')):
        if '.git' in p.relative_to(root).parts:
            continue
        if p.is_symlink():
            raise ValueError(f'Symlink rejected: {p}')
        if p.is_file():
            result[p.relative_to(root).as_posix()] = sha(p)
    return result


def template(name: str, **kwargs) -> str:
    text = (HERE / 'prompts' / f'{name}.md').read_text()
    for k, v in kwargs.items():
        text = text.replace('{{' + k + '}}', str(v))
    return text


def git(root: Path, *args: str) -> str:
    return subprocess.check_output(['git', '-C', str(root), *args], text=True, stderr=subprocess.STDOUT).strip()


def require(check: dict) -> None:
    if not check['ok']:
        raise ValueError('\n'.join(check['errors']))


def record_check(record: dict, check: dict) -> None:
    log = Path(record['log'])
    write_json(log / 'checks.json', check)
    saved = load(log / 'record.json')
    saved['checks'] = check
    if not check['ok']:
        saved['status'] = 'checks_rejected'
    write_json(log / 'record.json', saved)


class Pipeline:
    def __init__(self, root: Path, args):
        self.root, self.args = root, args
        self.manifest = load(root / 'corpus-manifest.json')
        self.sources = self.manifest['sources']
        from corpus import CUTOFF, P1_IDS
        if (self.manifest.get('scope') != 'p1' or self.manifest.get('cutoff') != CUTOFF or
                {s['source_id'] for s in self.sources} != set(P1_IDS) or len(self.sources) != len(P1_IDS)):
            raise ValueError('This runner is limited to the five-source P1 sample; P2 is not enabled')
        self.chunks = load(root / 'chunks.json')
        self.wiki = root / 'wiki'
        self.lock = threading.Lock()
        self.link_roots = [root / 'corpus', Path('/Users/leon/Stock/invest-sources')]

    def call(self, stage: str, workspace: Path, task: str, allowed: list[str]) -> dict:
        # Make CLI/git discovery stop at the disposable workspace, not the outer repo.
        if not (workspace / '.git').exists():
            git(workspace, 'init', '-b', 'stage-workspace')
        logs = self.root / 'logs' / stage
        logs.mkdir(parents=True, exist_ok=True)
        attempt = len(list(logs.glob('attempt-*'))) + 1
        log = logs / f'attempt-{attempt:02d}'
        log.mkdir()
        (log / 'prompt.md').write_text(task)
        before = hashes(workspace)
        command = [self.args.codex, 'exec', '--ephemeral', '--json', '--color', 'never',
                   '--sandbox', 'workspace-write', '--disable', 'memories', '--disable', 'apps',
                   '--disable', 'plugins', '--disable', 'multi_agent',
                   '--config', 'approval_policy="never"', '-C', str(workspace),
                   '--output-last-message', str(log / 'final.md'), '-']
        record = {'stage': stage, 'attempt': attempt, 'workspace': str(workspace), 'command': command,
                  'prompt_sha256': sha(log / 'prompt.md'), 'input_hashes': before, 'status': 'running',
                  'amount': None, 'billing_information': None, 'model': 'existing CLI configuration',
                  'allowed_paths': allowed, 'started_at': time.time()}
        write_json(log / 'record.json', record)
        print(f'START {stage} attempt {attempt}', flush=True)
        started = time.monotonic()
        with (log / 'events.jsonl').open('w') as out, (log / 'stderr.log').open('w') as err:
            proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=out, stderr=err,
                                    text=True, start_new_session=True)
            try:
                proc.communicate(task, timeout=self.args.stage_timeout)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                record['status'] = 'timed_out'
        try:
            record.update(_spike.inspect_events(log / 'events.jsonl'))
        except (ValueError, OSError) as error:
            record.update(completed=False, usage={}, tools=[], event_parse_error=str(error))
        record.update(seconds=round(time.monotonic()-started, 2), exit_code=proc.returncode)
        try:
            after = hashes(workspace)
        except ValueError as error:
            record.update(status='unsafe_artifact', artifact_error=str(error))
            write_json(log / 'record.json', record)
            raise
        record['output_hashes'] = after
        record['changed'] = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
        record['path_errors'] = check_changes(before, after, allowed)
        record['path_errors'].extend('Deletion is not permitted: '+p for p in before.keys()-after.keys())
        if record['status'] != 'timed_out':
            record['status'] = 'completed' if proc.returncode == 0 and record['completed'] else 'failed'
        if record['path_errors']:
            record['status'] = 'rejected_paths'
        write_json(log / 'record.json', record)
        print(f'END {stage}: {record["status"]} {record["seconds"]}s', flush=True)
        if record['status'] != 'completed':
            raise RuntimeError(f'{stage}: {record["status"]}; {log}')
        return record | {'log': str(log)}

    def extract(self, chunk: dict) -> None:
        key = chunk['id']
        source_path = Path(chunk['path'])
        if sha(source_path) != chunk['sha256']:
            raise ValueError(f'Chunk input changed: {source_path}')
        state_path = self.root / 'extract' / key / 'success.json'
        prompt = template('extract', CHUNK_ID=key)
        binding = {'input_sha256': chunk['sha256'], 'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                   'source_bindings': self.manifest.get('manifest_sha256', self.manifest.get('manifest_hash')),
                   'chunk': chunk}
        if state_path.exists():
            state = load(state_path)
            out = Path(state['workspace'])
            if state['binding'] != binding or hashes(out) != state['hashes']:
                raise RuntimeError(f'Success binding changed: {key}; use a new run')
            require(check_extraction(out, chunk, self.sources))
            print(f'SKIP {key}: checked success', flush=True)
            return
        attempts = self.root / 'extract' / key
        attempts.mkdir(parents=True, exist_ok=True)
        previous = sorted(attempts.glob('attempt-*'))
        work = attempts / f'attempt-{len(previous)+1:02d}'
        (work / 'input').mkdir(parents=True)
        shutil.copyfile(source_path, work / 'input/source.md')
        write_json(work / 'input/chunk.json', chunk)
        allowed = [f'{d}/{key}.{ext}' for d, ext in [('sources','md'),('mentions','json'),('gaps','json'),('coverage','json'),('market','md')]]
        if previous:
            # Incomplete attempts remain untouched; continue their bounded artifacts
            # in a new workspace, with fresh immutable source input.
            for rel in allowed:
                prior = previous[-1] / rel
                if prior.is_file() and not prior.is_symlink():
                    (work / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(prior, work / rel)
            prompt += '\n重试说明：当前目录可能已有上次未完成或未通过检查的部分产物。请对照input原文补齐、修正并检查全部四类产物；不要把已有内容当作已验收来源。保留正确的重要内容，避免无意义地从头改写。\n'
        feedback = self.root / 'reviews' / (key + '.md')
        if feedback.exists():
            prompt += '\n本轮原文回读反馈（须回input核对后落实）：\n' + feedback.read_text()
        record = self.call('extract-' + key, work, prompt, allowed)
        check = check_extraction(work, chunk, self.sources)
        record_check(record, check)
        require(check)
        write_json(state_path, {'binding': binding, 'workspace': str(work), 'hashes': hashes(work), 'checks': check,
                                'actual_prompt_sha256': record['prompt_sha256'], 'log': record['log']})

    def commit(self, message: str) -> str:
        git(self.wiki, 'add', '.')
        git(self.wiki, '-c', 'user.name=wiki-lab', '-c', 'user.email=wiki-lab@local',
            'commit', '--allow-empty', '-m', message)
        return git(self.wiki, 'rev-parse', 'HEAD')

    def assert_accepted_wiki(self) -> None:
        """Bind resumable views to clean, accepted Git commits, including later edits."""
        with self.lock:
            if git(self.wiki,'rev-parse','--abbrev-ref','HEAD') != 'main':
                raise ValueError('Resume only the accepted wiki main branch')
            if git(self.wiki,'status','--porcelain'):
                raise ValueError('Wiki has uncommitted changes; preserve and review them before resuming')
            states = [load(p) for p in (self.root/'accepted').glob('*.json')]
            if not states:
                raise ValueError('No accepted Git binding for this wiki')
            head = git(self.wiki,'rev-parse','HEAD')
            for state in states:
                try:
                    git(self.wiki,'merge-base','--is-ancestor',state['commit'],head)
                except subprocess.CalledProcessError as error:
                    raise ValueError('Wiki no longer contains the accepted commit chain') from error
            latest = max(states,key=lambda s:int(git(self.wiki,'rev-list','--count',s['commit'])))
            if head != latest['commit']:
                raise ValueError('Wiki HEAD is not the latest accepted stage commit')

    def aggregate(self) -> None:
        if self.wiki.exists():
            if not (self.root / 'source-note-hashes.json').exists():
                raise RuntimeError('Incomplete aggregate; preserve wiki for inspection and use a new run')
            require(check_wiki(self.wiki, self.sources, immutable=load(self.root/'source-note-hashes.json'), link_roots=self.link_roots))
            return
        # Collect in staging so incomplete extraction can never create a main wiki.
        aggregate = self.root / 'work' / 'aggregate'
        aggregate.mkdir(parents=True, exist_ok=True)
        names = {}
        for chunk in self.chunks:
            # Resume verification also guards aggregation from post-check edits.
            self.extract(chunk)
            state = load(self.root / 'extract' / chunk['id'] / 'success.json')
            work = Path(state['workspace'])
            require(check_extraction(work, chunk, self.sources))
            for d in ('sources', 'mentions', 'gaps', 'coverage', 'market'):
                for p in (work / d).glob('*'):
                    target = aggregate / d / p.name
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(p, target)
            for m in load(work / 'mentions' / (chunk['id'] + '.json')):
                names.setdefault(m['name'], []).append({'chunk_id': chunk['id'], **m})
        write_json(aggregate / '_sources.json', self.sources)
        write_json(aggregate / '_mentions_index.json', names)
        lines = ['# 原文核对入口', '', '引用使用PDF页序；逐页文字为原文副本，原件链接指向只读来源库。', '']
        for s in self.sources:
            lines += [f'## {s["id"]} · {s["title"]}', '', f'披露日：{s["date"]}；source_id：`{s["source_id"]}`', '',
                      f'绑定记录：{s["record_binding"]["version"]}，清单第{s["record_binding"]["line"]}行；原件SHA-256：`{s["pdf_sha256"]}`。', '',
                      f'[原件 PDF]({s["pdf_path"]})', '']
            lines += [f'原件共{s["pdf_page_count"]}页，本样本选用{len(s["pages"])}页。', '']
            if s.get('missing_text_pages'):
                lines += ['缺少已有逐页文字的PDF页序：'+', '.join(map(str,s['missing_text_pages']))+'；这些页本轮未读。', '']
            for p in s['page_records']:
                path = p.get('copied_path') or p.get('corpus_path') or p.get('path') or p.get('text_path')
                lines.append(f'- [{s["id"]} p{p["page"]}]({path})')
            lines.append('')
        (aggregate / '_sources.md').write_text('\n'.join(lines))
        require(check_wiki(aggregate, self.sources, link_roots=self.link_roots))
        shutil.copytree(aggregate, self.wiki)
        git(self.wiki, 'init', '-b', 'main')
        commit = self.commit('P1: collect immutable chunk notes and source bindings')
        write_json(self.root / 'source-note-hashes.json', {k:v for k,v in hashes(self.wiki).items() if k.startswith(('sources/','mentions/','gaps/','coverage/'))})
        write_json(self.root/'accepted/aggregate.json',{'commit':commit,'outputs':hashes(self.wiki)})

    def reject(self, stage: str, candidate: Path, errors: list[str]) -> None:
        write_json(candidate / '_rejection.json', {'stage': stage, 'errors': errors})
        git(candidate, 'init', '-b', 'candidate')
        git(candidate, 'add', '.')
        git(candidate, '-c', 'user.name=wiki-lab', '-c', 'user.email=wiki-lab@local', 'commit', '-m', 'Rejected ' + stage)
        ref = 'rejected/' + stage + '-' + str(time.time_ns())
        git(self.wiki, 'fetch', str(candidate), f'HEAD:refs/heads/{ref}')

    def stage(self, name: str, allowed: list[str], task: str, *, tag: str | None = None,
              raw: list[tuple[str,int]] | None = None, resolve: bool = False) -> None:
        marker = self.root / 'accepted' / (name + '.json')
        if marker.exists():
            self.assert_accepted_wiki()
            accepted = load(marker)
            for rel,digest in accepted['outputs'].items():
                content = subprocess.check_output(['git','-C',str(self.wiki),'show',accepted['commit']+':'+rel])
                if hashlib.sha256(content).hexdigest() != digest:
                    raise ValueError(f'Accepted stage binding changed: {rel}')
            print(f'SKIP stage {name}', flush=True)
            return
        stage_root = self.root / 'work' / name
        stage_root.mkdir(parents=True, exist_ok=True)
        previous = sorted(p for p in stage_root.glob('attempt-*') if p.name.split('-')[-1].isdigit())
        work = stage_root / f'attempt-{len(previous)+1:02d}'
        shutil.copytree(self.wiki, work, ignore=shutil.ignore_patterns('.git'))
        # Company/industry readers see only notes with the relevant layer tag.
        if tag:
            permitted = {c['id'] for c in self.chunks if tag in c.get('tags', []) or
                         tag in (self.wiki / 'sources' / (c['id']+'.md')).read_text()}
            for d in ('sources','mentions','gaps','coverage'):
                for p in (work / d).glob('*'):
                    if p.stem not in permitted:
                        p.unlink()
            names = load(work / '_mentions_index.json')
            names = {name: [m for m in rows if m['chunk_id'] in permitted] for name, rows in names.items()}
            write_json(work / '_mentions_index.json', {name: rows for name, rows in names.items() if rows})
            if tag.startswith('公司:'):
                shutil.rmtree(work / 'market', ignore_errors=True)
                for p in (work / 'companies').glob('*'):
                    if p.name != tag.split(':')[1]:
                        shutil.rmtree(p)
        if raw:
            for sid, page in raw:
                source = next(s for s in self.sources if s['id'] == sid)
                record = next(p for p in source['page_records'] if p['page'] == page)
                path = record.get('copied_path') or record.get('corpus_path') or record.get('path') or record.get('text_path')
                dest = work / 'raw' / f'{sid}-p{page:04d}.txt'
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
        before = hashes(work)
        # Retry only in a new workspace. Legal draft outputs are candidates, not
        # accepted facts; their hashes and source attempt remain reviewable.
        seeds = {}
        if previous:
            for path in sorted(previous[-1].rglob('*')):
                if not path.is_file() or path.is_symlink() or '.git' in path.relative_to(previous[-1]).parts:
                    continue
                rel = path.relative_to(previous[-1]).as_posix()
                if not path_allowed(rel, allowed):
                    continue
                if name == 'entities' and (len(Path(rel).parts) != 2 or path.suffix != '.md' or not path.read_text().startswith('---\n')):
                    continue
                dest = work / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
                seeds[rel] = sha(path)
        prompt = template('resolve', ALLOWED=', '.join(allowed), PAGE_BUDGET=len(raw or [])) if resolve else template('assemble', ALLOWED=', '.join(allowed), TASK=task)
        if seeds:
            prompt += '\n本工作区含上次失败尝试中路径合规的候选稿。它们没有被接受；须对本目录来源笔记复核并修正，但无需无意义地重写正确内容。不得为修复悬空链接而创建不属本阶段的页面。\n'
        feedback = self.root / 'reviews' / ('stage-' + name + '.md')
        if feedback.exists():
            prompt += '\n阶段回读反馈：\n' + feedback.read_text()
        seed_receipt = {'from': str(previous[-1]) if previous else None, 'hashes': seeds}
        write_json(stage_root / (work.name + '-retry-seed.json'), seed_receipt)
        try:
            record = self.call(name, work, prompt, allowed)
        except Exception as error:
            with self.lock:
                self.reject(name, work, [str(error)])
            raise
        write_json(Path(record['log']) / 'retry-seed.json', seed_receipt)
        changed = [p for p in hashes(work) if before.get(p) != hashes(work)[p]]
        merged = stage_root / (work.name + '-merged')
        with self.lock:
            shutil.copytree(self.wiki, merged, ignore=shutil.ignore_patterns('.git'))
            for rel in changed:
                dest = merged / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(work / rel, dest)
            check = check_wiki(merged, self.sources, immutable=load(self.root / 'source-note-hashes.json'), link_roots=self.link_roots)
            if resolve:
                try:
                    result = load(merged / 'resolutions.json')
                except (ValueError, OSError):
                    result = None
                resolution_check = check_resolutions(result, self.sources, page_budget=len(raw or []), allowed_pages=raw or [])
                check['errors'].extend(resolution_check['errors'])
                if isinstance(result,dict) and isinstance(result.get('items'),list) and len(result['items']) > 8:
                    check['errors'].append('Resolver exceeded eight-gap budget')
            required = ([f'companies/{name[8:]}/{p}.md' for p in ('overview','segments','timeline','metrics','risks','gaps')]
                        if name.startswith('company-') else
                        [f'industries/optical-fiber/{p}.md' for p in ('overview','products','demand','prices','supply','players','gaps')]
                        if name == 'industry' else ['_index.md','changes.md'] if name == 'index' else
                        ['resolutions.json'] if resolve else [])
            check['errors'].extend('Missing required stage output: '+p for p in required if not (merged/p).is_file())
            if name == 'entities' and not list((merged/'entities').glob('*.md')):
                check['errors'].append('No shared entity pages were produced')
            if name == 'entities' and len(list((merged/'entities').glob('*.md'))) > 16:
                check['errors'].append('P1 core entity-page budget exceeded (maximum 16)')
            check['ok'] = not check['errors']
            record_check(record, check)
            if not check['ok']:
                self.reject(name, merged, check['errors'])
                require(check)
            for rel in changed:
                dest = self.wiki / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(work / rel, dest)
            commit = self.commit('P1: ' + name)
            write_json(marker, {'commit': commit, 'outputs': {p:sha(self.wiki/p) for p in changed}, 'checks': check})

    def assemble(self) -> None:
        self.aggregate()
        self.assert_accepted_wiki()
        self.stage('entities', ['entities/'], '本次P1只整编至多16个核心实体页，优先俊知集团、江苏俊知技术、青海青银、青海中利/俊知、标的研发子公司、长飞、中天及主要相关子公司/股东。完整名字索引和来源笔记继续保留其余主体；不要为了名册完整而逐一建页，也不要宣称所有名字已完成身份统一。不把历史中利集团倒填成本次卖方。更名独立生效日与股权过户日分别核对，不能把过户日自动当旧名结束日；未单列确日应null并说明范围。通用券商覆盖名单、纯董事名单保留在mentions即可。')
        def company(slug):
            focus = ('分部利润要保留定义、未分配费用和内部交易抵销口径，不能当可归属净利润；集团与母公司表分开。'
                     if slug == 'yofc' else '三份公告只覆盖青海交易，不据此写全集团业务全景。股权对价、偿付支持义务和可退保证金分别列；交易阶段和期限起算点保留。')
            self.stage('company-' + slug, [f'companies/{slug}/'], f'写 {slug} 公司六页；仅基于带该公司标签的笔记。'+focus, tag='公司:' + slug)
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(2,self.args.workers)) as pool:
            list(pool.map(company, ['trigiant','yofc']))
        self.stage('industry', ['industries/optical-fiber/'], '公司页已完成，写光纤行业七页，交叉链接两家公司。美银预测、CRU价格表及产品/单位不要混同。', tag='行业:optical-fiber')
        self.stage('index', ['_index.md','changes.md'], '创建 _index.md 入口与 changes.md（P1首次整编范围，不是增量）；链接两个公司概览、行业概览、_sources.md和来源笔记。入口清晰易读，明确P1覆盖和剩余缺口；只写固定范围和导航，不预报尚未执行的补缺结果或数量。')
        # One bounded resolver pass. Existing corpus pages only; no held-out input.
        raw = []
        for s in self.sources:
            sid = s['source_id']
            wanted = ([1,2,3,4] if 'supplemental' in sid else [2,3,4,5] if 'qinghai-acquisition' in sid else
                      [1,2] if 'possible-acquisition' in sid else [1,3] if 'report' in sid else [6,7,173])
            raw.extend((s['id'], p) for p in wanted if p in s['pages'])
        self.stage('resolve', ['companies/','industries/','resolutions.json','changes.md'], '', raw=raw[:16], resolve=True)
        require(check_wiki(self.wiki, self.sources, immutable=load(self.root/'source-note-hashes.json'), link_roots=self.link_roots))
        if 'p1-sample' not in git(self.wiki, 'tag', '--list').splitlines():
            git(self.wiki, '-c', 'user.name=wiki-lab', '-c', 'user.email=wiki-lab@local', 'tag', '-a','p1-sample','-m','P1 sample; program checks only; bounded source readback recorded outside wiki')
        elif git(self.wiki,'rev-parse','p1-sample^{commit}') != git(self.wiki,'rev-parse','HEAD'):
            raise ValueError('Existing p1-sample tag does not bind the accepted HEAD')

    def report(self) -> None:
        records = [load(p) for p in sorted((self.root/'logs').glob('*/attempt-*/record.json'))]
        totals = {'input':0,'cached':0,'uncached':0,'output':0,'seconds':0,'calls':len(records),'amount':None}
        totals['usage_unreported_calls'] = 0
        lines = ['# P1 实际运行记录', '', '|阶段/尝试|状态|秒|输入|缓存|未缓存|输出|', '|---|---|---:|---:|---:|---:|---:|']
        for r in records:
            u = _spike.usage_of(r)
            seconds = r.get('seconds', round(time.time()-r.get('started_at',time.time()),2))
            measured = bool(r.get('usage'))
            if not measured:
                totals['usage_unreported_calls'] += 1
            for k,v in u.items(): totals[k] += v
            totals['seconds'] += seconds
            fields = [str(u[k]) if measured else '未报告' for k in ('input','cached','uncached','output')]
            lines.append(f'|{r["stage"]}/{r["attempt"]}|{r["status"]}|{seconds}|'+ '|'.join(fields)+'|')
        totals['seconds'] = round(totals['seconds'],2)
        write_json(self.root/'usage.json', {'totals':totals,'attempts':records})
        lines += ['', '汇总（包含失败、重试；token合计仅含已报告部分；累计会话耗时不是墙钟耗时）：', '', '```json', json.dumps(totals,ensure_ascii=False,indent=2), '```', '', 'CLI未提供计费金额，amount=null；未报告token的失败调用不当作已知零用量。程序检查只验证结构，原文抽查见 READBACK.md。']
        (self.root/'USAGE.md').write_text('\n'.join(lines)+'\n')


def main() -> int:
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir',type=Path,default=HERE/'runs/p1-20261021')
    p.add_argument('--prepare-only',action='store_true')
    p.add_argument('--registry',type=Path)
    p.add_argument('--phase',choices=['extract','assemble','all','report'],default='all')
    p.add_argument('--chunks',nargs='*')
    p.add_argument('--workers',type=int,default=2)
    p.add_argument('--stage-timeout',type=int,default=900)
    p.add_argument('--codex',default='codex')
    args=p.parse_args(); args.run_dir=args.run_dir.resolve()
    if not args.run_dir.is_relative_to(HERE/'runs'):
        p.error('Run directory must be inside ignored wiki-lab/runs/')
    if args.prepare_only:
        from corpus import prepare
        prepare(args.run_dir,scope='p1',registry_path=args.registry)
        return 0
    pipeline=Pipeline(args.run_dir,args)
    try:
        if args.phase in ('extract','all'):
            chunks=[c for c in pipeline.chunks if not args.chunks or c['id'] in args.chunks]
            if args.chunks and set(args.chunks)-{c['id'] for c in chunks}:
                raise ValueError('Unknown chunk selector')
            if args.chunks:
                by_id = {c['id']:c for c in chunks}
                chunks = [by_id[key] for key in dict.fromkeys(args.chunks)]
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                list(pool.map(pipeline.extract,chunks))
        if args.phase in ('assemble','all'):
            pipeline.assemble()
        return 0
    finally:
        pipeline.report()


if __name__=='__main__':
    raise SystemExit(main())
