# Codex Session Monitor

本地、只读的 Codex 会话监控器。它在一个浏览器页面或 Windows 桌面窗口中，实时汇总多个项目的会话、任务状态、工具调用和结果。

## 环境要求

- Python 3.12+
- [`uv`](https://docs.astral.sh/uv/)
- 浏览器开发：Node.js 和 npm
- Windows 桌面开发：Rust stable、Visual Studio 2022 C++ Build Tools、WebView2、Node.js、Python 和 `uv`

## 启动服务

### 方式一：使用 Python 命令

```powershell
uv sync --dev
uv run codex-monitor --project D:\path\to\project-a --project D:\path\to\project-b --port 8000
```

然后打开 <http://127.0.0.1:8000>。`--project` 可以重复传入多个项目，默认读取 `%USERPROFILE%\.codex\sessions`；测试或演示时可使用 `--session-root` 指向脱敏数据目录。需要完全忽略本机已保存项目时，可追加 `--no-saved-projects`。

### 方式二：使用仓库启动脚本

PowerShell：

```powershell
.\start-codex-monitor.ps1
```

脚本支持 `-Project`、`-SessionRoot`、`-Port` 和 `-StuckSeconds` 参数。`-Project` 可以重复传入多个本地项目。

Windows 用户也可以双击：

```text
start-codex-monitor.cmd
```

它默认监控当前仓库并在服务就绪后打开浏览器。

## 前端浏览器开发

```powershell
cd frontend
npm run dev
```

这个命令会自动启动本地监控服务（默认 `127.0.0.1:8766`），再启动 Vite 前端（默认 `127.0.0.1:5173`）。关闭命令后，脚本只会回收本次自动启动的服务。若端口被其他程序占用，脚本会明确提示；可通过 `CODEX_MONITOR_PORT` 和 `CODEX_MONITOR_VITE_PORT` 调整端口。

如果监控服务已经由其他方式启动，只需要运行纯 Vite：

```powershell
cd frontend
npm run dev:vite
```

浏览器开发页面默认地址为 <http://localhost:5173/static/>。

页面中导入的项目会保存到本机配置，重启后恢复；也可以在项目列表中移除不再需要监控的项目。移除只改变监控配置，不删除项目目录或 Codex 会话文件。

## Windows 桌面开发

推荐使用完整启动脚本，它会在 sidecar 缺失或过期时构建 sidecar，然后启动 Vite 和 Tauri：

```powershell
.\scripts\start-desktop-dev.ps1
```

也可以从 `frontend` 目录直接启动 Tauri：

```powershell
cd frontend
npm run desktop:dev
```

直接启动会自动准备 Vite，但不会替代 sidecar 构建；首次运行或 sidecar 更新后请先执行：

```powershell
.\scripts\build-sidecar.ps1             # 首次构建 sidecar
.\scripts\build-sidecar.ps1 -Refresh   # 已有 sidecar 时刷新构建
.\scripts\build-sidecar.ps1 -VerifyOnly # 仅校验现有产物
```

桌面开发使用仓库根目录下被 Git 忽略的 `.cargo-target` 作为 Rust 构建缓存，避免扫描受限的旧缓存目录。

## 构建与检查

```powershell
cd frontend
npm run typecheck       # TypeScript 类型检查
npm run build           # 浏览器生产构建
npm run build:desktop   # 桌面端前端资源构建
npm run e2e             # 浏览器冒烟测试
```

Windows 安装包和签名发布流程见 GitHub Releases；本地无签名构建仅用于开发验证。

## 主要功能

- 跨项目查看 Codex 会话和实时任务时间线。
- 注意力收件箱集中显示工具失败、任务中止和疑似卡住的事项，可标记处理并从通知直达相关会话。
- 支持导入项目、历史定位、筛选和窄窗口布局。
- 桌面端使用 Tauri 原生通知，浏览器端使用 Web Notifications API。
- 设置页提供本地脱敏日志摘要、日志目录打开、复制摘要、清空和导出功能。
- 桌面端可在“设置 > 关于”中查看当前构建版本并手动检查更新。

## 隐私与边界

- 服务只绑定 `127.0.0.1`，不会主动暴露到局域网或公网。
- 监控器只读 Codex JSONL，不删除、修改或写回会话文件。
- 页面可能显示提示词、回复和工具摘要，请勿把真实 `%USERPROFILE%\.codex\sessions` 数据提交到 Git。
- 诊断日志仅保存在本机应用数据目录，不会自动上传，并按产品规则进行脱敏。

版本变化见 [`CHANGELOG.md`](CHANGELOG.md)；本项目只读取本机 Codex 会话，不会上传或同步会话内容。
