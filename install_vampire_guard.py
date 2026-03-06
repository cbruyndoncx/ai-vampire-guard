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
import shutil
import sys
from pathlib import Path

import click
from rich.console import Console

console = Console(stderr=True)

SCRIPTS_DIR = Path.home() / ".claude" / "scripts"
SCRIPT_NAME = "ai_vampire_guard.py"
CONFIG_NAME = "config_default.json"
SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

STOP_HOOK_CMD = f"uv run ~/.claude/scripts/{SCRIPT_NAME} --no-open 2>/dev/null || true"
SUBMIT_HOOK_CMD = f"uv run ~/.claude/scripts/{SCRIPT_NAME} --check 2>/dev/null || true"


def _find_source_files() -> tuple[Path, Path]:
    """Locate the script and config relative to this installer.

    Checks same directory first (flat layout: repo root or ~/.claude/scripts/),
    then parent directory (skill layout: scripts/ with config one level up).
    """
    installer_dir = Path(__file__).resolve().parent
    script = installer_dir / SCRIPT_NAME
    # Try same dir first (flat repo), then parent (skill layout)
    config = installer_dir / CONFIG_NAME
    if not config.exists():
        config = installer_dir.parent / CONFIG_NAME
    if not script.exists():
        console.print(f"[red]Error:[/] {SCRIPT_NAME} not found at {script}")
        sys.exit(1)
    if not config.exists():
        console.print(f"[red]Error:[/] {CONFIG_NAME} not found next to installer or one level up")
        sys.exit(1)
    return script, config


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
    source_script, source_config = _find_source_files()
    dest_script = SCRIPTS_DIR / SCRIPT_NAME
    dest_config = SCRIPTS_DIR / CONFIG_NAME

    console.print("\n[bold]AI Vampire Guard — Install[/]\n")

    # 1. Copy files
    if dry_run:
        console.print(f"  Would copy {source_script.name} → {dest_script}")
        console.print(f"  Would copy {source_config.name} → {dest_config}")
    else:
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_script, dest_script)
        shutil.copy2(source_config, dest_config)
        console.print(f"  [green]Copied[/] {SCRIPT_NAME} → {dest_script}")
        console.print(f"  [green]Copied[/] {CONFIG_NAME} → {dest_config}")

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
        console.print(f"  [dim]uv run {dest_script}[/]\n")


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
