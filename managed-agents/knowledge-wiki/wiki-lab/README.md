# wiki-lab P0＋P1 与 v2 S1/S2

v2 S1 新增记录格式、必抽清单、Choice字段表及独立取数器；S2新增结构切块、单轮记录抽取和同输入对照。S2的运行状态以[交付记录](runs/s2-20261022/REPORT.md)为准，实现取舍见[S2_NOTES.md](S2_NOTES.md)。[S1交付记录](runs/s1-20261021/REPORT.md)及其[S1_NOTES](S1_NOTES.md)保留。以下原有P0/P1命令只供复现，不在已有冻结运行中重放。

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

## v2 S1：格式与离线核对

[RECORDS.md](RECORDS.md)说明五类记录、核验规则、覆盖关系及Choice来源例外。[records.py](records.py)是字段与Schema的唯一来源；修改字段后重新生成Schema并测试。三份`checklists/`分别用于A股定期报告、港股交易公告及机构研报。Python 3.9+标准库，无新包。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 records.py schema schemas/extract-output.json
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p 'test_*.py' -v
```

创建新的S1运行时先冻结输入指纹。已有运行不能重新prepare或重置取数预算；本轮跨日继续用`s1-20261021`。准备、P1映射/样例、Choice核对、测试和报告均走仓库程序，不在run内放一次性脚本。

```sh
# 仅新运行执行；本轮已经执行过，不能重做
PYTHONDONTWRITEBYTECODE=1 python3 s1.py prepare --run-dir runs/s1-new

# 本轮离线结果重建，不调用模型或SDK；样例配方含原文锚点，仅留在runs
PYTHONDONTWRITEBYTECODE=1 python3 s1_review.py --p1 runs/p1-20261021 --output runs/s1-20261021 --recipe runs/s1-20261021/sample-recipe.json --manual-review runs/s1-20261021/manual-review.json
PYTHONDONTWRITEBYTECODE=1 python3 choice_audit.py --run-dir runs/s1-20261021
# 港股接口后的离线核对，仅复用已捕获文件，不调用SDK
PYTHONDONTWRITEBYTECODE=1 python3 choice_audit.py --run-dir runs/s1-20261021 --hk-capture runs/s1-20261021/fetch/vendor/choice/choice-20261022-s1-trigiant-hk-statements.json --hk-decisions runs/s1-20261021/hk-review-decisions.json
PYTHONDONTWRITEBYTECODE=1 python3 s1.py tests
PYTHONDONTWRITEBYTECODE=1 python3 s1.py verify
PYTHONDONTWRITEBYTECODE=1 python3 s1.py report
```

`s1_review.py`以段落、列表项和表数据行盘点P1；规则预分类与人工逐条/分组字段审阅分开记录。只有`records/`内实际样例通过冻结页摘录、数值和字段核验，映射清单不等于全量重抽。`choice_audit.py`复用`corpus.py`的清单版本链及哈希，按证券代码、FIRSTNOTICEDATE及报告期找原报告。`check_values(...)`返回每字段查到/查不到/未查值，不生成报表页行列定位；源报告缺失不阻塞，字段派生/待定判断独立保存。

## v2 S1：Choice取数

先读invest-lab的`.agents/skills/choice-quantapi-skill/SKILL.md`和实际函数、指标、错误码页；文档路径写在请求里，不复制到公开仓库。系统`python3`使用已装SDK，无首次查询前自检；一律`Ispandas=0`。

`requests.json`示意（合成目的与名称）：

```json
{
  "schema": "wiki-lab-requests/1",
  "source_slug": "synthetic-example",
  "company_code": "000001.SZ",
  "title": "示例请求",
  "requests": [
    {
      "id": "example-1",
      "purpose": "对应缺口编号或接口测试编号",
      "function": "ctr",
      "args": ["BalanceStatementSHSZ", "", "SecuCode=000001.SZ,ReportDate=2025-12-31,ReportType=1,Ispandas=0"],
      "kwargs": {}
    }
  ]
}
```

```sh
python3 fetch.py runs/s1-new/requests.json runs/s1-new/fetch --max-calls 8
```

预算跨同一`fetch/`内的请求文件累计，失败也计次；每批出错即停，同一请求或查询不自动重试。`fetch/vendor/choice/`存完整返回，`fetch/manifests/sources.jsonl`只追加登记，`fetch/calls.jsonl`在调用前后留痕。每个返回绑定原查询、取得时间和SHA-256。未执行请求列在批次回执，ErrorCode=0但无值标no_data。`fetch.py`只允许CLI写到本项目runs下，不写invest-sources。

首次登录10002002的返回和terminal_stop仍保留。用户2026-10-22授权恢复后，用`--resume-network '明确的用户授权理由'`追加绑定原网络停止事件的恢复记录；预算不变。此选项不能清除账号或激活错误，新的失败也不会被旧恢复记录清除。10001009不强制登录，10001019/10001020不自动激活。

本轮接口测试已结束，累计6/8次：港股三表3次成功，FX3次未取得报价数字；币对名称成功不算报价成功。剩余2次不再使用，不要重复执行取数。汇率请求由`s1.py fx-spot-request`、`fx-midpoint-request`、`fx-quote-request`生成，后者只使用前一次真实返回的币对代码；请求文件存在时拒绝覆盖。`interface-tests.json`区分报价与元数据、保存逐次结果；旧报告和测试回执先按哈希归档到history/。单轮抽取prompt已起草，`run.py`和`codex exec --output-schema`实测仍留到S2。

## v2 S2：同输入记录对照

以下是新运行的顺序；现有`s2-20261022`要续用调用账本，不能通过换目录重置40次预算。先检查CLI版本；本机`codex`的PATH命中了0.144.2，P1使用的0.163.0在`~/.local/bin/codex`，本轮显式选择后者，模型和推理强度仍取原配置。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 s2.py init --run-dir runs/s2-new
PYTHONDONTWRITEBYTECODE=1 python3 s2_chunks.py runs/s2-new --max-chars 20100
# Schema被明确拒绝时，本入口自动再调用传输Schema；两次都计预算。
PYTHONDONTWRITEBYTECODE=1 python3 s2.py probe --run-dir runs/s2-new --codex "$HOME/.local/bin/codex"
# 已确认只能用传输Schema的后续试点轮次，可用--transport替代上条命令，勿重复执行。
# 下面块号来自修订后的33块清单；其他切块计划须先核对chunks.json。
PYTHONDONTWRITEBYTECODE=1 python3 s2.py extract --run-dir runs/s2-new --chunk S46.s01 --stage pilot-r1-S46 --transport --codex "$HOME/.local/bin/codex"
PYTHONDONTWRITEBYTECODE=1 python3 s2.py extract --run-dir runs/s2-new --chunk S34.s03 --stage pilot-r1-S34-section2 --transport --codex "$HOME/.local/bin/codex"
PYTHONDONTWRITEBYTECODE=1 python3 s2.py extract --run-dir runs/s2-new --chunk S34.s12 --stage pilot-r1-S34-note7 --transport --codex "$HOME/.local/bin/codex"
PYTHONDONTWRITEBYTECODE=1 python3 s2.py gate --run-dir runs/s2-new
# gate全部满足后才可运行；成功试点直接复用，不另花调用。
PYTHONDONTWRITEBYTECODE=1 python3 s2.py full --run-dir runs/s2-new --workers 2 --codex "$HOME/.local/bin/codex"
PYTHONDONTWRITEBYTECODE=1 python3 s2_choice.py runs/s2-new
PYTHONDONTWRITEBYTECODE=1 python3 s2.py supplement --run-dir runs/s2-new --codex "$HOME/.local/bin/codex"
PYTHONDONTWRITEBYTECODE=1 python3 s2.py collect --run-dir runs/s2-new
PYTHONDONTWRITEBYTECODE=1 python3 s2.py metrics --run-dir runs/s2-new
PYTHONDONTWRITEBYTECODE=1 python3 s2_report.py runs/s2-new --phase full
PYTHONDONTWRITEBYTECODE=1 python3 s2.py integrity --run-dir runs/s2-new
```

`s2_chunks.py --rechunk --reason '原因'`在同run内归档旧计划再重切；有成功调用时须保护已用块的哈希。超时/截断自动保存拆块回执，续跑沿用子块；其他失败至多重试一次。calls.jsonl在启动前占预算，最终以日志逐次核对。模型不允许看到P1笔记、S1样例或已有记录；这些文件只由事后对照代码读取。

`s2_compare.py --help`列出P1盘点、用量和分层审阅队列的离线入口；未完成全量时必须带`--incomplete`。待审项不算已判错误，尚未抽取不算漏抽。`s2_report.py --help`提供阶段明确的报告入口。所有原文、摘录、模型日志和运行数据都在runs/，仓库测试仅用合成文字。完整运行事实以REPORT和用量回执为准，S2结果不自动授权进入S3。

本轮试点修订后使用`--round 2`；不要把上面新run的默认第一轮命令用于现有账本。全部活动调用结束后运行`s2.py metrics --run-dir RUN`回填合法JSON的原始输出条数，再按实际状态生成报告。预算停止时，初始结构叶块全部通过的文档可独立对照；未完成文档也可对照已就绪区域：同页涉及的全部当前core叶块均通过才算就绪，context不计，混合排除边界页须等整份文档完成。条目全部所引页就绪后，未匹配/候选项才进入回读；跨未完成页的条目仍待抽取。完全排除页继续只走Choice规则。`comparison_region_complete`不等于`source_extraction_complete`，就绪条目数仅界定未匹配回读范围，不是全量覆盖率分母；邻页同值匹配保留候选标签，但不能反推页就绪。`comparison-regions.json`保存逐页依据，已就绪S34区域与S42共同分层抽60条。

报告生成的固定样本可用`python3 s2_readback.py RUN --queue semantic --batch-size 10`导出回读包（也支持`unmatched`、`reverse`）。包中保留完整记录、候选记录及冻结页全文，索引绑定队列和页哈希；不会生成正确/错误裁决。显式裁决JSON须绑定`review-queues.json`的规范化哈希，再经`s2_report.py RUN --phase PHASE --reviews FILE`应用；此处FILE按当前工作目录解析，须包含RUN前缀或使用绝对路径。样本确定前不要导出最终回读包，同名不同绑定会拒绝覆盖。

未匹配项按当前有效首轮结构叶块分层；超时拆块后使用实际子块的完整core片段，不再用失败父块，context不参与归块。队列metadata列出`current structural leaves`及块ID。抽样保留base seeds（未匹配20261022、反向20261023、语义20261024）和轮流配额；各层先按条目/记录ID排序，再使用独立RNG打乱，种子为规范化JSON `[base_seed, stratum_key]` 的UTF-8 SHA-256大端整数。层顺序仍按键排序后用base seed打乱；新增其他层的记录不改变既有层内排序，但总体配额仍随各层规模变化。队列metadata保存算法版本、派生规则和每层种子；最终裁决仍须绑定最终队列。

进行中的回读可先读已完成来源，用`--snapshot progress-01`写到独立子目录，保留当时绑定。每包另有`*-compact.json`条目概览，候选记录全文按ID去重附在包末。index同时保存原始队列文件的`queue_sha256`和规范化JSON的`queues_sha256`，草稿绑定用后者，两者不能混用。最终回读仍以最终队列为准。`supplement`只在完整首轮后运行；达到40次时保存预算停止，不增加调用；首轮不完整则不得执行补抽，用`--phase budget-stopped`生成报告。`--phase full`只说明首轮块完整，不能替代显式语义裁决。

逐份完成的公告可以先写明人工回读草稿；`s2_readback.py RUN --bind-drafts reviews/coverage-S46-draft.json ... --review-id ID --output reviews/final-reviews.json`只合并显式裁决并绑定最终队列。未匹配项须带对应来源records文件哈希；语义/反向样本草稿须带完全相同的当前队列哈希。条目重复、来源变化或已有输出不同均拒绝，不会代写判断。

抽样尚在变化时的预读草稿，全部模型调用结束、最终队列生成后，用`python3 s2_readback.py RUN --rebind-retained reviews/preread-draft.json --review-id ID --output reviews/retained-final.json`显式转移。草稿须有`reviewer`、`review_id`、`decisions`、旧`queues_sha256`、`readback_indices`和各来源records原始文件的`source_records_sha256`。所有队列（含未匹配项）均须引用已保存的旧index，证明条目当时确已入选；index须绑定该旧队列哈希与当前未变的清单SHA。语义/反向草稿还须直接保存与当前一致的`corpus_manifest_sha256`。程序验证每个原ID、旧样本归属及来源绑定后，仅原封保留仍在最终队列中的同`(queue,id)`裁决，并用`apply_reviews`复验；不生成判断，不改队列或记录，也不覆盖内容不同的既有输出。普通`--bind-drafts`仍允许来源级未匹配草稿省略整体队列哈希，这一例外不适用于`--rebind-retained`。

有效但未被最终样本选中的裁决留在`retention.skipped_not_selected`，只能作为定向回读证据。最终语义80条、反向40条的分母只取最终队列的实际样本；保留裁决没有覆盖到的最终样本仍待回读，不能用预读总数或`skipped_not_selected`补足分母。转移回执保留旧/新队列哈希、原草稿哈希、reviewer和来源哈希；随后通过`s2_report.py RUN --phase PHASE --reviews RUN/reviews/retained-final.json`应用。

日期合同留有待S3决定的歧义：历史事件的`occurred_on`已知时，`as_of`能否表示当前公告对该事件的观察日；通用方法的期间“不适用”与期间“未知”如何区分。S2保留原输出与逐条回读结论，不据此改动prompt、Schema或核验规则。

S2b 小样本命令（不启用全量或S3）

```sh
PYTHONDONTWRITEBYTECODE=1 python3 s2.py s2b-init
PYTHONDONTWRITEBYTECODE=1 python3 s2.py s2b-probe
PYTHONDONTWRITEBYTECODE=1 python3 s2.py s2b-extract --chunk S46.s01
# 其余固定块：S50.s01、S34.s03、S34.s09、S34.s12、S42.s01
PYTHONDONTWRITEBYTECODE=1 python3 s2.py s2b-request --chunk R1
PYTHONDONTWRITEBYTECODE=1 python3 s2.py s2b-request --chunk R2
PYTHONDONTWRITEBYTECODE=1 python3 s2.py s2b-collect
PYTHONDONTWRITEBYTECODE=1 python3 s2b_measure.py --run runs/s2b-20261022
PYTHONDONTWRITEBYTECODE=1 python3 s2.py s2b-integrity
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p 'test_*.py' -v
```

默认目录 `runs/s2b-20261022/`，可用 `--run-dir runs/s2b-YYYYMMDD` 指定新轮次；预算在该目录 `calls.jsonl` 中预留，固定CLI及版本，最多16次。每次显式extract/request均消耗调用，不隐式重试；重跑会在日志中保留各次尝试。`accepted`、`rejected`、`soft-queue`、`outside-index`、`entities` 分别保留展开结果及队列。`adjudications.json` 保存带页码和依据的逐条回读裁决，重新测量会把裁决并入对照JSON。名称表、输出合同及新规则详见 [RECORDS.md](RECORDS.md)，实现取舍见 [S2_NOTES.md](S2_NOTES.md)。

自定义请求用 `s2.py s2b-request --request-file REQUEST.json`，文件形如 `{"request_id":"R3","fact":"合成事实","source":"S34","pages":[111,112],"header_pages":[]}`；header_pages仅列真正需要的续表列头页，不默认附前一页。`s2.py s2b-revalidate --chunk ID` 只重放本地核验，保留原输出及原回执，不发模型调用。
