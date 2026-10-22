# wiki-lab P0＋P1 与 v2 S1

v2 S1 新增记录格式、必抽清单、Choice 字段表及独立取数器；用法在本页末尾。当前运行见 [S1交付记录](runs/s1-20261021/REPORT.md)，设计差异见 [S1_NOTES.md](S1_NOTES.md)。S1不调用模型，S2尚未实现。以下原有P0/P1命令保留供复现。

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
