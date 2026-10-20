# 实验语料

本次只使用 4 份原始材料，合计 18 页。实验输入是英文逐页原文，程序仅加来源标记，不生成摘要，也不读取现有研究报告、研究结论或问答答案。

| Source ID | 原始 Source ID | 日期 | 性质 | 输入页序 |
| --- | --- | --- | --- | --- |
| `S01` | `1300.HK-20260913-announcement-qinghai-possible-acquisition` | 2026-09-13 | 俊知拟收购公告 | 全部 4 页 |
| `S02` | `1300.HK-20260920-announcement-qinghai-acquisition` | 2026-09-20 | 俊知收购公告 | 全部 8 页 |
| `S03` | `1300.HK-20260925-announcement-qinghai-supplemental` | 2026-09-25 | 进度及补充公告 | 全部 4 页 |
| `S04` | `report-dbs-20261005-trigiant-equity-explorer` | 2026-10-05 | DBS Equity Explorer 研报 | PDF 第 2、5 页，共 13 页中的 2 页 |

公告逐页原文位于 `invest-lab/runs/trigiant-research-20261015/pre-price/official/text/`；相应原件位于 `/Users/leon/Stock/invest-sources/filings/1300.HK/`。程序只读原件核验 SHA-256，逐页文本来自 `invest-lab`。原始材料登记表 `source-records.json` 只用于来源身份、日期和文件绑定，不提供研究结论。

DBS 原件位于 `invest-lab/runs/trigiant-research-20261015/held/sources/reports/fxbaogao/report-dbs-20261005-trigiant-equity-explorer/`。程序用本机 `pdftotext -layout` 提取选定页，并将原始提取文本保存到本次 run 的 `source-text/`。第 2 页包含经营叙述和假设，第 5 页包含经营假设及分部表。标题和日期已在原 PDF 第 1 页核对，第 1 页不作为模型输入。

此语料含 2026-10-05 研报，不能称为 2026-09-30 截点资料。它也不覆盖公告以外的全部交割资料，或研报未选页内容。模型须区分公司披露、券商预测和未解决问题。

选料时已重新用 `pdftotext -layout` 从绑定 PDF 提取 16 页公告，与既有逐页文本逐词核对；所有页面只存在空白布局差异，词序和字符内容一致。DBS 所选第 2、5 页均渲染查看，确认章节和表格对应。语料准备实跑得到约 48,600 字符（含路径及来源包装，具体值随 run 路径长度变化）。

## 程序做的事

`prepare_corpus.prepare(run_dir)`：

1. 核对固定配置中的 4 份 PDF 哈希、来源登记表哈希，以及 16 页既有文本哈希；发生变化就报错。
2. 读取 16 页既有原文，并逐页提取 2 页 DBS 文本。
3. 给每页添加 `[SOURCE: ... | PDF_PAGE: ... | DATE: ...]` 和 `[PAGE: N]`，写入 `run_dir/corpus/<source-id>.md`。问答引用统一用 `[S01 p1]` 格式。
4. 写出 `corpus-manifest.json`，保留原件路径、PDF 哈希、原始文本路径及哈希、选页、字符量、规范化文件路径及哈希。

哈希检查只能证明当前文件与本次固定输入一致，不能证明事实正确、抽取完整或研究已经验收。

## 中性问答建议

1. 按这批资料，青海中利收购从拟议、签约到完成经历了哪些时间节点？分别给出来源和页码。
2. 股权收购对价、承接债务或其他义务、会计负债分别是什么口径？能否直接相加？资料哪里有说明，哪里仍不明确？
3. 语料中的光棒产能、销量、售价和利润数字分别来自公司披露还是 DBS 估计？哪些是已有经营数字，哪些带有预测假设？
4. 仅靠这批资料，能否确认交割后的实际产量、实际毛利和最终现金支出？若不能，缺哪些证据？
