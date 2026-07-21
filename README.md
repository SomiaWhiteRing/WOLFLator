# WOLFLator

WOLFLator 将 UberWolf、WOLF Translation Support Tool for FreeGames 和 AiNiee-Next 组合为一个可恢复的 Windows 桌面翻译流程。v1 只支持日文到简体中文，发布结果是带松散 `Data` 目录的完整游戏副本。

## 开发运行

```powershell
python -m pip install -r requirements.txt
$env:WOLFLATOR_UBERWOLF="C:\path\to\UberWolfCli.exe" # 未拉取 vendor 时可选
python app.py
```

无需打包或打开图形界面时，可直接使用源码 CLI。它读取同一份设置和当前用户的 DPAPI 密钥：

```powershell
python cli.py settings-check
python cli.py ainiee-prepare                 # 安装固定版本并准备依赖
python cli.py project-create "C:\Games\MyWolfGame"
python cli.py run "C:\Users\me\Documents\WOLFLator\MyWolfGame\project.json"
python cli.py status "C:\Users\me\Documents\WOLFLator\MyWolfGame\project.json" --json
```

`run --stage translate` 可只执行单个阶段；`scope`、`skip`、`retry` 与图形界面中的对应操作一致。失败时命令返回非零退出码，并保留项目版本目录下的外部日志和完整 Python 堆栈。

首次启动需要指定：

- 自行取得的 WOLF Translation Support Tool for FreeGames EXE；同目录必须有 `LibXL.dll`。
- 已安装/解压的 AiNiee-Next，或在设置窗口点击“安装 V2.7.5”。
- OpenAI 兼容 API 的基础地址、模型和密钥。
- 项目目录和纯 ASCII 的 UberWolf 执行目录。

原始游戏只读使用。实际解包、官方 XLSX、AiNiee 输入输出和发布物都位于版本化项目工作区中。

## 构建 Windows 发行包

```powershell
.\scripts\build.ps1
```

构建脚本会下载固定版本的 UberWolfCli 与 uv，验证 `vendor/manifest.json` 中的 SHA-256，运行测试，再生成 `dist\WOLFLator`。AiNiee 和官方 WOLF 工具不会打入发行包。

## 项目数据

```text
<项目目录>/<项目ID>/
  project.json
  glossary.json
  versions/<版本ID>/
    source/       原始游戏副本
    work/         外部工具工作目录
    artifacts/    XLSX、Paratranz JSON 和校验结果
    release/      最终可运行游戏
```

API 密钥通过当前 Windows 用户的 DPAPI 加密保存。AiNiee 运行时采用隔离副本，带密钥的 session profile 会在任务结束及下次启动时删除。
