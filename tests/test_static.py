import re
from pathlib import Path

from fastapi.testclient import TestClient

from codex_monitor.api import create_app
from codex_monitor.config import AppConfig


def _app(tmp_path: Path) -> TestClient:
    session_root = tmp_path / "sessions"
    session_root.mkdir(exist_ok=True)
    return TestClient(create_app(AppConfig(project_root=tmp_path, session_root=session_root)))


def test_dashboard_static_entrypoint(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        page = client.get("/")

    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert 'id="root"' in page.text
    assert "Codex Session Monitor" in page.text
    script_match = re.search(r'src="(/static/assets/[^\"]+\.js)"', page.text)
    assert script_match is not None
    with _app(tmp_path) as client:
        script = client.get(script_match.group(1))
    assert script.status_code == 200


def test_dashboard_serves_built_stylesheet(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        page = client.get("/")

    style_match = re.search(r'href="(/static/assets/[^\"]+\.css)"', page.text)
    assert style_match is not None
    with _app(tmp_path) as client:
        stylesheet = client.get(style_match.group(1))
    assert stylesheet.status_code == 200


def test_static_entrypoint_references_tracked_build_assets() -> None:
    static_root = Path("src/codex_monitor/static")
    page = (static_root / "index.html").read_text(encoding="utf-8")
    references: list[str] = [
        match.group(1) for match in re.finditer(r'(?:src|href)="/static/([^\"]+)"', page)
    ]

    assert references
    assert all((static_root / reference).is_file() for reference in references)


def test_repository_pins_toml_worktree_files_to_lf() -> None:
    attributes = Path(".gitattributes").read_text(encoding="utf-8")

    assert "*.toml text eol=lf" in attributes


def test_frontend_uses_structured_attention_contract() -> None:
    sources = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in ("frontend/src/App.tsx", "frontend/src/format.ts")
    )

    assert 'endsWith("· error")' not in sources
    assert "attention_reasons" in sources
    session_source = Path("frontend/src/useSessions.ts").read_text(encoding="utf-8")
    assert "protocol_version !== 1" in session_source


def test_frontend_declares_bounded_attention_registry() -> None:
    source = Path("frontend/src/useAttentionRegistry.ts").read_text(encoding="utf-8")

    assert '"codex-monitor.attention-registry.v1"' in source
    assert "30 * 24 * 60 * 60 * 1000" in source
    assert "MAX_ENTRIES = 1000" in source
    assert "schema_version: 1" in source
    assert re.search(r"handled_at\s*(?:<=|>)\s*(?:now\(\)|currentTime)", source)
    assert "memoryOnly" in source
    assert "handleMany" in source
    assert "undoMany" in source
    assert source.count("localStorage.setItem") == 1
    assert all(
        field not in source
        for field in (
            "session_id",
            "project_root",
            "turn_id",
            "tool_name",
            "current_user_message_summary",
        )
    )


def test_frontend_declares_search_filter_sort_and_url_state() -> None:
    source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert "URLSearchParams" in source
    assert 'aria-label="搜索会话"' in source
    assert 'aria-label="时间范围"' in source
    assert 'aria-label="会话排序"' in source
    assert "清除筛选" in source
    assert "current_user_message_summary" in source
    assert "setMobileDetailOpen(false)" in source


def test_frontend_declares_connection_freshness_states() -> None:
    hook = Path("frontend/src/useSessions.ts").read_text(encoding="utf-8")
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert '"connecting" | "online" | "reconnecting" | "stale"' in hook
    assert "lastSyncedAt" in hook
    assert "retryCount" in hook
    assert "最后同步" in app


def test_frontend_declares_long_timeline_workflows() -> None:
    hook = Path("frontend/src/useSessions.ts").read_text(encoding="utf-8")
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert '"loading" | "error" | "not_found" | "ready"' in hook
    assert "loadEarlier" in hook
    assert "加载更早" in app
    assert "仅看错误" in app
    assert "跳到最新" in app
    assert "element.scrollTop = element.scrollHeight" in app
    assert "document.scrollingElement" in app
    assert "workspaceRoot" in app
    assert "followLatest.current = true" in app
    assert 'type FollowMode = "idle" | "following" | "paused" | "historical" | "ended"' in app
    assert "setNewContentCount" in app
    assert "follow-indicator" in app
    assert "follow-new-count" in app
    assert "settleLatest" in app
    assert "distanceFromBottom" in app
    assert "onPointerDown={stopFollowingLatest}" in app
    assert "useLayoutEffect" in app
    assert "navigator.clipboard.writeText" in app
    assert "<details" in app


def test_v635_attention_anchor_frontend_contract() -> None:
    hook = Path("frontend/src/useSessions.ts").read_text(encoding="utf-8")
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert "anchor_turn_id" in hook
    assert "正在定位关注事件…" in app
    assert "历史定位视图" in app
    assert "未找到历史位置，已显示最近详情" in app  # noqa: RUF001


def test_frontend_declares_continuous_long_session_layout() -> None:
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
    hook = Path("frontend/src/useSessions.ts").read_text(encoding="utf-8")
    styles = Path("frontend/src/styles.css").read_text(encoding="utf-8")
    built_styles = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/codex_monitor/static/assets").glob("*.css")
    )
    built_scripts = "\n".join(
        path.read_text(encoding="utf-8")
        for path in Path("src/codex_monitor/static/assets").glob("*.js")
    )

    assert "折叠项目列" in app
    assert "折叠会话列" in app
    assert "回到顶部" in app
    assert "right: clamp(2.75rem, 5vi, 4rem)" in styles
    assert "overflow-anchor: none" in styles
    assert "scrollHeight" in built_scripts
    assert "follow-indicator" in built_styles
    assert "follow-new-count" in built_styles
    assert "MAX_DETAIL_TURNS" in hook
    assert "content-visibility: auto" in styles
    assert "@media" not in styles
    assert "@media" not in built_styles


def test_frontend_declares_confirmed_diagnostics_export() -> None:
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert 'role="dialog"' in app
    assert "本机诊断日志" in app
    assert "导出全部日志" in app
    assert "/api/diagnostics/export" in app
    assert "诊断日志导出失败" in app
    assert "已知会话文件" in app
    assert "最近完整校准" in app
    assert "完整校准次数" in app
    assert "合并重复事件" in app


def test_frontend_declares_safe_project_removal() -> None:
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
    hook = Path("frontend/src/useSessions.ts").read_text(encoding="utf-8")

    assert "移除项目" in app
    assert "disabled={removing !== null}" in app
    assert "window.confirm" in app
    assert 'method: "DELETE"' in hook


def test_frontend_exposes_project_lifecycle_states() -> None:
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert "目录失效" in app
    assert "已保存" in app
    assert "仅本次" in app


def test_frontend_declares_diagnostic_preview_and_confirmed_export() -> None:
    source = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert "/api/diagnostics" in source
    assert "diagnosticLogExportPreview" in source
    assert "仅本机、已脱敏、不会自动上传" in source
    assert "window.confirm" in source
    assert "exportDiagnosticLogs" in source


def test_frontend_declares_privacy_safe_attention_notifications() -> None:
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
    hook = Path("frontend/src/useAttentionNotifications.ts").read_text(encoding="utf-8")

    assert "开启桌面提醒" in app
    assert "Notification.requestPermission" in hook
    assert "isDesktopWindowFocused" in hook
    assert "getCurrentWindow().isFocused()" in Path("frontend/src/desktop.ts").read_text(
        encoding="utf-8"
    )
    assert "有 ${newKeys.length} 个会话需要关注" in hook
    assert "60_000" in hook
    assert "hasSynced" in hook
    assert 'const STORAGE_KEY = "attention-notifications-v2"' in hook
    assert 'localStorage.getItem(STORAGE_KEY) !== "false"' in hook
    assert "localStorage.setItem(STORAGE_KEY, String(enabled))" in hook
    assert "writeEnabled(preferred)" in hook
    assert "setPreferred(false)" in hook
    assert 'if (!granted) granted = (await requestPermission()) === "granted"' in hook
    assert 'setPermission("default")' in hook
    assert "if (cancelled || !enabledRef.current) return" in hook
    assert "previous.current = current;\n      if (!inactive) return" in hook
    assert "current_user_message_summary" not in hook
    assert "project_root" not in hook


def test_attention_notifications_guard_every_local_storage_access() -> None:
    hook = Path("frontend/src/useAttentionNotifications.ts").read_text(encoding="utf-8")
    try_depth = 0
    unguarded_lines: list[int] = []

    for line_number, line in enumerate(hook.splitlines(), start=1):
        if re.search(r"\btry\s*\{", line):
            try_depth += 1
        if re.search(r"localStorage\.(?:getItem|setItem)\s*\(", line) and try_depth == 0:
            unguarded_lines.append(line_number)
        if re.search(r"\}\s*catch\b", line):
            try_depth -= 1

    assert not unguarded_lines, (
        "localStorage get/set must be inside try/catch or a safe helper; "
        f"unguarded calls at lines {unguarded_lines}"
    )


def test_attention_notification_dedupe_key_prefers_valid_event_key() -> None:
    hook = Path("frontend/src/useAttentionNotifications.ts").read_text(encoding="utf-8")
    compact = re.sub(r"\s+", " ", hook)

    assert re.search(
        r"isAttentionContext\((?P<context>session\.attention_context|context)\)\s*"
        r"\?\s*(?P=context)\.event_key\s*:\s*session\.session_key",
        compact,
    ), "notification dedupe must use a validated attention event_key with session_key fallback"
    assert ".map((session) => session.session_key)" not in compact, (
        "notification dedupe must not use only session_key"
    )


def test_frontend_declares_compact_workspace_navigation() -> None:
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
    styles = Path("frontend/src/styles.css").read_text(encoding="utf-8")

    assert "compact-layout" in app
    assert "查看项目" in app
    assert "查看会话" in app
    assert "isCompact" in app
    assert ".compact-layout.mobile-detail-open" in styles
    assert ".workbench:not(.compact-layout) .back-button" in styles
    assert "align-self: stretch" in styles
    assert "@media" not in styles


def test_browser_smoke_declares_bounded_soak_cycles() -> None:
    browser = Path("frontend/e2e/browser-smoke.mjs").read_text(encoding="utf-8")

    assert "SOAK_CYCLES = 3" in browser
    assert "E2E soak ${cycle}" in browser
    assert "关闭桌面提醒" in browser


def test_dashboard_declares_session_metadata_and_tool_duration(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        page = client.get("/")

    script_match = re.search(r'src="(/static/assets/[^\"]+\.js)"', page.text)
    assert script_match is not None
    with _app(tmp_path) as client:
        script = client.get(script_match.group(1))

    assert "会话来源" in script.text
    assert "工具耗时" in script.text
    assert "decision-context-toggle" in script.text
    assert "aria-expanded" in script.text


def test_dashboard_declares_runtime_project_import(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        page = client.get("/")

    script_match = re.search(r'src="(/static/assets/[^\"]+\.js)"', page.text)
    assert script_match is not None
    with _app(tmp_path) as client:
        script = client.get(script_match.group(1))

    assert "/api/projects/pick" in script.text


def test_dashboard_declares_multi_project_navigation(tmp_path: Path) -> None:
    with _app(tmp_path) as client:
        page = client.get("/")

    script_match = re.search(r'src="(/static/assets/[^\"]+\.js)"', page.text)
    assert script_match is not None
    with _app(tmp_path) as client:
        script = client.get(script_match.group(1))

    assert "全部项目" in script.text
    assert "项目导航" in script.text
