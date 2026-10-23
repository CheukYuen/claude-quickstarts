# S1 记录格式

格式版本为 `wiki-records/1.0`，始于S1的格式定义与验证；S2已加入单轮抽取及模型输出到存储记录的转换，见下文。字段定义在 [records.py](records.py)，JSON Schema 由同一份定义生成；不手改 [schemas/extract-output.json](schemas/extract-output.json)。所有公开测试使用合成文字。

```sh
PYTHONDONTWRITEBYTECODE=1 python3 records.py schema schemas/extract-output.json
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest test_records -v
python3 records.py validate OUTPUT.json --pages PAGES.json --checklist checklists/a-share-periodic.json
```

`PAGES.json` 是 `[{"source_id":"合成来源","page":1,"text":"冻结页文字"}]`。程序接口直接接受 `{(source_id, PDF页序): 冻结文字}`。省略 `--pages` 只验证字段和数字与摘录的一致，不声称已查原文。传入页映射时，缺页会拒收。S1 的实际逐页文字及样例只放在被忽略的 `runs/` 下。

输出为 `schema_version`、`records` 数组和 `coverage` 数组。每条记录只属于五类之一；不使用无类型的自由 JSON 存放实质记录。对象未知值用 `null`，不使用空字符串代替日期、币种或出处。数字用十进制字符串，避免浮点误差。

| 共有字段 | 含义 |
| --- | --- |
| `record_id`、`record_type` | 不重复的编号；`metric/event/obligation/relationship/statement` |
| `source` | `kind` 为 `pdf` 或 `choice`；`source_id` 是资料库编号；Choice 另填 `choice_field` 和 `report_period`，PDF 两项为 null |
| `locator` | `pdf_pages` 为 PDF 页序数组；`table` 和 `section` 可为 null；多页表头、行、续表可同时定位 |
| `excerpts` | `[{page,text}]`；每段须在对应冻结页逐字连续出现，允许多个页、多个片段，不拼成不存在的连续引句 |
| `disclosed_on`、`as_of` | 披露日与数值/状态的基准日，格式 YYYY-MM-DD；两者不互代 |
| `date_notes` | `[{field,reason}]`；每个 null 日期都须有对应字段路径的说明，如 `as_of`、`detail.starts_on`、`detail.deadline.from_date` |
| `is_market` | 证券行情、评级、目标价、估值倍数等为 true；产品实物售价仍可为 false |
| `extraction_version`、`verification` | 抽取版本；核验状态及说明。状态为待核验、代码通过、人工复核通过、待复核、未查值、拒收 |
| `object_key`、`period` | 键为主体 `subject`、指标 `indicator`、期间标签 `period`；`period.label` 必须与键一致，`period.kind` 限定期间类型 |
| `nature` | `actual` 实绩/已披露事实、`guidance` 指引、`institution_forecast` 机构预测、`judgment` 判断、`secondary` 转引 |
| `numbers` | 所有结构化数值主张，五类记录共用，详见下文 |
| `components`、`additivity` | 金额组成；可加组 `group`、角色 `standalone/total/component/non_additive` 与理由。总额和分项必须有组，不自动相加 |
| `dimensions` | `[{name,value}]`；保留分部、对外/分部间收入、产品、地区、项目阶段、原始列名、上下界等维度 |
| `origin` | 陈述方、原始出处、共同出处ID；转引必须有后两项，不能当独立来源 |
| `limitations`、`supersedes` | 限制数组及被新记录覆盖的旧编号数组；旧记录保留 |

`period.kind` 枚举为 `instant/annual/half_year/quarter/range/event/unspecified`。标签保留真实期间，例如年度、半年度、季度、某交易或明确区间；机构预测列的 E 属性由 `nature` 保存，不能仅藏在标签里。未知期间用 `unspecified` 并在限制中解释。会计估计、假设敏感性测算用 `judgment` 加 `dimensions` 写明测算性质和条件，不能改成管理层指引或已实现结果。

五类 `detail` 字段如下。日期字段均遵守 null 附说明的规则。

| 类型 | 专有字段 |
| --- | --- |
| `metric` | `scope`、`scope_note`、`value_status`、`value_note`。范围枚举为 `consolidated/parent/segment/subsidiary/company_model/unspecified`。值状态为 `reported/not_available/not_meaningful`；NA、NM 不改成零，非 reported 的 numbers 必须为空，value_note 必须有非空白说明（保留原始 NA/NM/空白标记及含义） |
| `event` | `event_type`、`occurred_on`、`stage`、`description`。事件类型为 `auction/signing/payment/transfer/completion/incorporation/production_start/disclosure/other`；阶段为 `bidding/proposed/signed/paid/transferred/completed/operating/planned/unspecified` |
| `obligation` | `obligor`、`beneficiary`、`action`、`deadline`、`conditions`、`consequences`、`payment_status`。deadline 包含 amount、unit、from_event、from_date、deadline_on；期限单位为 business_days/calendar_days/months/years/none。付款状态为 unpaid/paid/part_paid/due/unspecified/not_applicable；not_applicable 用于非付款义务（例如开产、披露、过户、账户共管），unspecified 表示付款状态未知，不能用已付保证金推断其他义务履行 |
| `relationship` | `subject`、`object`、`relationship_type`、`starts_on`、`ends_on`、`status`、`description`。关系为 ownership/subsidiary/renamed_to/director/identity_candidate/independent_party/other；状态为 current/prospective/historical/unspecified。董事名单、改名前后名称、股权链都可以表达 |
| `statement` | `speaker`、`content`、`statement_type`、`attribution_note`。类型为 explanation/forecast/guidance/judgment/definition/risk/attribution/market_rating/market_target/other；评级和目标价类型强制 is_market=true |

数值、摘录与两种出处

摘录须取支持该记录的最短连续原文。正文取数值、日期或条款所在的一句；主语在前一句时可带紧邻的那半句。表格只取科目所在行，期间、单位和币种列头另取一段，续表列头可在另一页。多要素义务分散在几句时，每句各取一段，不合成大段。通用责任声明、页眉页脚、目录不作摘录；不取整页、整段或整张表。多条记录可以共用同一表行，不能共用整页。

每段去掉全部空白后最多 `records.MAX_EXCERPT_CHARS = 500` 字符，超过即拒收；不提供记录级豁免。JSON Schema 不另设按原始字符计数的 maxLength，避免拒绝排版空白较多而正文合规的片段；去空白长度由运行校验执行。摘录存在和数值在摘录中两项检查仍分别保留。

每个 `numbers` 项包含 `value`、`unit`、`currency`、`source_unit`、`decimals`、`qualifier` 和 `label`。`value` 按 `unit` 计量；校验时按 `source_unit` 换算，再用摘录的小数位 `decimals` 格式化。`label` 说明是哪项数值、某个上下界或某币种的说明性折算。区间写两条 number，并用相同组名的上下界标签及维度保留关系，不把区间中点当实绩。

PDF 义务的 `detail.deadline.amount` 即使没有重复放进 numbers，也必须在某一条摘录的同一短语中与 deadline.unit 一起出现。支持阿拉伯整数，以及 0–99 的英文整数（zero 至 nineteen、twenty 至 ninety 的整十和空格/连字符组合）和中文整数（零至九十九，2 可写两）。期限单位只认 business/working days、calendar days/days、months、years 和相应中文工作日、自然日/日历日/天/日、个月/月、年；支持 six (6) months 这种词与括号数字并列。不会从其他金额、另一句里的单位、英文单词片段或 ISO/常见中文日期中凑期限；纯括号或负号数字不作正期限。英文百以上、序数词、分数、模糊期限等不猜读，需要保留限制并人工处理。基点为独立单位，不与利率水平或百分点自动换算。Choice 记录沿用无逐字摘录的例外。

单位枚举由 `records.UNITS` 导出，覆盖元、千元、万元、百万元、十亿元、亿元、每股/每芯公里金额、股及百万股、百分比和百分点、倍、吨及吨/年、长度、数量和期限。P1 全文复核另补衰减 dB/km、流量 GB/亿GB、员工平台份额/万份额、人数单位人、股东户数单位户，以及亿芯公里；GB、份额、芯公里各自只在同族单位间换算，不把平台份额自动当股票股数。币种由 `records.CURRENCIES` 导出，包括 CNY、HKD、USD 及 P1 外币注册资本涉及的其他币种，本轮复核补 KRW 韩元和 THB 泰铢。确实没有印单位时用 `未知`，附限制且不换算；不同单位间仅在同一量纲内作明确换算。允许的数值限定是 exact/approximate/more_than/less_than/at_least/at_most。exact 不允许因为小数位不足静默舍入。

- PDF 记录必须有页序及逐字摘录。传入冻结页映射时，locator.pdf_pages 的每一页都必须存在，包括没有单独摘录的表头或续表定位页。空白规范化不改字符、标点、繁简或大小写；两个数字字符之间的空白保留一个分隔空格，其余排版空白移除。数字字符包括数字本身，以及紧挨数字的点、逗号、百分号。摘录和冻结页使用同一规则，汉字之间及汉字与数字间的空格仍可忽略。数值检查支持千分位、小数位、半角/全角括号负数及 -、−、－ 三种负号（负号和数字之间可有空白），匹配完整数值 token，不能将 50 匹配成 150、-50 或 50.5，也不把表格相邻列的数字拼起来。跨页续表分别保存表头、正文摘录和页码。
- Choice 转换记录必须没有 PDF 页序、没有摘录，出处写返回文件资料编号、字段名和报告期；披露日填 `FIRSTNOTICEDATE`。记录不虚构报表页定位，走 Choice 查值报警。查不到的标待复核；对应报告缺失的标未查值。代码通过仅指声明的检查通过，不代表出处、主体、口径或研究接受。

覆盖与增量

`coverage_relations(records, cutoff)` 按披露日生成同键覆盖关系，不改记录；未知披露日不能进入历史最新值判断，同日冲突保留为并列。覆盖键在主体/指标/期间之外包含 `record_type`、`nature`、合并范围和 `dimensions`，防止陈述覆盖指标、预测覆盖实绩、对外收入覆盖分部间收入。对于 statement 记录、机构预测及转引，还包含陈述方（优先 detail.speaker，否则 origin.speaker）、origin.common_origin_id 和 origin.original_source，使不同机构及不同转引出处保留独立记录链。非 statement 的机构预测必须填写非空白 origin.speaker，避免把来源不明的机构预测合在同一覆盖链。原预测/本次预测若需并列仍在指标或维度中区分。单位本身不进键，允许后公告明确年产能口径时覆盖早公告未写每年的旧值；S48 与 S50 的同主体实绩产能不会因吨与吨/年不同而断开覆盖关系。

`supersedes` 可显式记录覆盖关系；同批记录之间会核查同键且新记录披露日晚于旧记录。批外旧编号须在 S2/S3 载入记录库时进一步核对，不宣称 S1 已有跨批存储实现。

必抽覆盖项为 `item_id/status/record_ids/checked_pages/basis`。状态为已抽、本文档无、本块未覆盖；已抽必须引用存在的记录，本文档无必须有已查页及依据。分块不等于全文，因此为本块未覆盖保留状态，S2 合并各块时再判断全文缺项。传入清单时校验编号集合恰好相同。三份清单覆盖 A 股定期报告、港股交易公告及 v2 4.3 要求在 S1 补齐的机构研报。

S1没有调用模型验证Schema；S2已经实测，结果与差异见下文。代码校验不推断主体同一性、数字所处行列的语义、单位是否与原文一致、金额是否应相加、重要性或是否漏抽，仍须有界人工复核。

S2调用与存储

`records.model_output_schema()`从上述合同投影出模型输出，不含由代码补齐的`source/disclosed_on/extraction_version/verification`。`extract_output_schema()`仍生成完整存储合同，S1接口和生成文件不变。模型局部记录编号由代码加调用块前缀，coverage引用同步改写；未提供旧记录时supersedes必须为空。prompt只放`field_guide()`生成的简明字段说明，不重复完整Schema。

期间写法统一：年度`2025`，半年`2026H1`，时点`2026-06-30`，机构预测`2026E`。`object_key.period == period.label`；主体使用文档法定全称；指标采用原表科目名，合并范围和分部、产品、地区等口径另放scope/dimensions。Choice资产负债表用时点标签，利润表和现金流量表用年度或半年标签，披露日取各期FIRSTNOTICEDATE。

完整模型输出Schema在CLI strict模式下被`uniqueItems`关键字拒绝。`model_output_schema(transport=True)`从同一定义移除可能不受支持的约束和注释，供传输；代码仍按完整定义检查日期、格式、枚举、重复、摘录与数值。实测原样错误及两份Schema只存在run目录，不能将传输兼容当作放宽记录合同。

`accept_model_output()`将顶层/覆盖结构失败与单条记录失败分开：后者只拒收该条，核验状态为拒收，并保留原输出和错误。引用被拒收记录或仅落只读上下文的“已抽”降为“未通过核验”。摘录同时必须存在于冻结页及真正送入该块的字符区间；全落只读上下文的合格记录被丢弃。文档级覆盖按任一块已抽、否则有带页码依据的本文档无、否则未覆盖合并。

`s2_qa.py`使用同一对象键和Decimal生成质检队列，包括同日冲突、跨日覆盖、合计/分部等式、A01与Choice严格键对齐及公告覆盖链。查值报警、约数/舍入差额和无法比较分别保存，均不自动提升为研究缺口或内容接受。

S2b 紧凑传输与核验分级

`extract-records-s2b-1` 仅改变模型传输，存储仍为 `wiki-records/1.0`。旧 S1/S2 校验默认路径保留；S2b 显式调用 `accept_model_output(..., compact=True)`。

紧凑输出的顶层为 `schema_version=wiki-compact/1.0`、`entities`、`tables`、`records`、`coverage`、`outside_index`。每个实体有 `id/type/names/canonical/renamed_from`；记录只引用编号，展开时规范名填入 `object_key.subject`。名称表的每个名称绑定冻结页路径和 SHA256，代码核实逐字出现后，按来源元数据 `company_code` 或研报 `coverage` 代码加入每块输入。名称表不构成跨文档主体登记表。

表级共享 `id/name/pages/header/entity/scope/unit/currency/extras`；列写 `label/period/kind/nature/as_of/unit/currency/dimensions/extras`；行写 `id/name/excerpts/values/extras`。必须定义所有原印数值列，包括历史、同比、占比、变动列。每行 values 数量必须等于列数；原印字符串如 `"1,234"`、`"(56)"` 由代码解析，NA/NM变为值状态，空字符串不生成记录并计数。每个非空单元格独立展开 metric，编号为 `行id-c001` 等；摘录是行摘录加表头摘录。coverage 引用表/行编号时自动展开至单元格。非连续段落必须分开保存，每段仍限500非空白字符。行摘录中数值多于非空单元格时，整行生成记录进入疑似漏列队列；行号、注号也可能触发，须回读。

非表格只写 `id/type/entity/indicator/period/kind/nature/as_of/excerpts/numbers/extras`。稀疏 `extras=[{key,value}]` 的 value 是 JSON 编码字符串，代码按允许字段集合解析并逐条校验类型。常见低频字段 `components/additivity/origin/dimensions/limitations/date_notes` 不用时由代码补默认空值，真正的业务细节不自动编造。表、行、列 extras 按顺序覆盖，JSON strict 所要求的全部固定键仍出现。生成函数为 `compact_model_output_schema(transport=True)`，传输兼容约束与本地完整校验分离。

硬规则失败拒收：字段类型、枚举、日期格式；摘录在冻结页及实际供给片段中连续出现；每段500字上限；每个结构化数值在摘录中出现。整数词复用 `_integer_words`；只有时间分母不同的同量纲单位（如吨与吨/年）按同刻度比，不扩展跨量纲换算。

软规则失败照收，verification=待复核，进入 `soft-queue`：total/component缺组；转引缺 original_source/common_origin_id；null日期缺逐字段说明；statement打包多指标/多期数值；疑似漏列；另将未知单位缺口径说明、机构预测缺陈述方、评级或目标价缺市场标记留作语义软提示。程序通过不等于语义接受；软问题数量按记录和规则分别计。

主体、期间、日期和维度的新规则：

- 公司事实用公司实体，规范名为本文档语言的法定全称。交易的对价、阶段、付款、条件、估值基准用交易实体，规范名为“收购方全称收购标的全称”，双方取本文档印出的名称；期间固定“本次交易”及 event，日期另存。标的公司产能和财务仍用标的公司。
- 已知事件发生日时 as_of 取发生日，未知则 null 并说明。状态和余额用其所处日期。仅月份的数据以2026-07等期间表达，as_of=null，不能用披露日代替。
- dimensions 只放分部、产品、地区、项目期别、对外/分部间、调整前后、原预测/本次预测等真正拆分。“本期数”“上年同期”不放维度；非期间列通过列 label 保留账面余额、坏账准备、计提比例、同比、占比等指标含义；已由真正拆分维度表达的列名不重复加入指标。
- Choice 同名原表行保留 original_table_item 作为查值依据，record_indicator 作为记录键。不同报表位置的补充资料加位置前缀，同一原行的旧/新字段只发出一个，依据写在字段表 `s2b_naming`。A01 主表指标保持原名。

coverage 只抽必抽清单项，本块内相关行列抽全，不能确定归属时纳入清单。清单外的小节和表格只写 `outside_index=[{page,title}]`。请求模式以请求编号和范围为输入，读取指定冻结页，并带只读续表表头上下文；使用同一紧凑输出和逐条核验。
