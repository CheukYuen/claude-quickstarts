# codex exec 调用记录

日期：2026-10-21。本文记录 wiki-lab 实际怎么调用 `codex exec`，命令都取自运行日志里的 `record.json`，不是示意。环境：`codex-cli 0.163.0`；模型和推理强度来自 `~/.codex/config.toml`（`model = "gpt-6.1-sol"`、`model_reasoning_effort = "ultra"`），命令里没有覆盖。

## 1. 一句话

Python 负责编排：每个阶段（或每个抽取块）起一个新的 `codex exec` 进程，prompt 从标准输入传进去，模型在 `-C` 指定的临时目录里工作；标准输出是 JSONL 事件流，最后一条消息另存成文件。进程结束后，Python 解析事件拿到用量，再检查工作区里改了哪些文件。

## 2. P1 实际命令

P1 共 18 次调用，命令形状完全相同，只有 `-C` 和 `--output-last-message` 两个路径不同。下面是 S46 公告抽取第 2 次尝试的原样命令（`record.json` 里的数组用 shell 引号拼起来）：

```text
codex exec --ephemeral --json --color never --sandbox workspace-write --disable memories --disable apps --disable plugins --disable multi_agent --config 'approval_policy="never"' -C /Users/leon/Developer/claude-quickstarts/managed-agents/knowledge-wiki/wiki-lab/runs/p1-20261021/extract/S46.c01/attempt-02 --output-last-message /Users/leon/Developer/claude-quickstarts/managed-agents/knowledge-wiki/wiki-lab/runs/p1-20261021/logs/extract-S46.c01/attempt-02/final.md -
```

拆成多行，并补上 Python 在进程外做的重定向，等价于：

```text
codex exec \
  --ephemeral --json --color never \
  --sandbox workspace-write \
  --disable memories --disable apps --disable plugins --disable multi_agent \
  --config 'approval_policy="never"' \
  -C runs/p1-20261021/extract/S46.c01/attempt-02 \
  --output-last-message runs/p1-20261021/logs/extract-S46.c01/attempt-02/final.md \
  - \
  < runs/p1-20261021/logs/extract-S46.c01/attempt-02/prompt.md \
  > runs/p1-20261021/logs/extract-S46.c01/attempt-02/events.jsonl \
  2> runs/p1-20261021/logs/extract-S46.c01/attempt-02/stderr.log
```

不要在已有的 run 目录里重放这条命令：它会改写工作区和日志。想自己试，见第 8 节。

`-C` 的目录有两种：抽取阶段是 `runs/<run>/extract/<块>/attempt-NN`，里面放 `input/source.md` 和 `input/chunk.json`；整编、补缺阶段是 `runs/<run>/work/<阶段>/attempt-NN`，里面是这一阶段要读的笔记和要改的页面的副本。

## 3. 每个参数做什么

| 参数 | 作用 | wiki-lab 为什么这样设 |
| --- | --- | --- |
| `exec` | 非交互模式：跑完一次任务就退出 | 由 Python 批量调度，没有人在终端前 |
| `--ephemeral` | 不把会话文件存盘 | 每次都是全新会话，不继承上一次的上下文；代价是不能 `resume` |
| `--json` | 标准输出改成 JSONL 事件流 | 存成 `events.jsonl`，用量和工具调用都从这里解析 |
| `--color never` | 不输出颜色控制字符 | 日志干净，方便解析 |
| `--sandbox workspace-write` | 模型执行的 shell 命令只能写工作区 | P1 需要模型在工作区里写笔记和页面；只限制写，不限制读 |
| `--disable memories` | 关闭记忆 | 不让以前会话的记忆影响这次结果 |
| `--disable apps` / `plugins` | 关闭应用和插件 | 只用本地文件，不接外部工具 |
| `--disable multi_agent` | 关闭子 agent | 一个阶段只有一个模型会话，便于审计和计量 |
| `--config 'approval_policy="never"'` | 从不请求人工审批 | 无人值守；值按 TOML 解析，所以 `never` 要带引号 |
| `-C <dir>` | 指定模型的工作根目录 | 指向一次性临时工作区；Python 先在里面 `git init`，让 Codex 找 git 根时停在这里，不跑到外层仓库 |
| `--output-last-message <file>` | 把最后一条消息写进文件 | 存成 `final.md`，是模型的收尾说明 |
| `-` | prompt 从标准输入读 | prompt 很长，用 stdin 传，不放在命令行参数里 |
| 未传 `-m` | 用 `config.toml` 里的模型 | 所以 `record.json` 的 `model` 字段写的是 "existing CLI configuration" |

`--disable <X>` 等价于 `-c features.<X>=false`。`-c` 和 `--config` 是同一个参数。

注意 `workspace-write` 只限制写：模型仍然可以读工作区外的文件。prompt 里"只读当前工作区"是约束文字，不是系统级隔离；wiki-lab 靠事件审计确认它没有读外部内容（见 [P1 交付记录](runs/p1-20261021/REPORT.md) 的 TRACE-AUDIT）。

## 4. Python 侧怎么调用

P1 在 [run.py:92](run.py:92) 的 `Pipeline.call()`。顺序是：

1. 工作区里没有 `.git` 就 `git init`（[run.py:95](run.py:95)）。
2. 在 `logs/<阶段>/` 下新建 `attempt-NN`，写 `prompt.md`，再写一份状态为 `running` 的 `record.json`，含命令数组、prompt 哈希、工作区文件哈希和允许修改的路径。
3. 拼命令数组（[run.py:103](run.py:103)），用 `subprocess.Popen` 启动，`start_new_session=True` 让它自成一个进程组。
4. `communicate(prompt, timeout=...)` 把 prompt 写进 stdin 并等待；超时（P1 默认 900 秒）先对整个进程组发 SIGTERM，5 秒后还没退出就发 SIGKILL（[run.py:121](run.py:121)）。
5. 用 codex-spike 的 `inspect_events()` 解析 `events.jsonl`，取出是否完成、用量和工具调用。
6. 重新算工作区哈希，列出改动的文件，交给 `checks.py` 检查是否越界；通过才写回 wiki 并提交。

## 5. 每次调用留下的文件

在 `runs/<run>/logs/<阶段>/attempt-NN/`：

| 文件 | 内容 |
| --- | --- |
| `prompt.md` | 通过 stdin 传入的完整 prompt |
| `record.json` | 命令数组、prompt 和输入输出哈希、状态、退出码、耗时、用量、改动文件 |
| `events.jsonl` | `--json` 的事件流 |
| `stderr.log` | CLI 的警告和错误 |
| `final.md` | `--output-last-message` 写出的最后一条消息 |
| `checks.json` | 程序检查结果（抽取阶段） |

## 6. 事件流长什么样

以 S46 抽取第 2 次尝试为例（4 页公告）：

- 事件：`thread.started` 1 条、`turn.started` 1 条、`item.started` 9 条、`item.completed` 13 条、`turn.completed` 1 条。
- 按 item 去重：7 次 shell 命令（`command_execution`）、2 次文件改动（`file_change`）、4 条模型消息（`agent_message`）。
- 模型执行的命令依次是 `rg --files …`、`wc -l -c input/source.md`、`cat input/chunk.json`、`sed -n '1,200p' input/source.md`、`sed -n '1,200p' sources/S46.c01.md`，以及两段自检用的 `python3` 脚本。
- 用量在最后的 `turn.completed` 里：

```json
{"type": "turn.completed", "usage": {"input_tokens": 179902, "cached_input_tokens": 152448, "cache_write_input_tokens": 0, "output_tokens": 15679, "reasoning_output_tokens": 3631}}
```

这就是 [DESIGN-v2](DESIGN-v2.md) 第 1 节说的"工具循环"：模型通过多次 shell 命令自己读文件、写文件，每多一轮，上下文就再读入一次，所以输入 token 远多于原文本身。

## 7. 18 次调用一览

| 阶段 / 尝试 | 状态 | 退出码 | 秒 | 输入 token | 其中缓存 | 输出 token |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| extract-S46.c01 / 01 | failed | 1 | 0.25 | 未报告 | | |
| extract-S46.c01 / 02 | completed | 0 | 401.67 | 179,902 | 152,448 | 15,679 |
| extract-S34.c01 / 01 | timed_out | -15 | 900.02 | 未报告 | | |
| extract-S34.c01 / 02 | completed | 0 | 633.90 | 1,481,329 | 1,361,920 | 22,435 |
| extract-S34.c02 / 01 | completed | 0 | 906.82 | 1,313,451 | 1,205,760 | 41,244 |
| extract-S34.c03 / 01 | completed | 0 | 1,070.02 | 1,655,451 | 1,537,408 | 44,937 |
| extract-S34.c04 / 01 | completed | 0 | 711.75 | 437,844 | 402,816 | 30,920 |
| extract-S42.c01 / 01 | timed_out | -15 | 900.02 | 未报告 | | |
| extract-S42.c01 / 02 | completed | 0 | 543.85 | 636,690 | 576,000 | 20,588 |
| extract-S48.c01 / 01 | completed | 0 | 573.71 | 331,150 | 293,632 | 24,975 |
| extract-S50.c01 / 01 | completed | 0 | 423.72 | 234,549 | 208,128 | 17,931 |
| entities / 01 | failed | -15 | 582.32 | 未报告 | | |
| entities / 02 | completed | 0 | 350.69 | 1,004,589 | 910,720 | 13,572 |
| company-trigiant / 01 | checks_rejected | 0 | 539.42 | 532,507 | 455,040 | 22,257 |
| company-yofc / 01 | checks_rejected | 0 | 602.68 | 2,036,890 | 1,868,288 | 30,318 |
| industry / 01 | completed | 0 | 590.93 | 1,657,722 | 1,542,272 | 22,572 |
| index / 01 | completed | 0 | 225.80 | 359,411 | 303,872 | 8,670 |
| resolve / 01 | completed | 0 | 415.35 | 899,125 | 803,712 | 17,080 |

退出码 -15 表示进程被 SIGTERM 终止。`checks_rejected` 表示模型正常结束，但产物没通过程序检查。

几种失败的原因（详见 `runs/p1-20261021/IMPLEMENTATION_ERRORS.json`）：

- **extract-S46.c01 / 01**：CLI 0.25 秒就退出，`stderr.log` 是 `failed to initialize in-process app-server client: Operation not permitted`，属于启动环境问题，模型还没开始工作。
- **两次 timed_out**：超过 900 秒，被 runner 终止。
- **entities / 01**：模型越过阶段职责，在实体目录下建了公司页，由人工只终止这一个进程组；拒收的改动保留在 `rejected/` 分支。
- **两次 checks_rejected**：检查器把合法的导航链接误判成畸形引用，并被页面里的否定句触发市场词拦截；修正检查器和措辞后接收，没有重新调用模型。

## 8. 自己试一次

在一个临时目录里跑一个只读的小任务，不会碰 wiki-lab 的数据：

```bash
dir=$(mktemp -d) && cd "$dir" && git init -q && echo '只回答一句话：当前工作目录里有哪些文件？' | codex exec --ephemeral --json --color never --sandbox read-only -C "$dir" --output-last-message "$dir/final.md" - > "$dir/events.jsonl" && cat "$dir/final.md"
```

跑完后可以看 `events.jsonl`，对照第 6 节认事件类型。这会产生一次真实的模型调用和用量。
