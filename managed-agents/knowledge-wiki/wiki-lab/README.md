# wiki-lab P0＋P1

本实现只准备来源和构建五份资料的小样。截点固定为2026-10-15，原件及现有逐页文字来自只读 `invest-sources`。不执行全量构建、增量、研究报告或 invest-lab 接入。原设计和 codex-spike 不变；实现取舍见 [IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) 与 [P0_NOTES.md](P0_NOTES.md)。

从本目录运行，Python 3.9+，不需要新增包；需要本机已有 `codex` CLI 登录与配置、Git。`corpus.py` 不调用模型。CLI 模型与推理强度沿用已有配置，每块和每阶段为独立新会话；调用期间关闭 memories/apps/plugins/multi_agent。

```sh
python3 corpus.py runs/p0-new --scope p0 --prepare-only
python3 corpus.py runs/p1-new --scope p1 --registry runs/p0-new/source-registry.json --prepare-only
```

先审阅 `CORPUS.md`、`corpus-manifest.json`、`source-registry.json`、`chunks.json`。已有run不会覆盖；编号注册表只追加，不重排。记录版本按前驱规范化哈希解析链，保存绑定记录、行号、行哈希、全链与清单哈希；重复、分叉、悬空或原件身份变化会报错。截点按披露日筛选，不把后取得的元数据修订伪装成历史快照。

本次 prepare 产物：

- [P0来源、版本、页码和块清单](runs/p0-20261021/CORPUS.md)
- [P1输入范围](runs/p1-20261021/CORPUS.md)

本次短号由P0冻结，故从 S34 等开始，不在P1重新编号。先短公告，再代表块；读原文核对后才跑其余块：

```sh
python3 run.py --run-dir runs/p1-20261021 --phase extract --chunks S46.c01 --workers 1
python3 run.py --run-dir runs/p1-20261021 --phase extract --chunks S34.c01 S42.c01 --workers 2
# 完成原文回读后再运行其余块；已成功块自动核验并跳过
python3 run.py --run-dir runs/p1-20261021 --phase extract --workers 2 --stage-timeout 1200
python3 run.py --run-dir runs/p1-20261021 --phase assemble --workers 2 --stage-timeout 1200
```

`--stage-timeout` 默认900秒，超时会终止进程组并保留日志。失败重试保留新attempt目录，旧日志不覆盖。抽取成功绑定输入哈希、prompt哈希、清单哈希及产物哈希；改过成功输入或产物会拒绝续跑。更换prompt应新建run，不能静默重抽已接收笔记。

原文回读反馈保存在 `runs/<run>/reviews/<块>.md`，重试的实际prompt及哈希完整保存在日志。`review.py RUN_DIR CHUNK_ID EDITS_JSON` 可在汇集之前应用明确的逐字修正或确切单来源页数组的格式修正；它另建review副本、保留原稿、生成diff和检查记录，通过后才更新成功指针。不改已汇集的wiki来源笔记，也不重新调用模型。

`review_views.py RUN_DIR STAGE EDITS_JSON` 用于模型正常结束的实体/公司/行业/入口稿（结构拒绝后的修正，或已接收视图的回读修正）；计划为 `{"reason":"原因","replace":[{"path":"公司页路径","old":"唯一匹配原句","new":"修正句"}]}`。它核验真实原稿和日志哈希，只带入该阶段允许的页面，在新副本逐字修正、重查后提交；已有接收记录时以当前视图为基底，保留先前修正及补缺。原模型日志、拒绝分支及旧接收记录保留。此入口不支持补缺阶段，不能绕过其页数预算；不用于重写来源笔记。

整编顺序为程序汇集、至多16个共享核心实体、两家公司（可并行）、行业、入口、一次最多16页原文与8个缺口的补缺。实体目录只准直接子级实体Markdown。阶段只可修改明确路径；模型在临时工作区编辑，检查后由唯一写入者合并和提交。失败改动保留工作区及 `rejected/` 分支，重试在新工作区带入路径合规的候选稿并记录哈希；主线与来源笔记保持不变。续跑核验干净main及已接受提交链。小样仓库仅发布 `p1-sample`，不占用 `wiki-v1/wiki-v2`。runner会拒绝P0全量清单，避免误入P2。

阅读从 [小样入口](runs/p1-20261021/wiki/_index.md) 开始。每条引用是PDF页序，通过 [原文页索引](runs/p1-20261021/wiki/_sources.md) 打开原件或冻结页文字。来源笔记保留资料原有口径；公司与行业页是可重建视图。

```sh
python3 checks.py runs/p1-20261021/wiki --link-root runs/p1-20261021/corpus --link-root /Users/leon/Stock/invest-sources
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p 'test_*.py' -v
```

结构检查覆盖字段、来源页码、组合引用、实体frontmatter、别名、关系、缺口状态、链接及市场词隔离。结构通过不证明引用支持论断、提取完整或研究接受。

原文抽查见 [READBACK.md](runs/p1-20261021/READBACK.md)；用量含失败重试见 [USAGE.md](runs/p1-20261021/USAGE.md)。CLI没有计费金额时一律 `null`，不套用未知价格。全量估计与待调整事项在运行完成后写入该run的 `REPORT.md`。

`runs/` 在创建前已加入 `.gitignore`。语料、研报摘录、模型事件、wiki及其内部Git仓库均被外层仓库忽略，不应强制提交。
