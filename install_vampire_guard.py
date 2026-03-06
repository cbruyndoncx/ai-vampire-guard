#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click>=8.0", "rich>=13.0"]
# ///
"""AI Vampire Guard — Installer.

Installs the AI Vampire Guard script and hooks into Claude Code.

Usage:
    uv run install_vampire_guard.py              # Install with defaults
    uv run install_vampire_guard.py --uninstall  # Remove everything
    uv run install_vampire_guard.py --dry-run    # Show what would happen
"""

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

import click
from rich.console import Console

console = Console(stderr=True)

SCRIPTS_DIR = Path.home() / ".claude" / "scripts"
SCRIPT_NAME = "ai_vampire_guard.py"
CONFIG_NAME = "config_default.json"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

REPO_BASE = "https://raw.githubusercontent.com/cbruyndoncx/ai-vampire-guard/main"
DOWNLOAD_FILES = [SCRIPT_NAME, CONFIG_NAME]

STOP_HOOK_CMD = f"uv run ~/.claude/scripts/{SCRIPT_NAME} --no-open 2>/dev/null || true"
SUBMIT_HOOK_CMD = f"uv run ~/.claude/scripts/{SCRIPT_NAME} --check 2>/dev/null || true"


def _download_files(dry_run: bool) -> None:
    """Download script and config from GitHub to ~/.claude/scripts/."""
    if not dry_run:
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    for filename in DOWNLOAD_FILES:
        url = f"{REPO_BASE}/{filename}"
        dest = SCRIPTS_DIR / filename
        if dry_run:
            console.print(f"  Would download {filename} → {dest}")
        else:
            try:
                urllib.request.urlretrieve(url, dest)
                console.print(f"  [green]Downloaded[/] {filename} → {dest}")
            except urllib.error.URLError as e:
                console.print(f"  [red]Error downloading {filename}:[/] {e}")
                sys.exit(1)


def _load_settings() -> dict:
    """Load existing settings.json or return empty dict."""
    if SETTINGS_PATH.exists():
        try:
            return json.loads(SETTINGS_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            console.print("[yellow]Warning:[/] Could not parse existing settings.json, starting fresh")
    return {}


def _save_settings(settings: dict) -> None:
    """Write settings.json with nice formatting."""
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(settings, indent=2) + "\n")


def _hook_entry(command: str) -> dict:
    """Create a hook matcher+hooks entry."""
    return {
        "matcher": "",
        "hooks": [{"type": "command", "command": command}],
    }


def _has_vampire_hook(hook_list: list, command: str) -> bool:
    """Check if a vampire guard hook already exists in a hook list."""
    for entry in hook_list:
        for hook in entry.get("hooks", []):
            if SCRIPT_NAME in hook.get("command", ""):
                return True
    return False


def _add_hooks(settings: dict) -> tuple[dict, list[str]]:
    """Add Stop and UserPromptSubmit hooks. Returns (settings, list of added hook names)."""
    hooks = settings.setdefault("hooks", {})
    added = []

    # Stop hook
    stop_hooks = hooks.setdefault("Stop", [])
    if not _has_vampire_hook(stop_hooks, STOP_HOOK_CMD):
        stop_hooks.append(_hook_entry(STOP_HOOK_CMD))
        added.append("Stop")

    # UserPromptSubmit hook
    submit_hooks = hooks.setdefault("UserPromptSubmit", [])
    if not _has_vampire_hook(submit_hooks, SUBMIT_HOOK_CMD):
        submit_hooks.append(_hook_entry(SUBMIT_HOOK_CMD))
        added.append("UserPromptSubmit")

    return settings, added


def _remove_hooks(settings: dict) -> tuple[dict, list[str]]:
    """Remove vampire guard hooks. Returns (settings, list of removed hook names)."""
    hooks = settings.get("hooks", {})
    removed = []

    for hook_name in ["Stop", "UserPromptSubmit"]:
        hook_list = hooks.get(hook_name, [])
        filtered = [
            entry for entry in hook_list
            if not any(SCRIPT_NAME in h.get("command", "") for h in entry.get("hooks", []))
        ]
        if len(filtered) < len(hook_list):
            removed.append(hook_name)
            if filtered:
                hooks[hook_name] = filtered
            else:
                hooks.pop(hook_name, None)

    # Clean up empty hooks dict
    if not hooks:
        settings.pop("hooks", None)

    return settings, removed


@click.command()
@click.option("--uninstall", is_flag=True, help="Remove script, config, and hooks")
@click.option("--dry-run", is_flag=True, help="Show what would happen without making changes")
def main(uninstall: bool, dry_run: bool) -> None:
    """Install or uninstall AI Vampire Guard for Claude Code."""

    if uninstall:
        _do_uninstall(dry_run)
    else:
        _do_install(dry_run)


def _do_install(dry_run: bool) -> None:
    """Install script, config, and hooks."""
    console.print("\n[bold]AI Vampire Guard — Install[/]\n")

    # 1. Download files from GitHub
    _download_files(dry_run)

    # 2. Add hooks
    settings = _load_settings()
    settings, added = _add_hooks(settings)

    if dry_run:
        if added:
            console.print(f"  Would add hooks: {', '.join(added)}")
        else:
            console.print("  Hooks already configured")
    else:
        if added:
            _save_settings(settings)
            console.print(f"  [green]Added hooks:[/] {', '.join(added)}")
        else:
            console.print("  [dim]Hooks already configured[/]")

    # 3. Summary
    console.print()
    if dry_run:
        console.print("[yellow]Dry run — no changes made.[/]")
    else:
        console.print("[green]Installed.[/] Run a quick test:")
        console.print(f"  [dim]uv run {SCRIPTS_DIR / SCRIPT_NAME}[/]\n")


def _do_uninstall(dry_run: bool) -> None:
    """Remove script, config, and hooks."""
    dest_script = SCRIPTS_DIR / SCRIPT_NAME
    dest_config = SCRIPTS_DIR / CONFIG_NAME

    console.print("\n[bold]AI Vampire Guard — Uninstall[/]\n")

    # 1. Remove files
    for path in [dest_script, dest_config]:
        if path.exists():
            if dry_run:
                console.print(f"  Would remove {path}")
            else:
                path.unlink()
                console.print(f"  [red]Removed[/] {path}")
        else:
            console.print(f"  [dim]Not found:[/] {path}")

    # 2. Remove hooks
    if SETTINGS_PATH.exists():
        settings = _load_settings()
        settings, removed = _remove_hooks(settings)
        if dry_run:
            if removed:
                console.print(f"  Would remove hooks: {', '.join(removed)}")
            else:
                console.print("  No hooks to remove")
        else:
            if removed:
                _save_settings(settings)
                console.print(f"  [red]Removed hooks:[/] {', '.join(removed)}")
            else:
                console.print("  [dim]No hooks to remove[/]")

    console.print()
    if dry_run:
        console.print("[yellow]Dry run — no changes made.[/]")
    else:
        console.print("[green]Uninstalled.[/]\n")


if __name__ == "__main__":
    main()
