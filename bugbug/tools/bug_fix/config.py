from __future__ import annotations

from pathlib import Path

import yaml

# Tools that can modify the source repo — blocked under dry-run.
SOURCE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Bugzilla MCP tool names as exposed to the agent (mcp__<server>__<tool>).
BUGZILLA_READ_TOOLS = [
    "mcp__bugzilla__search_bugs",
    "mcp__bugzilla__get_bugs",
    "mcp__bugzilla__get_bug_comments",
    "mcp__bugzilla__get_bug_attachments",
    "mcp__bugzilla__download_attachment",
]
BUGZILLA_WRITE_TOOLS = [
    "mcp__bugzilla__update_bug",
    "mcp__bugzilla__add_comment",
    "mcp__bugzilla__add_attachment",
    "mcp__bugzilla__create_bug",
]

# Firefox build/test tools.
FIREFOX_TOOLS = [
    "mcp__firefox__evaluate_testcase",
    "mcp__firefox__build_firefox",
    "mcp__firefox__evaluate_js_shell",
    "mcp__firefox__bootstrap_firefox",
]

# Firefox DevTools MCP tools (@mozilla/firefox-devtools-mcp-moz), exposed under
# the "firefox-devtools" server name. Web-compat reproduction subset: page
# navigation, accessibility snapshots + UID-based interaction, console/network
# inspection, screenshots, and scripted DOM probing (evaluate_script needs
# --enable-script). Privileged-context and extension tools are intentionally
# omitted. Only registered when webcompat_tools is enabled.
DEVTOOLS_TOOLS = [
    "mcp__firefox-devtools__list_pages",
    "mcp__firefox-devtools__new_page",
    "mcp__firefox-devtools__navigate_page",
    "mcp__firefox-devtools__select_page",
    "mcp__firefox-devtools__close_page",
    "mcp__firefox-devtools__take_snapshot",
    "mcp__firefox-devtools__resolve_uid_to_selector",
    "mcp__firefox-devtools__clear_snapshot",
    "mcp__firefox-devtools__click_by_uid",
    "mcp__firefox-devtools__hover_by_uid",
    "mcp__firefox-devtools__fill_by_uid",
    "mcp__firefox-devtools__fill_form_by_uid",
    "mcp__firefox-devtools__drag_by_uid_to_uid",
    "mcp__firefox-devtools__upload_file_by_uid",
    "mcp__firefox-devtools__list_console_messages",
    "mcp__firefox-devtools__clear_console_messages",
    "mcp__firefox-devtools__list_network_requests",
    "mcp__firefox-devtools__get_network_request",
    "mcp__firefox-devtools__screenshot_page",
    "mcp__firefox-devtools__screenshot_by_uid",
    "mcp__firefox-devtools__evaluate_script",
    "mcp__firefox-devtools__accept_dialog",
    "mcp__firefox-devtools__dismiss_dialog",
    "mcp__firefox-devtools__navigate_history",
    "mcp__firefox-devtools__set_viewport_size",
    "mcp__firefox-devtools__get_firefox_info",
    "mcp__firefox-devtools__get_firefox_output",
]

# Deployment-stable settings that may be supplied via config YAML.
_CONFIG_KEYS = {
    "base_url",
    "source_repo",
    "rules_dir",
    "model",
    "max_turns",
    "effort",
    "webcompat_tools",
    "firefox_path",
}


def load_config(path: Path) -> dict:
    with path.open() as f:
        data = yaml.safe_load(f) or {}
    unknown = set(data) - _CONFIG_KEYS
    if unknown:
        raise ValueError(
            f"unknown config key(s) in {path}: {sorted(unknown)}\n"
            f"allowed: {sorted(_CONFIG_KEYS)}"
        )
    return data
