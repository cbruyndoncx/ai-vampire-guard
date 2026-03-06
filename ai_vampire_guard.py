#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click>=8.0", "rich>=13.0"]
# ///
"""AI Vampire Guard — Claude Code energy gauge.

Parses Claude Code session data and generates an Obsidian note with an SVG
burn-rate gauge + 7-day trend sparkline. Designed to prevent AI-amplified
burnout by making cognitive load visible.

Usage:
    uv run ai_vampire_guard.py                    # Today's gauge (10-day history)
    uv run ai_vampire_guard.py --days 30          # 30-day history
    uv run ai_vampire_guard.py --date 2026-02-24  # Historical date
    uv run ai_vampire_guard.py --verbose           # Show per-session breakdown
    uv run ai_vampire_guard.py --no-open           # Skip opening in Obsidian
    uv run ai_vampire_guard.py --check            # Quick score for hooks (one line, no file)
"""

import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import click
from rich.console import Console

# =============================================================================
# CONFIGURATION
# =============================================================================

SCORE_VERSION = 4  # Bump this when scoring algorithm changes to invalidate cache

# Hardcoded defaults — overridden by config_default.json → custom config → CLI flags
_DEFAULTS = {
    "caps": {
        "token_cap": 350_000,
        "tool_cap": 1_400,
        "depth_cap": 80_000,
        "parallel_cap": 5,
        "unique_tools_cap": 15,
        "unique_skills_cap": 8,
    },
    "thresholds": {"amber": 40, "red": 70},
    "weights": {
        "tokens": 0.20,
        "tools": 0.15,
        "depth": 0.15,
        "parallel": 0.10,
        "complexity": 0.25,
        "engagement": 0.15,
    },
    "thinking_boost": 0.10,
    "behavior": {"history_days": 10, "min_session_messages": 3},
    "output": {"output_dir": None, "vault_root": None, "vault_name": None},
}

# Resolved at startup — populated by _load_config()
CONFIG: dict = {}


def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base. Non-None override values win."""
    merged = dict(base)
    for k, v in override.items():
        if k.startswith("_"):
            continue  # skip _comment fields
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        elif v is not None:
            merged[k] = v
    return merged


def _load_config(config_path: str | None = None) -> dict:
    """Load config with chain: CLI path → config_default.json → hardcoded defaults.

    Config files are JSON with sections: caps, thresholds, weights, thinking_boost,
    behavior, output. Only non-null values override defaults.
    """
    result = dict(_DEFAULTS)

    # 1. Load config_default.json (shipped with skill)
    skill_dir = Path(__file__).resolve().parent.parent
    default_cfg = skill_dir / "config_default.json"
    if default_cfg.exists():
        try:
            with open(default_cfg) as f:
                result = _deep_merge(result, json.load(f))
        except (json.JSONDecodeError, OSError):
            pass

    # 2. Load custom config (CLI flag or auto-detected)
    custom_path = None
    if config_path:
        custom_path = Path(config_path)
    else:
        # Auto-detect: check vault_root from default config, or infer vault root
        # from script location (skill is at vault/00-CORE/.../ai-vampire-guard/)
        vault_root = result.get("output", {}).get("vault_root")
        candidates = []
        if vault_root:
            candidates.append(Path(vault_root) / "10-ME" / "00-PROFILE" / "ai_vampire_guard_config.json")
        # Infer vault root: walk up from script dir looking for 10-ME/
        for parent in skill_dir.parents:
            candidate = parent / "10-ME" / "00-PROFILE" / "ai_vampire_guard_config.json"
            if candidate.exists():
                candidates.append(candidate)
                break
        for candidate in candidates:
            if candidate.exists():
                custom_path = candidate
                break

    if custom_path and custom_path.exists():
        try:
            with open(custom_path) as f:
                result = _deep_merge(result, json.load(f))
        except (json.JSONDecodeError, OSError):
            pass

    # 3. Flatten to the flat CONFIG dict the rest of the script expects
    caps = result.get("caps", {})
    thresholds = result.get("thresholds", {})
    weights = result.get("weights", {})
    behavior = result.get("behavior", {})
    output = result.get("output", {})

    flat = {
        "token_cap": caps.get("token_cap", 350_000),
        "tool_cap": caps.get("tool_cap", 1_400),
        "depth_cap": caps.get("depth_cap", 80_000),
        "parallel_cap": caps.get("parallel_cap", 5),
        "unique_tools_cap": caps.get("unique_tools_cap", 15),
        "unique_skills_cap": caps.get("unique_skills_cap", 8),
        "amber_threshold": thresholds.get("amber", 40),
        "red_threshold": thresholds.get("red", 70),
        "weight_tokens": weights.get("tokens", 0.20),
        "weight_tools": weights.get("tools", 0.15),
        "weight_depth": weights.get("depth", 0.15),
        "weight_parallel": weights.get("parallel", 0.10),
        "weight_complexity": weights.get("complexity", 0.25),
        "weight_engagement": weights.get("engagement", 0.15),
        "thinking_boost": result.get("thinking_boost", 0.10),
        "claude_dir": Path.home() / ".claude",
        "history_days": behavior.get("history_days", 10),
        "min_session_messages": behavior.get("min_session_messages", 3),
    }

    # Output paths — only set if configured
    vr = output.get("vault_root")
    flat["vault_root"] = Path(vr) if vr else None
    flat["output_dir"] = output.get("output_dir")
    flat["vault_name"] = output.get("vault_name")

    return flat


console = Console(stderr=True)

# =============================================================================
# DATA CLASSES
# =============================================================================


@dataclass
class SessionMetrics:
    session_id: str
    first_ts: datetime
    last_ts: datetime
    output_tokens: int = 0
    tool_calls: int = 0
    user_messages: int = 0
    thinking_chars: int = 0
    # Cognitive load signals
    user_chars: int = 0  # total chars in user messages (excl system)
    short_confirms: int = 0  # "yes", "ok", "continue", etc.
    agent_delegations: int = 0  # Agent subagent tool calls
    first_prompt: str = ""  # first user message (topic summary)
    # Complexity signals
    unique_tools: set = field(default_factory=set)  # distinct tool names used
    unique_skills: set = field(default_factory=set)  # distinct skill names invoked

    @property
    def duration_minutes(self) -> int:
        delta = (self.last_ts - self.first_ts).total_seconds() / 60
        return max(1, int(delta))

    @property
    def tool_diversity(self) -> float:
        """Ratio of unique tools to total calls. Low = factory, high = complex."""
        if self.tool_calls == 0:
            return 0.0
        return len(self.unique_tools) / self.tool_calls

    @property
    def cognitive_load(self) -> str:
        """Estimate user cognitive engagement: low/medium/high.

        Factors in:
        - Tool diversity: low ratio = repetitive factory work
        - User message substance: confirms vs detailed instructions
        - Delegation: high agent use with confirms = autopilot
        - Thinking depth: extended reasoning = complex problems
        """
        if self.user_messages == 0:
            return "low"

        confirm_ratio = self.short_confirms / self.user_messages
        avg_msg_len = self.user_chars / max(1, self.user_messages)
        diversity = self.tool_diversity

        # Score 0-10 across multiple dimensions
        score = 0

        # Tool diversity: many unique tools = complex work (0-3)
        if diversity > 0.15:
            score += 3
        elif diversity > 0.08:
            score += 2
        elif diversity > 0.04:
            score += 1

        # User message substance (0-3)
        if avg_msg_len > 150:
            score += 3
        elif avg_msg_len > 80:
            score += 2
        elif avg_msg_len > 40:
            score += 1

        # Confirmation ratio — high confirms = autopilot (0-2)
        if confirm_ratio < 0.3:
            score += 2
        elif confirm_ratio < 0.5:
            score += 1

        # Thinking depth — extended reasoning = hard problems (0-2)
        thinking_per_tool = self.thinking_chars / max(1, self.tool_calls)
        if thinking_per_tool > 500:
            score += 2
        elif thinking_per_tool > 200:
            score += 1

        if score >= 7:
            return "high"
        elif score >= 4:
            return "medium"
        return "low"


@dataclass
class DayScore:
    target_date: date
    score: int = 0
    zone: str = "green"
    total_tokens: int = 0
    total_tools: int = 0
    max_depth: int = 0
    max_concurrent: int = 0
    comp_tokens: float = 0
    comp_tools: float = 0
    comp_depth: float = 0
    comp_parallel: float = 0
    comp_complexity: float = 0
    comp_engagement: float = 0
    day_unique_tools: int = 0
    day_unique_skills: int = 0
    day_tool_diversity: float = 0
    sessions: list[SessionMetrics] = field(default_factory=list)


# =============================================================================
# DATA COLLECTION
# =============================================================================


SHORT_CONFIRMS = {"yes", "no", "ok", "okay", "sure", "continue", "go", "y", "n",
                   "looks good", "lgtm", "proceed", "do it", "go ahead", "agreed"}


def _is_short_confirm(text: str) -> bool:
    """Check if a user message is a short confirmation/dismissal."""
    cleaned = text.strip().lower().rstrip(".!?,")
    return cleaned in SHORT_CONFIRMS or len(cleaned) <= 5


def parse_session_file(path: Path) -> SessionMetrics | None:
    """Parse a single session JSONL file into metrics."""
    session_id = path.stem
    timestamps = []
    output_tokens = 0
    tool_calls = 0
    user_messages = 0
    thinking_chars = 0
    user_chars = 0
    short_confirms = 0
    agent_delegations = 0
    first_prompt = ""
    unique_tools: set[str] = set()
    unique_skills: set[str] = set()

    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts_str = obj.get("timestamp")
                if ts_str and isinstance(ts_str, str):
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        timestamps.append(ts)
                    except ValueError:
                        pass

                msg_type = obj.get("type")
                if msg_type == "user":
                    user_messages += 1
                    # Extract user text (skip system-reminder tags)
                    content = obj.get("message", {}).get("content", [])
                    if isinstance(content, list):
                        for item in content:
                            if isinstance(item, dict) and item.get("type") == "text":
                                text = item["text"]
                                if text.startswith("<system-reminder>"):
                                    continue
                                user_chars += len(text)
                                if _is_short_confirm(text):
                                    short_confirms += 1
                                if not first_prompt:
                                    first_prompt = text[:120]

                msg = obj.get("message", {})
                if not isinstance(msg, dict):
                    continue

                usage = msg.get("usage", {})
                if usage:
                    output_tokens += usage.get("output_tokens", 0)

                content = msg.get("content", [])
                if isinstance(content, list):
                    for item in content:
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "tool_use":
                            tool_calls += 1
                            tool_name = item.get("name", "")
                            if tool_name:
                                unique_tools.add(tool_name)
                            if tool_name == "Agent":
                                agent_delegations += 1
                            elif tool_name == "Skill":
                                skill_name = (item.get("input") or {}).get("skill", "")
                                if skill_name:
                                    unique_skills.add(skill_name)
                        elif item.get("type") == "thinking":
                            thinking_text = item.get("thinking", "")
                            if thinking_text:
                                thinking_chars += len(thinking_text)
    except (OSError, PermissionError):
        return None

    if not timestamps or user_messages < CONFIG["min_session_messages"]:
        return None

    return SessionMetrics(
        session_id=session_id,
        first_ts=min(timestamps),
        last_ts=max(timestamps),
        output_tokens=output_tokens,
        tool_calls=tool_calls,
        user_messages=user_messages,
        thinking_chars=thinking_chars,
        user_chars=user_chars,
        short_confirms=short_confirms,
        agent_delegations=agent_delegations,
        first_prompt=first_prompt,
        unique_tools=unique_tools,
        unique_skills=unique_skills,
    )


def collect_live_sessions(target_date: date, claude_dir: Path) -> list[SessionMetrics]:
    """Scan all project session files for the target date."""
    projects_dir = claude_dir / "projects"
    if not projects_dir.exists():
        return []

    sessions = []
    for project_dir in projects_dir.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl_file in project_dir.glob("*.jsonl"):
            if jsonl_file.name.startswith("agent-"):
                continue
            metrics = parse_session_file(jsonl_file)
            if metrics is None:
                continue
            session_date = metrics.first_ts.date()
            if session_date == target_date:
                sessions.append(metrics)

    sessions.sort(key=lambda s: s.first_ts)
    return sessions


def compute_max_concurrent(sessions: list[SessionMetrics]) -> int:
    """Calculate peak concurrent sessions using sweep-line."""
    if not sessions:
        return 0

    events = []
    for s in sessions:
        events.append((s.first_ts, 1))
        events.append((s.last_ts, -1))
    events.sort(key=lambda e: (e[0], e[1]))

    max_conc = 0
    current = 0
    for _, delta in events:
        current += delta
        max_conc = max(max_conc, current)
    return max_conc


def _load_history_cache(claude_dir: Path) -> dict:
    """Load cached daily scores, keyed by date string."""
    cache_file = claude_dir / "vampire-guard-cache.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text())
            if data.get("version") == SCORE_VERSION:
                return data.get("days", {})
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_history_cache(claude_dir: Path, cache: dict) -> None:
    """Save daily score cache with version stamp."""
    cache_file = claude_dir / "vampire-guard-cache.json"
    try:
        cache_file.write_text(json.dumps({"version": SCORE_VERSION, "days": cache}, indent=2))
    except OSError:
        pass


def _day_score_to_cache(ds: DayScore) -> dict:
    """Serialize a DayScore to a cacheable dict (sessions summarized)."""
    return {
        "score": ds.score,
        "zone": ds.zone,
        "total_tokens": ds.total_tokens,
        "total_tools": ds.total_tools,
        "max_depth": ds.max_depth,
        "max_concurrent": ds.max_concurrent,
        "comp_tokens": ds.comp_tokens,
        "comp_tools": ds.comp_tools,
        "comp_depth": ds.comp_depth,
        "comp_parallel": ds.comp_parallel,
        "comp_complexity": ds.comp_complexity,
        "comp_engagement": ds.comp_engagement,
        "day_unique_tools": ds.day_unique_tools,
        "day_unique_skills": ds.day_unique_skills,
        "day_tool_diversity": ds.day_tool_diversity,
        "session_count": len(ds.sessions),
        "sessions": [
            {
                "session_id": s.session_id,
                "first_ts": s.first_ts.isoformat(),
                "last_ts": s.last_ts.isoformat(),
                "output_tokens": s.output_tokens,
                "tool_calls": s.tool_calls,
                "user_messages": s.user_messages,
                "user_chars": s.user_chars,
                "short_confirms": s.short_confirms,
                "agent_delegations": s.agent_delegations,
                "first_prompt": s.first_prompt,
                "unique_tools": sorted(s.unique_tools),
                "unique_skills": sorted(s.unique_skills),
            }
            for s in ds.sessions
        ],
    }


def _day_score_from_cache(d: date, entry: dict) -> DayScore:
    """Reconstruct a DayScore from a cached dict."""
    sessions = []
    for s in entry.get("sessions", []):
        sessions.append(
            SessionMetrics(
                session_id=s["session_id"],
                first_ts=datetime.fromisoformat(s["first_ts"]),
                last_ts=datetime.fromisoformat(s["last_ts"]),
                output_tokens=s["output_tokens"],
                tool_calls=s["tool_calls"],
                user_messages=s["user_messages"],
                user_chars=s.get("user_chars", 0),
                short_confirms=s.get("short_confirms", 0),
                agent_delegations=s.get("agent_delegations", 0),
                first_prompt=s.get("first_prompt", ""),
                unique_tools=set(s.get("unique_tools", [])),
                unique_skills=set(s.get("unique_skills", [])),
            )
        )
    return DayScore(
        target_date=d,
        score=entry["score"],
        zone=entry["zone"],
        total_tokens=entry["total_tokens"],
        total_tools=entry["total_tools"],
        max_depth=entry["max_depth"],
        max_concurrent=entry["max_concurrent"],
        comp_tokens=entry.get("comp_tokens", 0),
        comp_tools=entry.get("comp_tools", 0),
        comp_depth=entry.get("comp_depth", 0),
        comp_parallel=entry.get("comp_parallel", 0),
        comp_complexity=entry.get("comp_complexity", 0),
        comp_engagement=entry.get("comp_engagement", 0),
        day_unique_tools=entry.get("day_unique_tools", 0),
        day_unique_skills=entry.get("day_unique_skills", 0),
        day_tool_diversity=entry.get("day_tool_diversity", 0),
        sessions=sessions,
    )


def collect_history(
    target_date: date, claude_dir: Path, days: int = 7, verbose: bool = False
) -> list[DayScore]:
    """Pull historical daily scores, using cache for past days."""
    console = Console(stderr=True)
    cache = _load_history_cache(claude_dir)
    cache_dirty = False
    history = []

    today = date.today()
    for i in range(1, days + 1):
        d = target_date - timedelta(days=i)
        d_str = d.isoformat()
        # Only use cache for days that are fully complete (not today)
        if d_str in cache and d < today:
            if verbose:
                console.print(f"[dim]  {d_str}: cached (score {cache[d_str]['score']})[/dim]")
            history.append(_day_score_from_cache(d, cache[d_str]))
        else:
            sessions = collect_live_sessions(d, claude_dir)
            if sessions:
                day_score = score_from_sessions(sessions)
                day_score.target_date = d
            else:
                day_score = DayScore(target_date=d, score=0, zone="green")
            history.append(day_score)
            # Only cache completed days
            if d < today:
                cache[d_str] = _day_score_to_cache(day_score)
            cache_dirty = True
            if verbose:
                console.print(f"[dim]  {d_str}: computed (score {day_score.score})[/dim]")

    if cache_dirty:
        # Prune entries older than 30 days
        cutoff = (target_date - timedelta(days=30)).isoformat()
        cache = {k: v for k, v in cache.items() if k >= cutoff}
        _save_history_cache(claude_dir, cache)

    history.reverse()
    return history


# =============================================================================
# SCORING
# =============================================================================


def _score_to_zone(score: int) -> str:
    if score >= CONFIG["red_threshold"]:
        return "red"
    elif score >= CONFIG["amber_threshold"]:
        return "amber"
    return "green"


def score_from_sessions(sessions: list[SessionMetrics]) -> DayScore:
    """Compute burn-rate score from live session data.

    Six weighted components:
    - Tokens (20%):     total output volume
    - Tools (15%):      raw tool call count
    - Depth (15%):      heaviest single session
    - Parallel (10%):   peak concurrent sessions
    - Complexity (25%): tool diversity + skill variety (how varied the work was)
    - Engagement (15%): user cognitive engagement across sessions

    Plus a thinking boost multiplier for extended reasoning.
    """
    if not sessions:
        return DayScore(target_date=date.today(), score=0, zone="green")

    # --- Volume metrics ---
    total_tokens = sum(s.output_tokens for s in sessions)
    total_tools = sum(s.tool_calls for s in sessions)
    max_depth = max((s.output_tokens for s in sessions), default=0)
    max_concurrent = compute_max_concurrent(sessions)
    total_thinking = sum(s.thinking_chars for s in sessions)

    comp_tokens = min(1.0, total_tokens / CONFIG["token_cap"])
    comp_tools = min(1.0, total_tools / CONFIG["tool_cap"])
    comp_depth = min(1.0, max_depth / CONFIG["depth_cap"])
    comp_parallel = min(1.0, max_concurrent / CONFIG["parallel_cap"])

    # --- Complexity metrics (from actual data) ---
    # Day-level unique tools: union across all sessions
    all_unique_tools = set()
    all_unique_skills = set()
    for s in sessions:
        all_unique_tools |= s.unique_tools
        all_unique_skills |= s.unique_skills

    day_unique_tools = len(all_unique_tools)
    day_unique_skills = len(all_unique_skills)
    day_tool_diversity = day_unique_tools / max(1, total_tools)

    # Complexity = blend of tool diversity ratio, unique tool count, and unique skill count
    #   - diversity ratio: 0.0 (all same tool) → 1.0 (every call different)
    #   - unique tools: normalized against cap (15 = max)
    #   - unique skills: normalized against cap (8 = max)
    diversity_norm = min(1.0, day_tool_diversity / 0.20)  # 20%+ diversity = max
    unique_tools_norm = min(1.0, day_unique_tools / CONFIG["unique_tools_cap"])
    unique_skills_norm = min(1.0, day_unique_skills / CONFIG["unique_skills_cap"])
    comp_complexity = (diversity_norm * 0.4 + unique_tools_norm * 0.35 + unique_skills_norm * 0.25)

    # --- Engagement metrics (from actual data) ---
    # Weighted average of per-session cognitive load scores, weighted by session tokens
    total_engagement = 0.0
    total_weight = 0.0
    for s in sessions:
        load = s.cognitive_load
        load_val = {"low": 0.0, "medium": 0.5, "high": 1.0}[load]
        w = max(1, s.output_tokens)
        total_engagement += load_val * w
        total_weight += w
    comp_engagement = total_engagement / max(1, total_weight)

    # --- Composite score ---
    raw = (
        comp_tokens * CONFIG["weight_tokens"]
        + comp_tools * CONFIG["weight_tools"]
        + comp_depth * CONFIG["weight_depth"]
        + comp_parallel * CONFIG["weight_parallel"]
        + comp_complexity * CONFIG["weight_complexity"]
        + comp_engagement * CONFIG["weight_engagement"]
    )

    # Thinking boost: estimate tokens from chars (÷4), scale as fraction of cap
    if total_thinking > 0:
        thinking_tokens_est = total_thinking / 4
        thinking_ratio = min(1.0, thinking_tokens_est / CONFIG["token_cap"])
        raw = raw * (1 + CONFIG["thinking_boost"] * thinking_ratio)

    score = int(min(100, raw * 100))
    zone = _score_to_zone(score)

    return DayScore(
        target_date=sessions[0].first_ts.date(),
        score=score,
        zone=zone,
        total_tokens=total_tokens,
        total_tools=total_tools,
        max_depth=max_depth,
        max_concurrent=max_concurrent,
        comp_tokens=comp_tokens,
        comp_tools=comp_tools,
        comp_depth=comp_depth,
        comp_parallel=comp_parallel,
        comp_complexity=comp_complexity,
        comp_engagement=comp_engagement,
        day_unique_tools=day_unique_tools,
        day_unique_skills=day_unique_skills,
        day_tool_diversity=day_tool_diversity,
        sessions=sessions,
    )


# =============================================================================
# SVG GENERATION
# =============================================================================

ZONE_COLORS = {"green": "#22c55e", "amber": "#eab308", "red": "#ef4444"}
ZONE_LABELS = {
    "green": "SUSTAINABLE",
    "amber": "WATCH IT",
    "red": "OVERDRIVE",
}


def _score_to_xy(score: float, cx: float = 200, cy: float = 190, r: float = 150):
    """Convert 0-100 score to x,y on a semi-circle arc."""
    angle_deg = 180 - (score / 100) * 180
    angle_rad = math.radians(angle_deg)
    x = cx + r * math.cos(angle_rad)
    y = cy - r * math.sin(angle_rad)
    return x, y


def _arc_path(
    start_score: float, end_score: float, cx: float = 200, cy: float = 190, r: float = 150
) -> str:
    """Generate SVG arc path from start_score to end_score on semi-circle."""
    x1, y1 = _score_to_xy(start_score, cx, cy, r)
    x2, y2 = _score_to_xy(end_score, cx, cy, r)
    sweep = 1 if end_score > start_score else 0
    span = abs(end_score - start_score)
    # Our gauge is a 180° semicircle, so arcs never exceed 180° → always 0
    large_arc = 0
    return f"M {x1:.1f},{y1:.1f} A {r},{r} 0 {large_arc},{sweep} {x2:.1f},{y2:.1f}"


def build_gauge_svg(day_score: DayScore) -> str:
    """Build the semi-circle gauge SVG."""
    score = day_score.score
    zone = day_score.zone
    color = ZONE_COLORS[zone]
    label = ZONE_LABELS[zone]
    amber_t = CONFIG["amber_threshold"]
    red_t = CONFIG["red_threshold"]

    # Zone background arcs
    green_arc = _arc_path(0, amber_t)
    amber_arc = _arc_path(amber_t, red_t)
    red_arc = _arc_path(red_t, 100)

    # Value arc (from 0 to score)
    value_arc = _arc_path(0, max(1, score)) if score > 0 else ""

    # Needle
    nx, ny = _score_to_xy(score)

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 240" style="max-width:400px;width:100%">
  <!-- Zone backgrounds -->
  <path d="{green_arc}" stroke="#22c55e" stroke-opacity="0.2" stroke-width="24" fill="none" stroke-linecap="butt"/>
  <path d="{amber_arc}" stroke="#eab308" stroke-opacity="0.2" stroke-width="24" fill="none" stroke-linecap="butt"/>
  <path d="{red_arc}" stroke="#ef4444" stroke-opacity="0.2" stroke-width="24" fill="none" stroke-linecap="butt"/>'''

    if score > 0:
        svg += f'''
  <!-- Value arc -->
  <path d="{value_arc}" stroke="{color}" stroke-width="24" fill="none" stroke-linecap="butt"/>'''

    # Zone boundary labels — placed inside the arc at 0.75 radius
    ax, ay = _score_to_xy(amber_t, r=150 * 0.75)
    rx, ry = _score_to_xy(red_t, r=150 * 0.75)
    svg += f'''
  <!-- Zone boundary labels -->
  <text x="{ax:.1f}" y="{ay:.1f}" text-anchor="middle" dominant-baseline="middle" font-size="11" font-weight="600" fill="currentColor" opacity="0.4" font-family="system-ui,sans-serif">{amber_t}</text>
  <text x="{rx:.1f}" y="{ry:.1f}" text-anchor="middle" dominant-baseline="middle" font-size="11" font-weight="600" fill="currentColor" opacity="0.4" font-family="system-ui,sans-serif">{red_t}</text>
  <!-- Needle -->
  <line x1="200" y1="190" x2="{nx:.1f}" y2="{ny:.1f}" stroke="currentColor" stroke-width="2" stroke-opacity="0.5"/>
  <circle cx="200" cy="190" r="6" fill="currentColor" opacity="0.5"/>
  <!-- Score -->
  <text x="200" y="158" text-anchor="middle" font-size="52" font-weight="700" fill="{color}" font-family="system-ui,sans-serif">{score}</text>
  <text x="200" y="178" text-anchor="middle" font-size="12" fill="currentColor" opacity="0.4" font-family="system-ui,sans-serif">BURN RATE</text>
  <!-- Zone label -->
  <text x="200" y="218" text-anchor="middle" font-size="14" font-weight="600" fill="{color}" font-family="system-ui,sans-serif">{label}</text>
  <!-- Scale labels -->
  <text x="42" y="205" font-size="11" fill="currentColor" opacity="0.3" font-family="system-ui,sans-serif">0</text>
  <text x="356" y="205" text-anchor="end" font-size="11" fill="currentColor" opacity="0.3" font-family="system-ui,sans-serif">100</text>
</svg>'''
    return svg


def build_sparkline_svg(history: list[DayScore], today: DayScore) -> str:
    """Build a trend sparkline SVG for all history days + today."""
    all_days = history + [today]
    if not all_days:
        return ""

    w, h = 400, 80
    pad_x, pad_y = 40, 15
    plot_w = w - 2 * pad_x
    plot_h = h - 2 * pad_y
    n = len(all_days)
    if n < 2:
        return ""

    max_score = max(max(d.score for d in all_days), 1)
    points = []
    for i, d in enumerate(all_days):
        x = pad_x + (i / (n - 1)) * plot_w
        y = pad_y + plot_h - (d.score / max(max_score, 100)) * plot_h
        points.append((x, y, d))

    polyline = " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)

    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" style="max-width:{w}px;width:100%">\n'
    svg += f'  <polyline points="{polyline}" fill="none" stroke="currentColor" stroke-opacity="0.3" stroke-width="1.5"/>\n'

    # Show labels only for every 7th day, first day, and today
    for i, (x, y, d) in enumerate(points):
        color = ZONE_COLORS[d.zone]
        is_today = i == len(points) - 1
        r = 5 if is_today else 2.5 if n > 14 else 3.5
        svg += f'  <circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}"/>\n'
        # Label: first, every Monday, and today
        show_label = is_today or i == 0 or d.target_date.weekday() == 0
        if show_label:
            day_label = d.target_date.strftime("%d/%m") if n > 10 else d.target_date.strftime("%a")[:2]
            svg += f'  <text x="{x:.1f}" y="{h - 2}" text-anchor="middle" font-size="8" fill="currentColor" opacity="0.4" font-family="system-ui,sans-serif">{day_label}</text>\n'

    svg += "</svg>"
    return svg


# =============================================================================
# MARKDOWN OUTPUT
# =============================================================================


def build_markdown(
    day_score: DayScore,
    history: list[DayScore],
    verbose: bool = False,
) -> str:
    """Build the complete markdown output note."""
    d = day_score.target_date.isoformat()
    gauge_svg = build_gauge_svg(day_score)
    sparkline_svg = build_sparkline_svg(history, day_score)

    now = datetime.now(timezone.utc).strftime("%H:%M")
    zone_color = ZONE_COLORS[day_score.zone]

    # Cognitive load icons
    load_icons = {"low": "🟢", "medium": "🟡", "high": "🔴"}

    # Build breakdown HTML rows
    breakdown_rows = f"""<tr><td>Output tokens</td><td>{day_score.total_tokens:,}</td><td>{day_score.comp_tokens * CONFIG['weight_tokens'] * 100:.0f}/100</td></tr>
<tr><td>Tool calls</td><td>{day_score.total_tools:,}</td><td>{day_score.comp_tools * CONFIG['weight_tools'] * 100:.0f}/100</td></tr>
<tr><td>Depth (peak)</td><td>{day_score.max_depth:,} tok</td><td>{day_score.comp_depth * CONFIG['weight_depth'] * 100:.0f}/100</td></tr>
<tr><td>Parallel</td><td>{day_score.max_concurrent} concurrent</td><td>{day_score.comp_parallel * CONFIG['weight_parallel'] * 100:.0f}/100</td></tr>
<tr><td>Complexity</td><td>{day_score.day_unique_tools} tools, {day_score.day_unique_skills} skills ({day_score.day_tool_diversity:.0%} div)</td><td>{day_score.comp_complexity * CONFIG['weight_complexity'] * 100:.0f}/100</td></tr>
<tr><td>Engagement</td><td>{day_score.comp_engagement:.0%} cognitive</td><td>{day_score.comp_engagement * CONFIG['weight_engagement'] * 100:.0f}/100</td></tr>
<tr><td><b>Composite</b></td><td></td><td><b style="color:{zone_color}">{day_score.score} ({day_score.zone.upper()})</b></td></tr>"""

    # Build sessions HTML rows with cognitive load
    session_rows = ""
    for s in day_score.sessions:
        t = s.first_ts.strftime("%H:%M")
        load = s.cognitive_load
        icon = load_icons.get(load, "")
        topic = s.first_prompt[:50] + ("..." if len(s.first_prompt) > 50 else "")
        # Escape HTML in topic
        topic = topic.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        n_unique = len(s.unique_tools)
        diversity_pct = f"{s.tool_diversity:.0%}" if s.tool_calls else "—"
        session_rows += (
            f"<tr><td>{t}</td><td>{s.duration_minutes}m</td>"
            f"<td>{s.output_tokens:,}</td><td>{s.tool_calls}</td>"
            f"<td>{n_unique} ({diversity_pct})</td>"
            f"<td>{icon} {load}</td>"
            f"<td><small>{topic}</small></td></tr>\n"
        )

    # Build 7-day history rows
    history_rows = ""
    for h in reversed(history):
        hd = h.target_date.strftime("%a %m-%d")
        hcolor = ZONE_COLORS[h.zone]
        # Compute cognitive load summary for history days
        if h.sessions:
            high_count = sum(1 for s in h.sessions if s.cognitive_load == "high")
            med_count = sum(1 for s in h.sessions if s.cognitive_load == "medium")
            low_count = sum(1 for s in h.sessions if s.cognitive_load == "low")
            load_summary = ""
            if high_count:
                load_summary += f"{load_icons['high']}{high_count} "
            if med_count:
                load_summary += f"{load_icons['medium']}{med_count} "
            if low_count:
                load_summary += f"{load_icons['low']}{low_count}"
            load_summary = load_summary.strip()
        else:
            load_summary = "-"
        history_rows += (
            f"<tr><td>{hd}</td>"
            f"<td style=\"color:{hcolor}\"><b>{h.score}</b></td>"
            f"<td>{h.zone.upper()}</td>"
            f"<td>{h.total_tokens:,}</td>"
            f"<td>{h.total_tools:,}</td>"
            f"<td>{len(h.sessions)}</td>"
            f"<td>{load_summary}</td></tr>\n"
        )
    # Prepend today at top
    if day_score.sessions:
        high_count = sum(1 for s in day_score.sessions if s.cognitive_load == "high")
        med_count = sum(1 for s in day_score.sessions if s.cognitive_load == "medium")
        low_count = sum(1 for s in day_score.sessions if s.cognitive_load == "low")
        today_load = ""
        if high_count:
            today_load += f"{load_icons['high']}{high_count} "
        if med_count:
            today_load += f"{load_icons['medium']}{med_count} "
        if low_count:
            today_load += f"{load_icons['low']}{low_count}"
        today_load = today_load.strip()
    else:
        today_load = "-"
    today_row = (
        f"<tr style=\"font-weight:bold\"><td>Today</td>"
        f"<td style=\"color:{zone_color}\"><b>{day_score.score}</b></td>"
        f"<td>{day_score.zone.upper()}</td>"
        f"<td>{day_score.total_tokens:,}</td>"
        f"<td>{day_score.total_tools:,}</td>"
        f"<td>{len(day_score.sessions)}</td>"
        f"<td>{today_load}</td></tr>\n"
    )
    history_rows = today_row + history_rows

    md = f"""---
type: energy-gauge
date: {d}
score: {day_score.score}
zone: {day_score.zone}
tags: [ai-vampire-guard, energy]
---

# AI Vampire Guard — {d}

![[ai_vampire_guard_{d}.png]]

<figure>
{gauge_svg}
</figure>

<figure>
{sparkline_svg}
</figure>

<table>
<tr>
<td valign="top">
<table>
<thead><tr><th>Signal</th><th>Value</th><th>Score</th></tr></thead>
<tbody>
{breakdown_rows}
</tbody>
</table>
</td>
<td valign="top">
<table>
<thead><tr><th>Time</th><th>Dur</th><th>Tokens</th><th>Tools</th><th>Unique (div)</th><th>Your load</th><th>Topic</th></tr></thead>
<tbody>
{session_rows}
</tbody>
</table>
</td>
</tr>
</table>

## History

<table>
<thead><tr><th>Day</th><th>Score</th><th>Zone</th><th>Tokens</th><th>Tools</th><th>Sessions</th><th>Your load</th></tr></thead>
<tbody>
{history_rows}
</tbody>
</table>

<small>{load_icons['high']} high = you actively directing/reviewing &nbsp; {load_icons['medium']} medium = mixed &nbsp; {load_icons['low']} low = agent on autopilot</small>

> Generated by ai-vampire-guard at {now} UTC
"""
    return md


# =============================================================================
# OBSIDIAN INTEGRATION
# =============================================================================


def _detect_platform() -> str:
    """Detect runtime platform for Obsidian URI opening."""
    if os.path.exists("/proc/version"):
        try:
            with open("/proc/version") as f:
                if "microsoft" in f.read().lower():
                    return "wsl"
        except OSError:
            pass
    plat = sys.platform
    if plat == "darwin":
        return "macos"
    if plat == "win32":
        return "windows"
    return "linux"


def open_in_obsidian(filepath: str, vault_name: str) -> None:
    """Open a file in Obsidian using Advanced URI — auto-detects platform."""
    encoded = filepath.replace("/", "%2F").replace(" ", "%20")
    platform = _detect_platform()

    if platform == "wsl":
        uri = f"obsidian://adv-uri?vault={vault_name}^&filepath={encoded}^&openmode=tab"
        cmd = ["/mnt/c/Windows/System32/cmd.exe", "/c", "start", "", uri]
    elif platform == "macos":
        uri = f"obsidian://adv-uri?vault={vault_name}&filepath={encoded}&openmode=tab"
        cmd = ["open", uri]
    elif platform == "windows":
        uri = f"obsidian://adv-uri?vault={vault_name}&filepath={encoded}&openmode=tab"
        cmd = ["start", "", uri]
    else:  # linux
        uri = f"obsidian://adv-uri?vault={vault_name}&filepath={encoded}&openmode=tab"
        cmd = ["xdg-open", uri]

    try:
        subprocess.run(cmd, capture_output=True, timeout=5)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


# =============================================================================
# CLI
# =============================================================================


@click.command()
@click.option("--date", "target_date", default=None, help="Target date YYYY-MM-DD (default: today)")
@click.option("--verbose", is_flag=True, help="Show per-session breakdown in terminal")
@click.option("--no-open", is_flag=True, help="Skip opening in Obsidian")
@click.option("--days", default=None, type=int, help="Number of history days (default: 10)")
@click.option("--check", is_flag=True, help="Quick score check — output one line for hooks, no file written")
@click.option("--claude-dir", default=None, help="Override ~/.claude path")
@click.option("--config", "config_path", default=None, help="Path to custom config JSON")
def main(target_date: str | None, verbose: bool, no_open: bool, days: int | None, check: bool, claude_dir: str | None, config_path: str | None):
    """AI Vampire Guard — monitor your Claude Code burn rate."""
    global CONFIG
    CONFIG = _load_config(config_path)

    claude_path = Path(claude_dir) if claude_dir else CONFIG["claude_dir"]
    vault_root = CONFIG["vault_root"]

    if target_date:
        try:
            td = date.fromisoformat(target_date)
        except ValueError:
            console.print(f"[red]Invalid date: {target_date}[/red]")
            sys.exit(1)
    else:
        td = date.today()

    # Collect data
    if verbose:
        console.print(f"[dim]Scanning sessions for {td}...[/dim]")

    sessions = collect_live_sessions(td, claude_path)

    if verbose:
        console.print(f"[dim]Found {len(sessions)} substantive sessions[/dim]")

    # Score
    day_score = score_from_sessions(sessions)
    day_score.target_date = td

    # --check mode: output one line for hooks and exit
    if check:
        # Throttle: only output once per session (stamp file with PID of parent)
        stamp_file = claude_path / "vampire-guard-check.stamp"
        ppid = str(os.getppid())
        try:
            if stamp_file.exists():
                stamp_data = stamp_file.read_text().strip().split("|")
                if len(stamp_data) == 2 and stamp_data[0] == ppid:
                    # Same parent process — already shown this session, replay cached msg
                    if stamp_data[1]:
                        print(stamp_data[1])
                    return
        except OSError:
            pass

        zone = day_score.zone
        score = day_score.score
        n = len(sessions)
        msg = ""
        if zone == "red":
            msg = (f"⚠️ VAMPIRE GUARD: {score}/100 (RED — OVERDRIVE). "
                   f"{n} sessions, {day_score.total_tokens:,} tokens today. "
                   f"You're burning hot. Keep this session short and focused, "
                   f"avoid rabbit holes, and suggest wrapping up proactively.")
        elif zone == "amber":
            msg = (f"🟡 VAMPIRE GUARD: {score}/100 (AMBER — WATCH IT). "
                   f"{n} sessions, {day_score.total_tokens:,} tokens today. "
                   f"Be concise and efficient. Flag if a task is growing in scope.")
        # Green: say nothing — no need to distract

        # Write stamp so subsequent calls in this session are instant
        try:
            stamp_file.write_text(f"{ppid}|{msg}")
        except OSError:
            pass

        if msg:
            print(msg)
        return

    # History
    history_days = days if days is not None else CONFIG["history_days"]
    history = collect_history(td, claude_path, history_days, verbose=verbose)

    # Output
    if vault_root and CONFIG["output_dir"]:
        out_dir = Path(vault_root) / CONFIG["output_dir"]
    else:
        out_dir = Path.cwd()
    output_file = out_dir / f"ai_vampire_guard_{td.isoformat()}.md"

    md = build_markdown(day_score, history, verbose)
    out_dir.mkdir(parents=True, exist_ok=True)
    output_file.write_text(md)

    # Terminal output — always show warning for amber/red
    zone_style = {"green": "green", "amber": "yellow", "red": "red bold"}
    style = zone_style.get(day_score.zone, "white")
    console.print(f"[{style}]Burn Rate: {day_score.score}/100 ({day_score.zone.upper()})[/{style}]")

    if day_score.zone == "red":
        console.print("[red bold]⚠️  You're in overdrive. Consider taking a break.[/red bold]")
    elif day_score.zone == "amber":
        console.print("[yellow]🟡 Watch your energy. Stay focused, avoid scope creep.[/yellow]")

    if verbose:
        console.print(f"  Tokens: {day_score.total_tokens:,} ({day_score.comp_tokens:.0%} of cap)")
        console.print(f"  Tools:  {day_score.total_tools:,} ({day_score.comp_tools:.0%} of cap)")
        console.print(f"  Depth:  {day_score.max_depth:,} ({day_score.comp_depth:.0%} of cap)")
        console.print(f"  Parallel: {day_score.max_concurrent} ({day_score.comp_parallel:.0%} of cap)")
        console.print(f"  Complexity: {day_score.day_unique_tools} tools, {day_score.day_unique_skills} skills, {day_score.day_tool_diversity:.1%} diversity → {day_score.comp_complexity:.2f}")
        console.print(f"  Engagement: {day_score.comp_engagement:.0%} cognitive load")
        console.print(f"  Sessions: {len(sessions)}")
        if sessions:
            console.print()
            for s in sessions:
                console.print(
                    f"  {s.first_ts.strftime('%H:%M')} | {s.duration_minutes:>3}m | "
                    f"{s.output_tokens:>7,} tok | {s.tool_calls:>4} tools | {s.user_messages} msgs"
                )

    console.print(f"[dim]Written to {output_file}[/dim]")

    if not no_open and vault_root and CONFIG["vault_name"]:
        rel_path = str(output_file.relative_to(vault_root))
        open_in_obsidian(rel_path, CONFIG["vault_name"])


if __name__ == "__main__":
    main()
