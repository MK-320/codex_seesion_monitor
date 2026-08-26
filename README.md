# Codex Session Monitor

Codex Session Monitor 是一个本地、只读的 Codex 会话监控器，可在浏览器或 Windows 桌面窗口中汇总多个项目的会话、任务状态、工具调用和结果。

## 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)
- 浏览器模式：Node.js 和 npm
- Windows 桌面模式：Rust stable、Visual Studio 2022 C++ Build Tools、WebView2、Node.js、Python 和 uv

## 使用方式

### 直接启动本地监控服务

```powershell
uv sync --dev
uv run codex-monitor --project D:\path\to\project-a --project D:\path\to\project-b --port 8000
```

然后打开 `http://127.0.0.1:8000`。`--project` 可以重复传入多个项目；默认读取当前用户的 Codex 会话目录。也可在界面中使用“导入项目”添加项目，已保存项目会在重启后恢复；移除项目仅从监控清单中移除，不删除项目目录或 Codex 会话文件。测试或演示时可使用 `--session-root` 指向脱敏数据目录，并可使用 `--no-saved-projects` 忽略已保存项目。

### Windows 浏览器开发

```powershell
cd frontend
npm ci
npm run dev
```

浏览器开发页面默认地址为 `http://localhost:5173/static/`。如果只需要 Vite：

```powershell
npm run dev:vite
```

### Windows 桌面开发

```powershell
.\scripts\start-desktop-dev.ps1
```

首次运行或 sidecar 更新后：

```powershell
.\scripts\build-sidecar.ps1
```

也可以从 `frontend` 目录启动：

```powershell
npm run desktop:dev
```

### 检查与构建

```powershell
cd frontend
npm run typecheck
npm run build
npm run build:desktop
npm run e2e
```

## 主要功能

- 跨项目查看 Codex 会话和实时任务时间线。
- 集中显示工具失败、任务中止和疑似卡住的关注事项，可标记处理并定位到相关会话。
- 支持项目导入、历史定位、筛选和窄窗口布局。
- 桌面端使用 Tauri 原生通知，浏览器端使用 Web Notifications API。
- 设置页提供本地脱敏日志摘要、日志目录打开、复制摘要、清空和导出功能。
- 桌面端可在“设置 > 关于”中查看版本并手动检查更新。

## 隐私与安全

- 服务只绑定 `127.0.0.1`，不会主动暴露到局域网或公网。
- 监控器只读 Codex JSONL，不删除、修改或写回会话文件。
- 页面可能显示提示词、回复和工具摘要，请勿把真实会话数据提交到 Git。
- 诊断日志仅保存在本机应用数据目录，不会自动上传，并按产品规则脱敏。

## Windows 发布说明

Windows 正式发布包使用自签名证书。自签名证书可以验证发布者身份和包完整性，但默认不会被 Windows 公共信任链信任，首次安装时可能显示“未知发布者”。

版本更新清单见 [`CHANGELOG.md`](CHANGELOG.md)。