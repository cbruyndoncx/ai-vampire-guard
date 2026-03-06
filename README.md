# AI Vampire Guard

**Monitor your Claude Code burn rate. Prevent AI-amplified burnout.**

![AI Vampire Guard](vampire-guard-screenshot.png)

---

Steve Yegge coined the term "AI Vampire" — when AI tools amplify your productivity so much that you burn through your energy without realizing it. You finish the day having shipped more than ever, but you're completely drained.

AI Vampire Guard makes your cognitive load visible. It reads your Claude Code session logs and produces a daily burn-rate score from 0 to 100, visualized as a gauge with three zones:

| Zone | Score | What It Means |
|------|-------|---------------|
| GREEN | 0–39 | Sustainable pace |
| AMBER | 40–69 | Watch your energy |
| RED | 70–100 | Overdrive — take a break |

The score isn't just token count. It weighs six factors: output volume, tool call intensity, session depth, parallel sessions, task complexity, and your personal cognitive engagement.

## Requirements

- **Python 3.11+** (`python3 --version`)
- **uv** — the fast Python package manager (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- **Claude Code** — reads session logs from `~/.claude/projects/`

No other dependencies. The script uses PEP 723 inline metadata — `uv run` installs `click` and `rich` automatically on first run.

## Install (60 seconds)

### Option A: Automatic Install (Recommended)

Run the installer — it downloads the script and config from GitHub, places them in `~/.claude/scripts/`, and configures Claude Code hooks:

```bash
curl -sL https://raw.githubusercontent.com/cbruyndoncx/ai-vampire-guard/main/install_vampire_guard.py \
  | uv run -
```

Preview first with `--dry-run`, or remove later with `--uninstall`:

```bash
uv run ~/.claude/scripts/install_vampire_guard.py --dry-run
uv run ~/.claude/scripts/install_vampire_guard.py --uninstall
```

### Option B: Manual Install

Download the script and config yourself, then configure hooks manually (see [Automatic Monitoring](#automatic-monitoring-hooks) below):

```bash
mkdir -p ~/.claude/scripts

curl -o ~/.claude/scripts/ai_vampire_guard.py \
  https://raw.githubusercontent.com/cbruyndoncx/ai-vampire-guard/main/ai_vampire_guard.py

curl -o ~/.claude/scripts/config_default.json \
  https://raw.githubusercontent.com/cbruyndoncx/ai-vampire-guard/main/config_default.json
```

You'll see:

```
Burn Rate: 47/100 (AMBER)
🟡 Watch your energy. Stay focused, avoid scope creep.
Written to ./ai_vampire_guard_2026-03-06.md
```

## Automatic Monitoring (Hooks)

The real power is in Claude Code hooks — the guard runs automatically without you thinking about it.

> **If you used the automatic installer (Option A), these hooks are already configured.** Skip to the Obsidian Integration section below.

### Post-Session Gauge (Stop Hook)

Generates a gauge file after every Claude Code session ends. Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "uv run ~/.claude/scripts/ai_vampire_guard.py --no-open 2>/dev/null || true"
          }
        ]
      }
    ]
  }
}
```

### Real-Time Warning (UserPromptSubmit Hook)

Injects a one-line warning into your conversation when you're in amber or red zone. Claude sees it and adjusts — staying concise and flagging scope creep.

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "uv run ~/.claude/scripts/ai_vampire_guard.py --check 2>/dev/null || true"
          }
        ]
      }
    ]
  }
}
```

**Combine both hooks** in the same `settings.json` — they work independently.

- **Green zone?** Total silence. No distraction.
- **Amber zone?** Gentle nudge to stay focused.
- **Red zone?** Strong warning to wrap up.

## Obsidian Integration (Optional)

If you use Obsidian, create a config file to direct output to your vault:

```json
{
  "output": {
    "output_dir": "path/to/inbox",
    "vault_root": "/full/path/to/your/vault",
    "vault_name": "your-vault-name"
  }
}
```

Save as `~/.claude/vampire-guard-config.json`, then:

```bash
uv run ~/.claude/scripts/ai_vampire_guard.py --config ~/.claude/vampire-guard-config.json
```

The gauge file lands in your Obsidian vault and opens automatically. Works on macOS, Linux, WSL, and Windows.

## Configuration

The defaults work for most people. To customize:

```bash
cp ~/.claude/scripts/config_default.json ~/.claude/vampire-guard-config.json
```

Edit only the sections you want to change:

| Setting | Default | What It Controls |
|---------|---------|------------------|
| `caps.token_cap` | 350,000 | Tokens for 100% on the volume component |
| `caps.tool_cap` | 1,400 | Tool calls for 100% |
| `thresholds.amber` | 40 | Score where amber zone starts |
| `thresholds.red` | 70 | Score where red zone starts |
| `behavior.history_days` | 10 | Days shown in trend chart |

Heavy users might raise the caps. If you're getting amber every day and feel fine, bump `thresholds.amber` to 50.

## CLI Reference

```bash
uv run ai_vampire_guard.py                         # Today's gauge (10-day history)
uv run ai_vampire_guard.py --days 30               # 30-day history
uv run ai_vampire_guard.py --date 2026-02-24       # Historical date
uv run ai_vampire_guard.py --verbose                # Detailed terminal output
uv run ai_vampire_guard.py --check                  # Quick check (one line, for hooks)
uv run ai_vampire_guard.py --no-open                # Skip Obsidian open
uv run ai_vampire_guard.py --config /path/to.json   # Custom config
```

## How the Score Works

Six weighted components:

| Component | Weight | What It Measures |
|-----------|--------|------------------|
| Output tokens | 20% | Total AI work volume |
| Tool calls | 15% | Implementation intensity |
| Session depth | 15% | Heaviest single session |
| Parallel pressure | 10% | Max concurrent sessions |
| **Complexity** | **25%** | Tool diversity + skill variety |
| **Engagement** | **15%** | Your personal cognitive load |

**Complexity** distinguishes factory work (same tools repeated) from genuinely complex work (many different tools, multiple skills).

**Engagement** measures whether you're actively directing every move (high load) or letting the agent run on autopilot (low load). Token-weighted across sessions.

Bonus: +10% multiplier when extended thinking is detected.

## Output

The generated markdown file includes:

- **SVG semi-circle gauge** with zone coloring and boundary labels
- **Trend sparkline** showing your pattern over time
- **Component breakdown table** (all 6 components)
- **Per-session details** with unique tools, diversity %, and cognitive load
- **History table** (most recent first)

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "No sessions found" | Claude Code stores logs in `~/.claude/projects/`. Run a few sessions first. |
| Score seems too high/low | Adjust `caps` in your config for your usage pattern. |
| Obsidian doesn't open | Check `vault_name` matches exactly and Advanced URI plugin is installed. |
| `uv` not found | Install: `curl -LsSf https://astral.sh/uv/install.sh | sh` |

## License

MIT. Use it, modify it, share it.

Built by [Carine Bruyndoncx](https://brn.cx) — AI Integration Specialist.

*Working Hard at Working Smart.*
