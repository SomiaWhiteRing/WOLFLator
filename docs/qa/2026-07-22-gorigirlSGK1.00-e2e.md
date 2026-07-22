# gorigirlSGK1.00 实机全流程归档（2026-07-22）

本文是给后续开发 Agent 的事实索引。它记录 WOLFLator 从架构收敛、可诊断 CLI、AiNiee 对接修正，到真实游戏产物贯通的过程。结论必须以这里列出的本地清单、日志和审计文件为准，不能从项目目录名或单次进程退出码推断。

## 先读结论

| 验收项 | 状态 | 说明 |
|---|---|---|
| 八阶段产物贯通 | 通过 | `copy -> unpack -> extract -> glossary -> translate -> validate -> import -> release` 最终全部为 `completed` |
| 原始游戏只读 | 通过 | 5 个文件、447,755,102 字节，目录哈希前后一致 |
| 翻译数据完整性 | 通过 | 18,631 个 AiNiee 输入键全部回收；无缺键、额外键、控制符占位残留或 COPY 行译文 |
| 默认危险导入范围 | 通过但有边界 | 最终导入表中 `<FILENAME>`、纯半角、COPY 行译文均为 0；另有 185 个可选名称源行因显示文本 `COPY-FROM` 依赖而保留，详见“未决事项” |
| 发布目录结构 | 通过 | 2,042 个文件、453,078,153 字节，含 `Game.exe` 与 `Data/BasicData/Game.dat`，无 `*.wolf` |
| 干净项目单命令一键通过 | **尚未证明** | 翻译结果经过针对性恢复并人工认证；导入和发布在修复代码后分阶段重跑 |
| 发布版启动验收 | 部分通过 | `Game.exe` 约 5 秒出现初始化窗口；用户按 Escape 中断自动化，未完成 60 秒响应性检查和截图 |

禁止把本次结果描述为“全新项目一次 `cli.py run` 无干预通过”。准确说法是：**真实游戏的完整产物链已经贯通，恢复、校验、导入和发布都留下了可复核证据；还需要一次干净项目的一键回归作为最终验收。**

## 固定证据位置

```text
仓库：C:\Users\旻\Documents\GitHub\WOLFLator
游戏：C:\Users\旻\Downloads\gorigirlSGK1.00
项目：C:\Users\旻\Documents\WOLFLator\gorigirlSGK1.00-e2e-5ed4264-20260722-085115
版本：2026-07-22T005143+0000
产物：<项目>\versions\2026-07-22T005143+0000\artifacts
发布：<项目>\versions\2026-07-22T005143+0000\release
```

项目 ID 中的 `5ed4264` 只表示创建项目时的代码基线。最终验收时仓库 `HEAD` 为 `33743e6`，并叠加了本次实机发现的未提交修复。判断代码状态必须查看 `git status` 和 `git diff`，不能只看项目名。

核心证据：

- `project.json`：最终阶段状态、输入哈希、产物路径和源目录哈希。
- `artifacts/logs/20260722-085208-742-one-click.log`：首次真实运行，包含各阶段耗时及初次翻译缺键失败。
- `artifacts/ainiee-recovered-complete-audit.json`：翻译恢复的权威审计。
- `artifacts/ainiee-recovered-complete-output.json`：恢复后的完整 AiNiee 输出。
- `artifacts/items-translated-recovered.json`：通过 WOLFLator 合并校验的完整译文项。
- `artifacts/logs/20260722-110404-424-one-click.log`：校验成功和首次导入参数错误。
- `artifacts/logs/20260722-111708-978-import.log`：修正危险范围后的最终官方导入。
- `artifacts/logs/20260722-111851-401-release.log`：最终发布。
- `artifacts/import-scoped.xlsx`：最终交给官方工具的范围化工作簿。

不要删除这个项目。它是当前唯一包含首次失败、针对性恢复、最终工作簿和发布目录的完整实机证据集。

## 架构与测试策略如何收敛

### 1. 不再解析 WOLF 二进制

WOLFLator 最终只编排已有工具：

- UberWolf 负责把 `Data.wolf` 解成松散 `Data`。
- 官方 WOLF Translation Support Tool 的 XLSX 是唯一文本协议。
- AiNiee-Next 负责 Paratranz JSON 翻译。
- WOLFLator 负责复制、分类、范围、控制符保护、产物校验、恢复和发布。

UberWolf 必须从纯 ASCII 目录运行。当前实机使用 `C:\Users\Public\WOLFLator\bin\UberWolfCli.exe`；非 ASCII 路径曾稳定触发 `CreateProcess() failed: 2`。

### 2. 依赖准备从运行期移到设置期

早期实现每次启动 AiNiee 都执行 `uv sync`，导致测试长时间停留在依赖安装且难以区分安装问题和翻译问题。当前边界是：

- 安装、修复或选择 AiNiee 时调用 `prepare_managed_runtime()`，完成复制和 `uv sync --frozen`。
- 翻译时只调用 `require_managed_runtime()` 检查 `.venv`、`.uv-sync` 和 `uv.lock` 指纹。
- 正式翻译命令使用 `uv run --frozen --no-sync`，不得偷偷安装依赖。

### 3. CLI 与 GUI 共用业务逻辑

直接跑打包 GUI 的首次实机测试过慢且无法精确定位问题，因此增加了源码 `cli.py`。CLI 使用和 GUI 相同的 Qt identity、`SettingsStore`、DPAPI 密钥、`Pipeline` 与项目清单。

后续诊断优先使用：

```powershell
.\.venv\Scripts\python.exe cli.py settings-check
.\.venv\Scripts\python.exe cli.py api-test --target glossary
.\.venv\Scripts\python.exe cli.py api-test --target translation
.\.venv\Scripts\python.exe cli.py status "<project.json>" --json
.\.venv\Scripts\python.exe cli.py run "<project.json>" --stage <stage>
```

GUI 只做轻量交互验收，不应成为诊断前置条件。

### 4. 本地详细日志是主要故障证据

应用内日志只显示人能快速阅读的进度。本地日志额外记录：

- 阶段输入哈希和清单原子保存点。
- 外部命令、PID、退出码、时长、stdout/stderr 行数。
- API 请求号、重试、超时、响应状态和异常正文。
- AiNiee session 日志路径及输出文件结构。
- Python 完整堆栈。

不要仅凭 `AiNiee exit=0` 判定成功。本次 AiNiee 返回 0，但输出仍少 2,054 个键，正是结构校验抓到了真实失败。

### 5. 术语与翻译 API 分开

术语生成和正文翻译的负载完全不同，现已分开保存 API 地址、模型、密钥、线程和超时。实机最终使用：

```text
术语：deepseek-v4-pro
翻译：deepseek-v4-flash
术语输入块：500,000 字符
术语 max_tokens：393,216
正文分块：Token 模式，256 Token；8 行仅作为行模式参数
正文目标重试轮次：6
```

首次术语运行日志中的实际总超时仍是 180 秒，出现 3 次超时后通过重试完成。随后设置中的术语超时已调整为 600 秒。修改设置会改变阶段输入哈希；恢复旧项目时不要惊讶于流水线要求重做阶段。

### 6. 全量导出，翻译范围与导入范围独立

官方工具始终全量导出，并额外生成关闭名称项的基准 XLSX。WOLFLator 根据两份表的差分分类可选名称；翻译范围控制送给 AiNiee 的行，导入范围控制最终 XLSX 中保留的译文。

本次两套范围均为：

```json
{
  "display": true,
  "external": false,
  "optional_name": false,
  "halfwidth": false,
  "filename": false
}
```

## 实机时间线

### 初始一键运行

| 阶段 | 结果 | 关键数据 |
|---|---|---|
| copy | 完成 | 约 4.3 秒，记录源目录哈希 |
| unpack | 完成 | 12.3 秒，UberWolf 退出 0 |
| extract | 完成 | 约 131 秒，生成全量表和名称基准表 |
| glossary | 完成 | 550 秒，语料 1,543,667 字符，4 块，144 个角色、605 个术语 |
| translate | 失败 | AiNiee 运行 2,199.6 秒并退出 0，但仅返回 16,577/18,631 行 |

首次失败为：

```text
ValueError: AiNiee 输出键集合不一致: missing=2054, extra=0
```

这证明“外部进程退出 0 后必须校验产物结构、键集合和译文完整性”不是可选防御。

### 翻译恢复与代码改进

恢复只处理已证实失败的行，没有重跑 18,631 行全文：

```text
输入行                         18,631
首次有效译文                   16,577
AiNiee 明确排除、经规则恢复       224
第一次针对性恢复                1,796
第二次针对性恢复                   34
控制符专项替换                     27  （与上述行重叠，不增加键数）
最终键数                       18,631
```

权威校验结果：

```text
missing_rows=0
extra_rows=0
merge_validated=true
private_use_placeholders_after_merge=0
copy_rows_with_translation=0
完整 AiNiee 输出 SHA-256=66934384b190f2e5aaec20ee1ac609e538c309b1e8381b8f0e5f2417898e525a
译文 items SHA-256=72bd02448cc0e65d70ea9e7c997d0253494b3a081e8905af7339cf8813549a55
```

一次人工重试在 1,017 秒后报告 `OSError: [Errno 22] Invalid argument`，但保留了可审计输出。当前代码已有窄控制台、日志写入和“退出 0 但无产物”回归测试；由于没有在当前代码上从头复现，该错误只记为历史回归项，不能继续猜测根因。

当前正式实现会在首轮后筛选 `missing`、空译文和控制符错误，只为这些行开启一个新的 AiNiee session 重试一次；不会无限重试，也不会重翻已成功行。相关逻辑已进入 `33743e6`。

### 标记翻译完成后继续下游

恢复产物通过校验后，标准 `ainiee-output.json` 和 `items-translated.json` 被原子替换，翻译阶段使用当前输入哈希认证为完成。

第一次继续运行时，因为术语超时配置已变化，glossary 输入哈希失效并开始重做。该次运行被立即终止；旧 `glossary.json` 的长度、时间和内容未变化。确认产物后重新认证 glossary/translate 的当前输入哈希，再继续下游。这是本次不是“纯一键通过”的原因之一。

### validate

`20260722-110404-424-one-click.log` 记录：

```text
expected_rows=57985
workbook_rows=57985
missing=0
```

`translated-full.xlsx` 可被 openpyxl 回读。

### import 的两个实机缺陷

1. `_import()` 调用 `_official_runner()` 时漏传 `scope`，在写完范围表后抛出：

   ```text
   TypeError: Pipeline._official_runner() missing 1 required positional argument: 'scope'
   ```

   修复为 `_official_runner(self.manifest.import_scope)`，并增加针对性测试。

2. 可选名称差分会把带 `<FILENAME>` 的源行重分类为 `optional_name`，跨类别 `COPY-FROM` 分组随后丢失其固有文件名危险属性。首次导入表因此保留了 13 条文件名译文。

   修复为：分组时额外保留源行固有的 `FILENAME`/`HALFWIDTH` 标记。增加“可选名称 + 文件名 + 显示 COPY”回归测试后，重新执行 import 和 release。

最终官方工具结果：

```text
CREATE_FOLDER exit=0，14.3 秒
TRANSLATE     exit=0，19.5 秒
```

### release 与启动烟测

最终发布目录：

```text
文件数：2,042
总字节：453,078,153
Game.exe：存在
Data/BasicData/Game.dat：存在
*.wolf：0
```

发布阶段再次校验原目录哈希并完成原子替换。随后启动发布版，约 5 秒出现标题为 `Starting up, initializing system ...` 的窗口。10 秒后的画面/响应抓取被用户按 Escape 中断；不要把它写成完整 60 秒启动验收，测试进程也可能由用户手动关闭。

## 最终数据审计

```text
原始目录文件数：5
原始目录总字节：447,755,102
原始目录 SHA-256：9d41ffc53130f4c6750d56ab8e9b7f6ca797abb6ef5fede757dcf22936836235
最终复核：一致

全量 XLSX 数据行：57,985
AiNiee 输入行：18,631
完整工作簿非空译文：18,635（含 4 个 WOLFLator 托管字体项）
最终导入工作簿非空译文：18,622
<FILENAME> 非空译文：0
纯半角非空译文：0
COPY-FROM 非空译文：0
私用区占位符单元格：0
```

最终完整测试：

```text
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
Ran 71 tests
OK
```

`git diff --check` 通过。最终成功日志未匹配 `ERROR`、`Traceback`、API key 或 `Bearer` 特征。

## 当前工作树，不要误删

归档创建前，`HEAD` 为 `33743e6`，以下实机修复仍未提交：

- `pipeline.py`：将当前导入范围传给官方工具 runner。
- `wolf_tools.py`：跨类别分组保留固有文件名/纯半角危险属性。
- `tests/test_pipeline.py`：runner scope 回归测试。
- `tests/test_core.py`：可选名称重分类不得绕过文件名范围的回归测试。

本归档文档本身也是新增文件。后续 Agent 应先运行 `git status --short`，不得 reset 或 checkout 掉这些修改。

## 未决事项

### 1. 可选名称与显示 COPY 的范围语义

最终 `import-scoped.xlsx` 仍保留 185 个内部分类为 `optional_name` 的源行，因为启用的显示文本通过 `COPY-FROM` 引用它们。当前策略优先保证显示文本覆盖：COPY 行保持空白，译文写在唯一源行。

这与“可选名称及引用默认关闭”的严格字面含义有冲突。两个可行策略不能同时满足：

- 严格范围：整个混合分组不导入；可选名称安全，但对应显示文本保持日文。
- 显示覆盖优先：保留源行；显示引用能翻译，但源名称也会被官方工具导入。

在用户明确选择之前不要擅自删除间接流支持，也不要宣称可选名称已严格为 0。

### 2. 干净的一键回归

必须新建项目，不复用当前 `project.json`、恢复输出或阶段状态，只执行一次：

```powershell
.\.venv\Scripts\python.exe cli.py run "<新项目>\project.json"
```

成功条件：八阶段均由本次运行完成，无 `skipped`、人工认证、旧产物复制或运行中改代码。当前的失败行自动重试需要在这次回归中接受真实验证。

### 3. 发布版完整启动验收

需要重新启动最终 `release/Game.exe`，确认 60 秒内进入实际游戏画面、窗口持续响应、没有缺文件或崩溃对话框，并保存截图。上次只证明了进程和初始化窗口能出现。

## 后续 Agent 的最短操作路径

1. 先读本文和当前 `git diff`，运行 71 项测试。
2. 确认两套 API 测试返回真实非空正文，不使用固定回复标记。
3. 用 `settings-check` 和 `require_managed_runtime()` 验证 AiNiee；运行时不再执行 `uv sync`。
4. 先决定 185 个可选名称间接依赖的范围策略。
5. 新建唯一项目执行一次干净 `run`，期间只观察，不修代码、不改清单、不复制本归档产物。
6. 失败时保存清单、日志和产物，随后只重跑失败阶段以判定稳定/瞬态问题；首次一键仍记失败。
7. 完成后复核原目录哈希、XLSX 键集合、控制符、危险范围、发布结构和 60 秒启动烟测。

不要再次进行无上限、无阶段证据的打包 GUI 测试。源码 CLI、阶段硬时限、详细日志和失败阶段最小重跑是本项目已经验证过的诊断路径。
