版本：resolve-1

你负责P1最后一次有预算补缺。只能读当前目录笔记、页面、_sources.json 和 raw/ 下提供的选定原文；不读外部路径、网络、记忆，不启动子agent。只允许修改 {{ALLOWED}}，其他文件保持不变。

先搜来源笔记，对主体、交易阶段、金额口径、表格脚注、实绩/预测等高影响缺口优先回查raw。原文预算最多 {{PAGE_BUDGET}} 个不同PDF页面，raw目录已由程序按预算提供，不得请求预算外资料。创建 resolutions.json：{page_budget: {{PAGE_BUDGET}},raw_pages_read:[{id,pages}],items:[{gap_id,status,checked_scope,answer,cite,next_source}]}。只记录实际读取的页数。checked_scope用数组；回原文的项采用{id:"S12",pages:[5,6]}，仅查笔记可用路径字符串。声明“资料里没有”时，checked_scope必须包含实际raw_pages_read中的具体原文页，并加raw_checked:true。没有答案时answer可空串，但next_source必须说明需要哪份资料。

本次最多处理8个高影响缺口，未选中的保留原状态，不宣称全库补齐。更新三个对象 gaps.md 的真实状态；必要的事实修正只在允许的归属页进行。状态为“已解决”“资料里没有”“没查完”。已解决必须有cite；资料里没有必须 raw_checked:true，明确查过的原文页码，且结论限定于这些页；其余维持没查完和下一份资料。同步更新changes.md记录实际回查范围、解决和未解内容、本次改了哪些页。不要假装已检查整个语料。未进入小样的资料不得利用。每条事实引用语法 [S12 p34] 或 [S12 pp34–36] 或 [S12 p5; S40 p8]。不改来源笔记。此次完成后停止，不增量、不P2。
