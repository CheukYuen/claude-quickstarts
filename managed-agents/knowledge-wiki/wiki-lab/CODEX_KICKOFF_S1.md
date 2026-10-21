# wiki-lab v2 S1 启动 prompt

在 `/Users/leon/Developer/claude-quickstarts/managed-agents/knowledge-wiki/wiki-lab` 工作，分支 `codex/knowledge-wiki-codex-spike`。按 [DESIGN-v2.md](DESIGN-v2.md) 第 14 节做 **S1（格式）**。S1 做完就停，不进入 S2，不跑全量抽取。

## 先读

- `DESIGN-v2.md` 全文，这是本轮依据。`DESIGN.md` 只读 v2 引用到的部分（原稿第 5.1、8、9 节）。
- P1 的结果：`runs/p1-20261021/REPORT.md`、`runs/p1-20261021/wiki/sources/*.md`（8 份来源笔记，覆盖对照用）、`prompts/extract.md`（里面的规则要迁成字段和校验）。
- 现有代码：`corpus.py`、`checks.py`、`run.py`。能复用的复用，不重写。
- invest-sources：`/Users/leon/Stock/invest-sources/AGENTS.md`（清单格式、Source ID 规则），以及长飞中报的 8 条提取记录 `evidence/601869.SH-20260822-interim-2026h1/extracts/v1.json`（记录字段对照样例，4.2 节）。
- invest-lab 的 Choice 代码和文档（见第 5 步）。

## 约束

- `DESIGN.md`、`DESIGN-v2.md` 不改。发现设计问题，写进 S1 交付记录。
- `runs/p1-20261021/` 和 `p1-sample` 标签只读。本轮新建 `runs/s1-<YYYYMMDD>/`。
- `/Users/leon/Stock/invest-sources`、`/Users/leon/Stock/invest-lab` 只读。
- Python 3.9+ 标准库，不装新包。Choice SDK 已装在 `~/.choice/EMQuantAPI_Python/`，系统 `python3` 可以直接 `from EmQuantAPI import c`。
- 本轮不调用模型。第 15 节已经定了：抽取继续用 `codex exec`，改为单轮调用，原文经标准输入放进 prompt，用 `--sandbox read-only` 和 `--output-schema`，prompt 要求不执行命令。`run.py` 的单轮抽取调用留到 S2 实现。
- 外层仓库是公开的 claude-quickstarts。原文摘录、研报内容、Choice 返回只放在 `runs/` 下（已被 `.gitignore` 忽略）。仓库里的单元测试用合成文字，不贴原文。Choice 接口文档不复制进仓库，需要时写它在 invest-lab 里的路径。
- 所有操作都走仓库里的代码并留痕，不在 run 目录里留一次性脚本（第 13 节"去掉"一栏）。
- 不 commit，不 push。做完我审阅。

## 要做的

### 1. 记录格式（第 4 节）

新建 `records.py` 和格式说明 `RECORDS.md`：

- 五类记录（4.1 节）各自的字段，加上共有字段：记录编号、来源和定位（PDF 页序、表或章节）、逐字摘录、披露日、基准日 `as_of`、是否市场内容、抽取版本、核验状态。
- 记录来源分两种：从原文抽取的，带逐字摘录和 PDF 页序；从 Choice 转换的，没有摘录，出处写 Choice 返回文件的资料编号、字段名和报告期，披露日取 `FIRSTNOTICEDATE`（5.1 节）。核验状态要能表示"待复核"和"未查值"。
- 对象键（主体、指标、期间），用于查询、覆盖判断和增量。
- 金额组成和可加性用字段表达，例如 `components: [本金, 利息, 罚息, 违约利息]` 加 `as_of`。
- 覆盖关系：同键记录按披露日判断谁覆盖谁，旧记录保留。
- 取值枚举：单位、币种、期间、合并范围、性质（实绩、指引、机构预测、判断、转引）、关系类型、事件类型、交易阶段。未知日期为 `null`，必须附说明。
- 校验函数，对应第 7 节前三行：
  - 摘录存在：逐字摘录出现在所引页的冻结逐页文字里，空白规范化后比较。
  - 数值在摘录中：数值按单位规则格式化后出现在摘录里，要处理千分位、小数位、千元／百万元／亿元换算、括号负数。
  - 字段合法：取值在枚举内；`null` 日期有说明。
- 由记录格式生成抽取输出的 JSON Schema `schemas/extract-output.json`（记录数组加覆盖清单），给 `codex exec --output-schema` 用。Schema 和 `records.py` 的校验出自同一份定义，不各写一套。Schema 能否被 `--output-schema` 接受，留到 S2 第一次调用时验证。
- 起草单轮抽取 prompt `prompts/extract-records.md`，S2 使用，本轮不跑。
- 单元测试 `test_records.py`。

### 2. 两份必抽清单（4.3 节）

`checklists/a-share-periodic.json`（A 股中报、年报）和 `checklists/hk-transaction-announcement.json`（港股交易公告）。每项写：编号、名称、对应的记录类型和对象键、判为"本文档无"时要写什么依据。

### 3. 用 P1 已抽内容验证格式

这是 S1 的验收标准（第 14 节"看什么"）。

- 逐份读 P1 的 8 份来源笔记，把每条已抽内容对到记录类型和字段。落不下的就改格式，改完再对一遍。
- 把有代表性的难点写成样例记录，放在 `runs/s1-*/records/`，全部要通过第 1 步的校验：
  - S46 全文（4 页）的全部记录。
  - S48、S50：对价及组成、付款安排、偿付支持（`components`、`as_of`）、履约保证金条款（义务方、期限、起算事件、条件）、更名和附属关系（起止日未知时为 `null` 并说明）、S48 的"400 吨"被 S50 的"400 吨/年"覆盖。
  - S34：主要会计数据一组、分部收入（对外收入和分部间收入分开）、非经常性损益、P1 里一处跨块续表、CRU 需求预测的转引（注明共同出处）。
  - S42：美银的盈利预测，以及评级和目标价（市场内容标记）。
- 交付 `runs/s1-*/COVERAGE.md`：P1 条目总数、直接能放下的、改了格式才放下的、仍放不下的（逐条列出原因）。

### 4. 长飞 Choice 字段对照表（5.1、5.3 节）

- 读 `invest-sources/vendor/choice/choice-20261004-yofc-financials-market.json`（`ctr` 完整三表，6 期）。字段说明看 invest-lab `.agents/skills/choice-quantapi-skill/references/indicators/ctr/` 下对应的报表页。
- 生成 `choice_fields/ctr-a-share.json`：每个字段写 Choice 字段名、对应的原表科目名、类别（原表科目／Choice 派生／待定）、对应的记录指标、单位规则。
- 派生的判断用代码重做 5.1 节的核对：2026H1、2025、2024 三期的整数值，是否出现在对应报告的逐页文字里（`601869.SH-20260822-interim-2026h1`、`601869.SH-20260328-annual-2025`、`601869.SH-20250329-annual-2024`）。结果应当是 444 个值里 435 个能找到，没找到的 9 个落在 ACCOUNTBILLREC、ACCOUNTBILLPAY、OTHERPAY、OTHERCINCOME。对不上就说明原因。
- 原文里找不到不等于派生：要结合字段说明，判断不了的标"待定"。
- 这段查值代码就是 5.1 节的查值报警，写成可复用的函数：按证券代码和 `FIRSTNOTICEDATE` 找到对应报告，返回每个原表科目"查到／查不到／未查值"。S2 读入 Choice 时直接用它。
- 核对现有文件：全部查询都是 `ReportType=1`，返回的报表类型都是"合并报表"；6 期的 `FIRSTNOTICEDATE` 和清单里对应定期报告的披露日一致（应当全部一致）。不一致就停下来告诉我。
- 不做逐值定位（报表页、科目行、期间列），S2 也不做。A 股原表科目直接用 Choice 的值（5.1 节）。

### 5. 取数阶段（5.2 节）和 S1 的两项接口测试

**代码直接从 invest-lab 复制，不抽成两边共用的模块。** 这一点偏离了 5.2 节"抽成两边共用的取数模块"的写法，是我的决定，在交付记录里写明。

- 新建 `fetch.py`。复制 `/Users/leon/Stock/invest-lab/runs/research-handoff-20261003/data-transcribe/t04b-3/choice_capture.py`（invest-lab commit `317c9b0`）的 `pack`、`sdk_result`、登录检查、出错即停和 `finally` 里退出登录的逻辑。现有的长飞三表文件就是这份脚本产出的，复制后返回文件格式和 `vendor/choice/` 里已有的文件一致。文件头注明复制来源的路径和 commit。不 import invest-lab 里的任何路径。
- 输入是 `requests.json`。格式由你定，以后由缺口生成（第 9 节），至少包括：请求编号、目的（对应哪个缺口或测试项）、函数、参数。
- 每次运行设调用上限，失败的调用也算一次。保存原样返回、查询参数、取数时间和 SHA-256。登记只追加，不改旧行。取不到的写明原因，同一请求不反复重试。
- 存放：本轮写到 `runs/s1-*/fetch/`，目录布局和清单行都照 invest-sources 的格式（`vendor/choice/<source_id>.json`、`sources.jsonl`，source_id 按 invest-sources 的 Source ID 规则）。**不写 invest-sources**：它的 AGENTS.md 规定清单只由 D-抄录写入，5.2 节"上线前要处理的"第三条还没定。在交付记录里写一段 invest-sources 规则的建议改法，由我决定。
- 调用规则沿用 invest-lab 的 Choice skill（`.agents/skills/choice-quantapi-skill/SKILL.md`）：
  - 写调用前先读 `references/functions/ctr.md`（用到 `css`、`csd` 等其他函数时也读对应的页）和 `references/help/error-codes.md`。
  - 用 `Ispandas=0`，和现有 vendor 文件一致。
  - 首次调用前不跑 SDK 自检。
- 登录或调用出错时：
  - `c.start()` 失败就停下。
  - 10001009（账号已在其他设备登录）：不加 `--force-login`，停下告诉我。
  - 10001020、10001019（凭据失效、换了设备）：不跑 `activate.py`（激活要我手动走 GUI 或短信），停下告诉我。
- 网络：沙箱禁网时调用会失败，invest-sources 里有先例 `choice-20261014-yofc2-sandbox-network-failure.json`。遇到就记录下来并停下，告诉我需要放开网络，不要设法绕过。

S1 的接口测试，总共最多调用 8 次：

1. **俊知港股完整三表**（5.5 节）：`01300.HK`，`BalanceStatementHK`、`IncomeStatementHK`、`CashFlowStatementHK`，报告期 2025-12-31，共 3 次。参数按 invest-lab 的 `references/indicators/ctr/0002000100010001.md`、`…0002.md`、`…0003.md`。测通了就用第 4 步的查值函数，在 `1300.HK-20260422-annual-2025` 的逐页文字里查一遍，看哪些字段是原表科目、哪些是 Choice 自定口径；再看返回里有没有类似 `FIRSTNOTICEDATE` 的披露日字段、有没有区分原始和调整报表的参数。只记录结果，不建港股的字段对照表，港股也不因此改为直接用 Choice。
2. **汇率**（5.6 节）：先看 `vendor/choice/choice-20261015-bc-fx-*` 三次是怎么查的、为什么返回空，再按文档换一种没试过的查法，最多 3 次。还是取不到就记下来，港币汇率继续用 Wind。
3. 其余 2 次留给参数写错后的修正。

### 6. 交付

仓库内：

- `records.py`、`schemas/extract-output.json`、`prompts/extract-records.md`、`checklists/`、`choice_fields/`、`fetch.py`，以及对应的测试。
- `RECORDS.md`（记录格式说明）。
- `S1_NOTES.md`：实现上的取舍，以及和 DESIGN-v2 不一致的地方（至少包括：Choice 代码是复制的，不是共用模块；取数结果暂时不进 invest-sources）。
- `README.md` 补上 S1 的用法。

run 目录 `runs/s1-*/`：

- `records/`（样例记录）、`COVERAGE.md`、Choice 字段核对结果、`fetch/`（取数产物和调用日志）。
- `REPORT.md`，回答这几件事：
  - 格式和清单能不能覆盖 P1 已抽的内容，改了哪些格式点。
  - 长飞 Choice 的查值、`ReportType` 和 `FIRSTNOTICEDATE` 核对结果。
  - 港股完整报表接口和汇率查询的测试结果。
  - 进入 S2 之前要我决定的事：invest-sources 的写入规则，以及你发现的其他问题。

测试全部通过，P1 原有的测试也不能坏：

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s . -p 'test_*.py' -v
```
