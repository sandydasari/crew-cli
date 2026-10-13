#!/usr/bin/env python3
"""crew — run your own Claude Code agents on a schedule, and decide what they can't.

Each agent lives in ~/.crew/agents/<name>/:
  agent.json   your settings: budget, tools, folders, notifications, standing-rule freedoms
  AGENT.md     its job, rules and how you like things; written from your brief and kept current by the agent
  INDEX.md     the agent's own map of its folder; notes/, work/ and .claude/skills/ are its own too

Agents work on their own (Overtime-style): each wake they decide what deserves attention, do the work (code
in their own worktrees), and ask you only what's yours to decide: anything public or done as you, destructive
outside their folder, or that spends money. They pick when to wake next, or wake on a watch.
Older item-based agents (prompt.md + scan.*) still run as before.
"""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import plistlib
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

HOME = Path(os.environ.get("CREW_HOME", Path.home() / ".crew"))
AGENTS_DIR = HOME / "agents"
DB_PATH = HOME / "state.db"
LOGS = HOME / "logs"
LAUNCH_AGENTS = Path.home() / "Library/LaunchAgents"
CLAUDE_TIMEOUT = 25 * 60
LEGACY_PRBOT_DB = Path.home() / ".prbot/state.db"
WORK = HOME / "work"  # shared cwd for new sessions, so Claude Code asks to trust just one folder
CONFIG_PATH = HOME / "config.json"
CONFIG_DEFAULTS = {
    "daily_budget_usd": 100,  # per agent, unless its agent.json sets budget_usd
    "wake_timeout_minutes": 90,
    # Folders no agent may write to during a wake, shell commands included (the macOS sandbox enforces it). A repo's
    # .git stays writable, so agents can still make their own worktrees from it.
    "protect": [],  # e.g. ["~/code/main-checkout"]
    "blocked_tools": [],  # more tool rules a wake must ask about first, e.g. ["Bash(deploy-tool create:*)"]
    "terminal_app": None,  # bundle id brought forward when you click a notification; None = the one crew last ran in
}
ICON = Path(__file__).resolve().parent.parent / "assets" / "icon.png"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import galley  # noqa: E402  (palette, words, flush-left markdown headings)
from galley import DONE, GRAPHITE, PAPER, PENCIL, RULE  # noqa: E402

console = Console(theme=galley.THEME)

AGENT_DEFAULTS = {
    "description": "",
    "interval": "1h",
    "scan": None,  # shell command run in the agent dir; prints a JSON array of items. None = one item per run.
    "max_items_per_run": 5,
    "auto_pick": True,  # False = only work items you've picked (crew pick <agent>); new items just land in the list
    "skills": [],
    "model": None,
    "tools": {
        "run": {"allow": ["@readonly"], "deny": ["@no-write"]},
        "act": {"allow": ["@readonly", "@write"], "deny": ["@never"]},
    },
    "verdicts": ["ok", "attention", "nothing_new", "needs_human"],
    "labels": {"item": "Item", "summary": "Summary", "approach": "Approach", "draft": "Draft (not sent)"},
    "columns": [],  # extra item fields shown in the dashboard
    "act_instructions": "",
    # shell command (item fields as {placeholders}) printing an item's state once it drops out of the scan, e.g.
    # MERGED / CLOSED / OPEN. Anything but OPEN closes the item. None = an item that drops out is resolved and closed.
    "closed_check": None,
    "extra_instructions": "",
    "folders": [],  # extra folders the agent may read (and Claude Code trusts), passed as --add-dir
    "settings": {},  # free-form; passed to the scan as $CREW_SETTINGS
    # inbox events: decision (new decisions), alert (agent-flagged heads-up), error (failed run), done (action finished)
    "notify": {"desktop": True, "sound": False, "events": ["decision", "alert", "paused", "failing", "done"]},
}

TOOL_PRESETS = {
    "@readonly": [
        "Read", "Grep", "Glob", "Skill", "Agent", "WebFetch", "WebSearch",
        "Bash(ls:*)", "Bash(cat:*)", "Bash(grep:*)", "Bash(rg:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(wc:*)",
        "Bash(jq:*)", "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)", "Bash(git status:*)",
        "Bash(git branch:*)", "Bash(git blame:*)", "Bash(git fetch:*)",
        "Bash(gh pr view:*)", "Bash(gh pr diff:*)", "Bash(gh pr checks:*)", "Bash(gh pr list:*)",
        "Bash(gh issue view:*)", "Bash(gh issue list:*)", "Bash(gh search:*)", "Bash(gh api:*)",
        "Bash(gh repo view:*)", "Bash(gh run view:*)", "Bash(gh run list:*)",
    ],
    "@no-write": [
        "Edit", "Write", "NotebookEdit",
        "Bash(gh pr review:*)", "Bash(gh pr comment:*)", "Bash(gh pr merge:*)", "Bash(gh pr close:*)",
        "Bash(gh pr edit:*)", "Bash(gh issue comment:*)", "Bash(gh issue close:*)", "Bash(gh issue edit:*)",
        "Bash(gh issue create:*)", "Bash(gh api * -X *)", "Bash(gh api * --method *)", "Bash(gh api * -f *)",
        "Bash(gh api * -F *)", "Bash(gh api * --field *)", "Bash(gh api * --raw-field *)",
        "Bash(git push:*)", "Bash(git commit:*)", "Bash(git checkout:*)", "Bash(git reset:*)",
    ],
    "@write": [
        "Edit", "Write", "Bash(gh pr review:*)", "Bash(gh pr comment:*)", "Bash(gh issue comment:*)",
        "Bash(gh issue create:*)", "Bash(gh api:*)", "Bash(git:*)", "Bash(npm:*)", "Bash(pnpm:*)",
        "Bash(yarn:*)", "Bash(pytest:*)", "Bash(python3:*)", "Bash(uv:*)", "Bash(make:*)",
    ],
    # public, done as the user, or costs money: a wake asks first (unless the user granted it as a standing rule)
    "@public": [
        "Bash(git push:*)", "Bash(git * push*)", "Bash(gh pr create:*)", "Bash(gh pr review:*)", "Bash(gh pr comment:*)",
        "Bash(gh pr edit:*)", "Bash(gh pr ready:*)", "Bash(gh issue create:*)", "Bash(gh issue comment:*)",
        "Bash(gh issue close:*)", "Bash(gh issue edit:*)", "Bash(gh release:*)", "Bash(gh repo create:*)",
        "Bash(gh workflow run:*)", "Bash(gh run rerun:*)", "Bash(gh run cancel:*)",
        "Bash(gh api * -X *)", "Bash(gh api * --method *)", "Bash(gh api * -f *)", "Bash(gh api * -F *)",
        "Bash(gh api * --field *)", "Bash(gh api * --raw-field *)", "Bash(gh api * --input *)",
        "Bash(curl * -X *)", "Bash(curl * -d *)", "Bash(curl * --data*)", "Bash(curl * -F *)", "Bash(curl * --form *)",
        *(f"Bash(composio execute *{w}*)" for w in ("SEND", "POST", "UPDATE", "DELETE", "CREATE", "ADD", "REMOVE",
                                                    "INVITE", "UPLOAD", "SCHEDULE", "REACT", "ARCHIVE", "JOIN",
                                                    "LEAVE", "SET", "RENAME", "PIN", "EDIT", "REPLY")),
        "Bash(npm publish:*)", "Bash(pnpm publish:*)", "Bash(osascript:*)",
    ],
    "@never": ["Bash(gh pr merge:*)", "Bash(gh pr close:*)", "Bash(git push --force:*)", "Bash(git push -f:*)",
               "Bash(rm -rf:*)", "Bash(gh repo delete:*)", "Bash(gh secret:*)", "Bash(sudo:*)", "Bash(diskutil:*)",
               "Bash(launchctl:*)", "Bash(crew decide:*)", "Bash(crew answered:*)", "Bash(crew answer:*)", "Bash(crew remove:*)",
               "Bash(crew learn:*)", "Bash(crew run:*)", "Bash(crew tick:*)", "Bash(crew ui:*)", "Bash(crew chat:*)"],
}
GRANTABLE = [t for t in TOOL_PRESETS["@public"] if t not in TOOL_PRESETS["@never"]]

ACT_SCHEMA = {
    "type": "object",
    "properties": {
        "done": {"type": "boolean", "description": "True when you carried out their answer, or it meant doing nothing."},
        "keep_open": {"type": "boolean", "description": "True when their reply was a question or unclear, so the question stays open."},
        "reply": {"type": "string", "description": "Markdown, short and plain: what you did (with links to anything posted or pushed), or your answer to their question. No hashes or ids."},
        "follow_up": {"type": "string", "description": "When their answer needs long work (a deeper review, a big fix, anything over a couple of minutes): don't do it here. Say in `reply` that you're on it, and put what to do here; crew starts a background wake for it. Empty otherwise."},
    },
    "required": ["done", "keep_open", "reply", "follow_up"],
}

VERDICT_COLORS = {
    "approve": "green", "ok": "green", "request_changes": "red", "attention": "yellow", "comment": "yellow",
    "nothing_new": "dim", "needs_human": "magenta",
}


# ---------------------------------------------------------------- helpers

def now():
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def ago(ts):
    if not ts:
        return "—"
    then = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    then = then if then.tzinfo else then.astimezone()  # older rows were stored without a timezone
    secs = (dt.datetime.now().astimezone() - then).total_seconds()
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if abs(secs) >= size:
            return f"{int(secs // size)}{unit} ago"
    return "just now"


def parse_interval(s):
    m = re.fullmatch(r"(\d+)\s*([smhd])", str(s).strip())
    if not m:
        raise SystemExit(f"Bad interval {s!r} — use e.g. 15m, 1h, 1d")
    return int(m[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m[2]]


def expand_tools(names):
    out = []
    for n in names:
        for t in TOOL_PRESETS.get(n, [n]):
            if t not in out:
                out.append(t)
    return out


class _Blank(dict):
    def __missing__(self, key):
        return ""


def fill(text, fields):
    """{placeholders} filled in; text with stray braces (JSON, code) is left as it is."""
    try:
        return text.format_map(fields)
    except (ValueError, IndexError, AttributeError):
        return text


def config():
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        raw = {}
    return {**CONFIG_DEFAULTS, **raw}


_me = None


def whoami():
    global _me
    if _me is None:
        r = subprocess.run(["gh", "api", "user", "--jq", ".login"], capture_output=True, text=True)
        _me = r.stdout.strip() or os.environ.get("USER", "me")
    return _me


# ---------------------------------------------------------------- agents

def agent_names():
    if not AGENTS_DIR.exists():
        return []
    def rank(p):  # agent.json "order" sets the sidebar order; unordered agents follow, by name
        try:
            return (json.loads((p / "agent.json").read_text()).get("order", 999), p.name)
        except (OSError, ValueError):
            return (999, p.name)
    return [p.name for p in sorted((p for p in AGENTS_DIR.iterdir() if (p / "agent.json").exists()), key=rank)]


def load_agent(name):
    d = AGENTS_DIR / name
    if not (d / "agent.json").exists():
        raise SystemExit(f"No agent named {name!r}. Agents: {', '.join(agent_names()) or 'none'} — create one with `crew new`.")
    raw = json.loads((d / "agent.json").read_text())
    a = json.loads(json.dumps(AGENT_DEFAULTS))
    for k, v in raw.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            a[k].update(v)
        else:
            a[k] = v
    if isinstance(a["notify"], bool):
        a["notify"] = dict(AGENT_DEFAULTS["notify"], desktop=a["notify"])
    a["name"], a["dir"] = name, d
    job = d / "AGENT.md" if (d / "AGENT.md").exists() else d / "prompt.md"
    a["job_file"] = job.name
    a["prompt"] = job.read_text() if job.exists() else ""
    return a


def save_agent_field(name, key, value):
    p = AGENTS_DIR / name / "agent.json"
    raw = json.loads(p.read_text())
    raw[key] = value
    p.write_text(json.dumps(raw, indent=2) + "\n")


def output_schema(agent):
    return {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": f"Markdown. {agent['labels']['summary']}: explain the item to someone who hasn't seen it."},
            "approach": {"type": "string", "description": f"Markdown. {agent['labels']['approach']}: what you did, what you checked, which skills you ran, how you reached your verdict."},
            "verdict": {"type": "string", "enum": agent["verdicts"]},
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "severity": {"type": "string", "enum": ["blocking", "suggestion", "nit", "info"]},
                        "file": {"type": "string"},
                        "line": {"type": "integer"},
                        "title": {"type": "string"},
                        "detail": {"type": "string"},
                    },
                    "required": ["severity", "title", "detail"],
                },
            },
            "draft": {"type": "string", "description": "Markdown. Anything you would post or send, not sent yet. Empty if nothing."},
            "alert": {"type": "string", "description": "One line worth interrupting the human for right now even without a decision (e.g. something broke or got urgent). Empty string otherwise — most runs should leave it empty."},
            "decisions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "context": {"type": "string", "description": "Markdown. Why this needs the human, with the evidence."},
                        "options": {"type": "array", "items": {"type": "string"}, "minItems": 2},
                        "recommendation": {"type": "string", "description": "Which option you'd pick and why."},
                    },
                    "required": ["question", "context", "options", "recommendation"],
                },
            },
        },
        "required": ["summary", "approach", "verdict", "findings", "draft", "decisions"],
    }


def scan(agent):
    if not agent["scan"]:
        return [{"key": "default", "title": agent["description"] or agent["name"], "updated_at": now()}]
    env = dict(os.environ, CREW_SETTINGS=json.dumps(agent["settings"]), CREW_AGENT=agent["name"])
    proc = subprocess.run(agent["scan"], shell=True, cwd=agent["dir"], capture_output=True, text=True, env=env, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"scan failed ({proc.returncode}): {proc.stderr.strip()[-400:]}")
    items = json.loads(proc.stdout or "[]")
    for it in items:
        if "key" not in it or "title" not in it:
            raise RuntimeError(f"scan item missing key/title: {it}")
    return items


def item_version(item):
    return item.get("updated_at") or hashlib.sha1(json.dumps(item, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- storage

def db():
    HOME.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY, agent TEXT, started_at TEXT, finished_at TEXT, status TEXT, trigger TEXT,
            current TEXT, items_seen INTEGER DEFAULT 0, items_done INTEGER DEFAULT 0,
            new_decisions INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0, error TEXT);
        CREATE TABLE IF NOT EXISTS items (
            agent TEXT, key TEXT, title TEXT, url TEXT, data TEXT, version TEXT, done_version TEXT,
            last_run_at TEXT, session_id TEXT, cwd TEXT, summary TEXT, approach TEXT, verdict TEXT,
            findings TEXT, draft TEXT, run_count INTEGER DEFAULT 0, in_queue INTEGER DEFAULT 1,
            PRIMARY KEY (agent, key));
        CREATE TABLE IF NOT EXISTS decisions (
            id INTEGER PRIMARY KEY, agent TEXT, item_key TEXT, run_id INTEGER, question TEXT, context TEXT,
            options TEXT, recommendation TEXT, status TEXT DEFAULT 'pending', answer TEXT, outcome TEXT,
            created_at TEXT, answered_at TEXT);
        CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY, agent TEXT, kind TEXT, title TEXT, body TEXT, item_key TEXT, ts TEXT,
            read INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS splits (
            id INTEGER PRIMARY KEY, agent TEXT, title TEXT, item_key TEXT, session_id TEXT, cwd TEXT,
            intro TEXT, created_at TEXT, opened INTEGER DEFAULT 0, closed_at TEXT);
        CREATE TABLE IF NOT EXISTS chat (
            id INTEGER PRIMARY KEY, agent TEXT, role TEXT, text TEXT, ts TEXT, cost_usd REAL DEFAULT 0);
        """
    )
    cols = {r[1] for r in conn.execute("PRAGMA table_info(items)")}
    for c in ("closed_at", "closed_reason", "picked", "seen_version"):
        if c not in cols:
            conn.execute(f"ALTER TABLE items ADD COLUMN {c} TEXT")
    for table, c in (("runs", "session_id"), ("decisions", "drafts"), ("runs", "report"), ("decisions", "category"),
                     ("decisions", "grant_tools")):
        if c not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {c} TEXT")
    migrate_prbot(conn)
    if not conn.execute("SELECT 1 FROM meta WHERE k = 'inbox_backfilled'").fetchone():
        for r in conn.execute("""SELECT agent, item_key, COUNT(*) n, GROUP_CONCAT('- ' || question, char(10)) qs,
                                 MAX(created_at) ts FROM decisions WHERE status = 'pending' GROUP BY agent, item_key"""):
            conn.execute("INSERT INTO notifications (agent, kind, title, body, item_key, ts) VALUES (?, 'decision', ?, ?, ?, ?)",
                         (r["agent"], f"{short_key(r['item_key'])}: {r['n']} decision(s) waiting", r["qs"], r["item_key"], r["ts"]))
        conn.execute("INSERT INTO meta VALUES ('inbox_backfilled', ?)", (now(),))
        conn.commit()
    return conn


def migrate_prbot(conn):
    """One-time import of the earlier single-agent prbot state into the pr-review agent."""
    if not LEGACY_PRBOT_DB.exists() or conn.execute("SELECT 1 FROM meta WHERE k = 'prbot_migrated'").fetchone():
        return
    old = sqlite3.connect(LEGACY_PRBOT_DB)
    old.row_factory = sqlite3.Row
    run_ids = {}
    for r in old.execute("SELECT * FROM runs ORDER BY id"):
        run_ids[r["id"]] = conn.execute(
            """INSERT INTO runs (agent, started_at, finished_at, status, trigger, items_seen, items_done,
               new_decisions, cost_usd, error) VALUES ('pr-review', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["started_at"], r["finished_at"], r["status"], r["trigger"], r["prs_seen"], r["prs_reviewed"],
             r["new_decisions"], r["cost_usd"], r["error"])).lastrowid
    for p in old.execute("SELECT * FROM prs"):
        data = {"repo": p["repo"], "number": p["number"], "author": p["author"], "role": p["role"]}
        conn.execute(
            """INSERT OR IGNORE INTO items (agent, key, title, url, data, version, done_version, last_run_at,
               session_id, cwd, summary, approach, verdict, findings, draft, run_count, in_queue)
               VALUES ('pr-review', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (p["key"], p["title"], p["url"], json.dumps(data), p["gh_updated_at"], p["reviewed_gh_updated_at"],
             p["last_reviewed_at"], p["session_id"], p["cwd"], p["summary"], p["approach"], p["verdict"],
             p["findings"], p["draft_review"], p["review_count"], p["in_queue"]))
    for d in old.execute("SELECT * FROM decisions"):
        conn.execute(
            """INSERT INTO decisions (agent, item_key, run_id, question, context, options, recommendation, status,
               answer, outcome, created_at, answered_at) VALUES ('pr-review', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d["pr_key"], run_ids.get(d["run_id"]), d["question"], d["context"], d["options"], d["recommendation"],
             d["status"], d["answer"], d["outcome"], d["created_at"], d["answered_at"]))
    conn.execute("INSERT INTO meta VALUES ('prbot_migrated', ?)", (now(),))
    conn.commit()


# ---------------------------------------------------------------- claude

def claude_args(*, session_id=None, resume=False, allowed=(), denied=(), model=None, add_dirs=(), system=None):
    cmd = []
    if allowed:
        cmd += ["--allowedTools", *allowed]
    if denied:
        cmd += ["--disallowedTools", *denied]
    if session_id:
        cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    if model:
        cmd += ["--model", model]
    for d in dict.fromkeys([str(Path.home()), *(str(Path(x).expanduser()) for x in add_dirs)]):
        cmd += ["--add-dir", d]  # agents can read anything under your home folder
    if system:
        cmd += ["--append-system-prompt", system]
    return cmd


def sandbox_prefix(paths):
    """sandbox-exec that makes these folders read-only (their .git excepted) for claude and everything it starts."""
    rules = [f'(require-all (subpath "{p}") (require-not (subpath "{p}/.git")))'
             for p in (str(Path(x).expanduser().resolve()) for x in paths) if Path(p).exists()]
    if not rules or not shutil.which("sandbox-exec"):
        return []
    return ["sandbox-exec", "-p", "(version 1)(allow default)(deny file-write* " + " ".join(rules) + ")"]


ACTIVITY = LOGS / "activity"  # one file per session: what a wake did, step by step, as it happens


LIVE = {}  # session id -> the claude process working in it (in this process), so the feed can stop it
STOPPED = set()


def kill_tree(pid):
    """Stop a process and everything under it. Claude Code runs commands in their own process groups, so stopping
    its group alone would leave a test run or a sleep going (and holding its output open)."""
    try:
        rows = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout.split("\n")
    except OSError:
        rows = []
    children = {}
    for row in rows:
        parts = row.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    tree, todo = [], [pid]
    while todo:
        p = todo.pop()
        tree.append(p)
        todo.extend(children.get(p, []))
    for p in reversed(tree):  # deepest first
        try:
            os.kill(p, signal.SIGTERM)
        except OSError:
            pass


def stop_work(session_id):
    """Stop the work in a session, with everything it started (helpers, test runs)."""
    proc = LIVE.get(session_id)
    if not proc or proc.poll() is not None:
        return False
    STOPPED.add(session_id)
    kill_tree(proc.pid)
    return True


def activity_path(session_id):
    return ACTIVITY / f"{session_id}.log"


def copy_text(text):
    """Put text on the macOS clipboard (works inside tmux, where terminal clipboard escapes often don't)."""
    subprocess.run(["pbcopy"], input=text, text=True)


def describe_step(block):
    """One plain line for a tool call or a remark, as the feed shows it. None for steps not worth showing."""
    if block.get("type") == "text":
        line = next((l.strip() for l in block.get("text", "").splitlines() if l.strip()), "")
        return f"says: {line[:160]}" if line else None
    if block.get("type") != "tool_use":
        return None
    name, inp = block.get("name", ""), block.get("input") or {}
    first = lambda v: str(v or "").strip().splitlines()[0][:160] if str(v or "").strip() else ""
    if name == "StructuredOutput":
        return "wrote its report"
    if name == "Bash":
        return f"$ {first(inp.get('command'))}"
    if name in ("Read", "NotebookRead"):
        return f"read {inp.get('file_path', '')}"
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"{'wrote' if name == 'Write' else 'edited'} {inp.get('file_path') or inp.get('notebook_path', '')}"
    if name in ("Grep", "Glob"):
        return f"searched for {first(inp.get('pattern'))}" + (f" in {inp['path']}" if inp.get("path") else "")
    if name in ("Agent", "Task"):
        return f"started a helper: {first(inp.get('description') or inp.get('prompt'))}"
    if name == "Skill":
        return f"used the {inp.get('skill') or inp.get('name', '')} skill"
    if name in ("WebFetch", "WebSearch"):
        return f"{'fetched' if name == 'WebFetch' else 'searched the web for'} {first(inp.get('url') or inp.get('query'))}"
    if name.startswith("mcp__"):
        _, server, tool = (name.split("__", 2) + ["", ""])[:3]
        return f"{server}: {tool.replace('_', ' ')}"
    if name in ("TodoWrite", "ToolSearch"):
        return None
    return name


def run_claude(prompt, *, session_id, resume, cwd, allowed, denied, schema, model, mode="dontAsk", add_dirs=(),
               protect=(), timeout=CLAUDE_TIMEOUT, on_step=None):
    """Run headless Claude Code and return (structured output, cost). Each step it takes is appended to
    activity_path(session_id) as it happens, so the feed can show the work live."""
    cmd = [*sandbox_prefix(protect), "claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--json-schema", json.dumps(schema), "--permission-mode", mode]
    cmd += claude_args(session_id=session_id, resume=resume, allowed=allowed, denied=denied, model=model,
                       add_dirs=add_dirs)
    LOGS.mkdir(parents=True, exist_ok=True)
    ACTIVITY.mkdir(parents=True, exist_ok=True)
    raw = open(LOGS / f"{session_id or 'oneoff'}.{int(time.time())}.json", "w")
    steps = open(activity_path(session_id or "oneoff"), "a")
    # its own process group: stopping it (or a timeout) also stops what it started, which could otherwise hold the
    # output open and leave this waiting forever
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    key = session_id or "oneoff"
    LIVE[key] = proc
    on_step and on_step("session", key)

    timer = threading.Timer(timeout, kill_tree, args=(proc.pid,))  # a run that hangs, even silently, is stopped on time
    timer.start()
    result = None
    try:
        for line in proc.stdout:
            raw.write(line)
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") == "result":
                result = ev
            elif ev.get("type") == "assistant":
                helper = bool(ev.get("parent_tool_use_id"))  # a helper's own steps
                for block in ev.get("message", {}).get("content", []):
                    text = describe_step(block)
                    if text:
                        steps.write(f"{dt.datetime.now():%H:%M:%S}\t{'helper' if helper else 'main'}\t"
                                    f"{text.replace(chr(9), ' ').replace(chr(10), ' ')}\n")
                        steps.flush()
                        on_step and on_step("step", ("  ↳ " if helper else "") + text)
        proc.wait(timeout=30)
    finally:
        timed_out = not timer.is_alive() and proc.returncode not in (0, None)
        timer.cancel()
        LIVE.pop(key, None)
        err = proc.stderr.read() if proc.stderr else ""
        raw.write("\n--- stderr ---\n" + err)
        raw.close()
        steps.close()
    if not result:
        if key in STOPPED:
            STOPPED.discard(key)
            raise RuntimeError("stopped")
        if timed_out:
            raise RuntimeError(f"timed out after {timeout // 60} minutes")
        raise RuntimeError(f"claude exited {proc.returncode}: {err.strip()[-400:]}")
    if result.get("is_error") or not result.get("structured_output"):
        raise RuntimeError(f"claude returned no structured output ({result.get('subtype')}): {str(result.get('result'))[:400]}")
    return result["structured_output"], result.get("total_cost_usd") or 0


def read_activity(session_id, last=None):
    """[(time, who, text)] of a session's steps; who is main or helper."""
    try:
        lines = activity_path(session_id).read_text().splitlines()
    except OSError:
        return []
    rows = [tuple((l.split("\t", 2) + ["", ""])[:3]) for l in lines if l.strip()]
    return rows[-last:] if last else rows


def item_fields(row):
    """The scanned item, backfilled from the stored row (older rows were saved without url/title)."""
    item = json.loads(row["data"] or "{}")
    item.setdefault("key", row["key"])
    if not item.get("url") and row["url"]:
        item["url"] = row["url"]
    if not item.get("title") and row["title"]:
        item["title"] = row["title"]
    m = re.fullmatch(r"(.+/.+)#(\d+)", row["key"])
    if m:
        item.setdefault("repo", m[1])
        item.setdefault("number", int(m[2]))
        item.setdefault("url", f"https://github.com/{m[1]}/pull/{m[2]}")
    return item


def build_prompt(agent, item, row, pending):
    fields = _Blank(item)
    if "{me}" in agent["prompt"]:
        fields["me"] = whoami()
    body = agent["prompt"].format_map(fields)
    lines = [
        body.strip(),
        "",
        "## Rules from crew",
        "- This run is read-only unless your tools say otherwise. Never post, send, push, merge or modify anything "
        "outward-facing. Anything you would post goes into `draft` plus a decision; the human decides.",
        "- Run one simple command per Bash call (no `cd`, `;`, `&&`, or shell variables) — compound commands are blocked.",
        f"- Work item: ```json\n{json.dumps(item, indent=2)}\n```",
    ]
    if agent["skills"]:
        lines.append(f"- Apply these skills with the Skill tool and fold their results into your findings: "
                     f"{', '.join('/' + s for s in agent['skills'])}. If a skill tries to post anywhere, skip that step.")
    if row and row["last_run_at"]:
        lines.append(f"- You worked this item before ({row['last_run_at']}, verdict {row['verdict']}). Focus on what "
                     "changed since then, but return the full current picture.")
    if pending:
        lines.append("- Decisions still waiting on the human (re-issue the relevant ones, drop resolved ones): "
                     + "; ".join(d["question"] for d in pending))
    lines.append("- Write `summary`, `approach`, `draft` and each decision's `context` the way you'd explain it to me in a "
                 "Claude Code chat: plain, direct sentences, short paragraphs, concrete specifics, no filler. Then run that "
                 "text through the /humanizer skill and return the humanized version.")
    lines.append("- Output: `summary` explains the item to someone who hasn't seen it; `approach` explains what you did "
                 "and why; `decisions` lists only what genuinely needs the human, each with options and your "
                 "recommendation. Keep decisions few.")
    if agent["extra_instructions"]:
        lines.append("- Extra instructions from the user: " + agent["extra_instructions"])
    return "\n".join(lines)


# ---------------------------------------------------------------- run

def desktop_notify(title, msg, sound=True, agent=None):
    """A macOS notification. With terminal-notifier, one per agent (a new one replaces the last) and clicking it opens
    that agent's feed in crew; otherwise a plain one via osascript."""
    tn = shutil.which("terminal-notifier") or next(
        (p for p in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier") if Path(p).exists()), None)
    if tn:
        cmd = [tn, "-title", title, "-message", msg[:240] or " ", "-group", f"crew-{agent or 'crew'}",
               "-activate", terminal_app()]
        if ICON.exists():
            cmd += ["-appIcon", str(ICON), "-contentImage", str(ICON)]
        if agent:
            cmd += ["-execute", f"{shlex.quote(sys.executable)} {shlex.quote(str(Path(__file__).resolve()))} "
                                f"open-feed {shlex.quote(agent)}"]
        if sound:
            cmd += ["-sound", "Glass"]
        subprocess.run(cmd, capture_output=True)
        return
    script = f"display notification {json.dumps(msg)} with title {json.dumps(title)}"
    if sound:
        script += ' sound name "Glass"'
    subprocess.run(["osascript", "-e", script], capture_output=True)


def terminal_app():
    """The terminal crew runs in (remembered whenever crew starts from one), unless config.json names another."""
    if config()["terminal_app"]:
        return config()["terminal_app"]
    try:
        return (HOME / "terminal_app").read_text().strip() or "com.apple.Terminal"
    except OSError:
        return "com.apple.Terminal"


def remember_terminal():
    bundle = os.environ.get("__CFBundleIdentifier")
    if bundle and sys.stdout.isatty():
        HOME.mkdir(parents=True, exist_ok=True)
        if not (HOME / "terminal_app").exists() or (HOME / "terminal_app").read_text().strip() != bundle:
            (HOME / "terminal_app").write_text(bundle)


def open_in_terminal(*args):
    """Start `crew <args>` in a new window of the user's terminal."""
    app = terminal_app()
    line = " ".join(shlex.quote(a) for a in [sys.executable, str(Path(__file__).resolve()), *args])
    if "ghostty" in app.lower():
        subprocess.run(["open", "-na", "Ghostty", "--args", "-e", sys.executable, str(Path(__file__).resolve()), *args])
    elif "iterm" in app.lower():
        subprocess.run(["osascript", "-e", f'tell application "iTerm" to create window with default profile command {json.dumps(line)}'])
    else:
        subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script {json.dumps(line)}',
                        "-e", 'tell application "Terminal" to activate'])


def cmd_open_feed(args):
    """Clicked a notification: show that agent's feed in the crew workspace (starting it if it isn't running)."""
    import crew_tmux as tx
    agent = load_agent(args.agent)
    conn = db()
    conn.execute("UPDATE notifications SET read = 1 WHERE agent = ?", (agent["name"],))
    conn.commit()
    if subprocess.run(["tmux", "-L", "crew", "has-session", "-t", tx.SESSION], capture_output=True).returncode == 0:
        tx.show(f"feed:{agent['name']}", tx.crew_cmd("feed", agent["name"]), agent["name"])
    else:
        open_in_terminal("ui")


def notify_event(agent, kind, title, body="", item_key=None, conn=None):
    """Record an inbox event for an agent and, if the agent wants that kind, raise a desktop notification."""
    conn = conn or db()
    conn.execute("INSERT INTO notifications (agent, kind, title, body, item_key, ts) VALUES (?, ?, ?, ?, ?, ?)",
                 (agent["name"], kind, title, body, item_key, now()))
    conn.commit()
    cfg = agent["notify"]
    if cfg.get("desktop") and kind in cfg.get("events", []):
        headline = {"decision": "needs you", "alert": "needs you", "paused": "paused", "failing": "keeps failing"}
        desktop_notify(f"{agent['name']} {headline.get(kind, kind)}", title, cfg.get("sound", False), agent["name"])


def run_agent(agent, *, item_ref=None, force=False, trigger="manual"):
    name = agent["name"]
    lock = open(HOME / f".{name}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print(f"[yellow]{name}: a run is already in progress.[/]")
        return
    conn = db()
    run_id = conn.execute("INSERT INTO runs (agent, started_at, status, trigger) VALUES (?, ?, 'running', ?)",
                          (name, now(), trigger)).lastrowid
    conn.commit()
    done = new_decisions = 0
    cost = 0.0
    workdir = WORK
    try:
        found = scan(agent)
        was_open = {r["key"]: r for r in conn.execute(
            "SELECT * FROM items WHERE agent = ? AND in_queue = 1 AND closed_at IS NULL", (name,))}
        conn.execute("UPDATE items SET in_queue = 0 WHERE agent = ?", (name,))
        for it in found:
            conn.execute(
                """INSERT INTO items (agent, key, title, url, data, version, in_queue) VALUES (?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(agent, key) DO UPDATE SET title=excluded.title, url=excluded.url, data=excluded.data,
                   version=excluded.version, in_queue=1,
                   closed_at=CASE WHEN closed_reason = 'removed' THEN closed_at END,
                   closed_reason=CASE WHEN closed_reason = 'removed' THEN closed_reason END""",
                (name, it["key"], it["title"], it.get("url"), json.dumps(it), item_version(it)))
        conn.commit()
        close_dropped(agent, conn, [r for k, r in was_open.items() if k not in {i["key"] for i in found}])
        rows = conn.execute("SELECT * FROM items WHERE agent = ? AND in_queue = 1 ORDER BY version DESC", (name,)).fetchall()
        if item_ref:
            rows = [find_item(conn, name, item_ref)]
        else:
            new = [r for r in rows if r["picked"] is None and not r["run_count"]]
            if not agent["auto_pick"]:
                if new and trigger == "schedule":
                    notify_event(agent, "decision", f"{len(new)} new {agent['labels']['item']}(s) to pick from",
                                 "\n".join(f"- {short_key(r['key'])} {r['title']}" for r in new[:15]), None, conn)
                for r in new:  # remember we've announced them
                    conn.execute("UPDATE items SET picked = '0' WHERE agent = ? AND key = ?", (name, r["key"]))
                rows = [r for r in rows if r["picked"] == "1"]
            rows = [r for r in rows if force or r["done_version"] != r["version"]][: agent["max_items_per_run"]]
        conn.execute("UPDATE runs SET items_seen = ? WHERE id = ?", (len(found), run_id))
        conn.commit()
        console.print(f"[bold]{name}[/] run #{run_id}: {len(found)} items, {len(rows)} to work.")

        busy = open_threads()
        for row in rows:
            if f"item:{name}:{row['key']}" in busy:
                console.print(f"  skipping [bold]{row['key']}[/] — its thread is open in the crew UI")
                continue
            item = item_fields(row)
            conn.execute("UPDATE runs SET current = ? WHERE id = ?", (row["key"], run_id))
            conn.commit()
            console.print(f"  working [bold]{row['key']}[/] …")
            first = not session_exists(row["session_id"])
            session_id = row["session_id"] or str(uuid.uuid4())
            cwd = row["cwd"] or item.get("cwd") or str(workdir)  # a session can only be resumed from its own cwd
            Path(cwd).mkdir(parents=True, exist_ok=True)
            pending = conn.execute("SELECT question FROM decisions WHERE agent = ? AND item_key = ? AND status = 'pending'",
                                   (name, row["key"])).fetchall()
            try:
                out, c = run_claude(build_prompt(agent, item, row, pending), session_id=session_id, resume=not first,
                                    cwd=cwd, allowed=expand_tools(agent["tools"]["run"]["allow"]),
                                    denied=expand_tools(agent["tools"]["run"]["deny"]), schema=output_schema(agent),
                                    model=agent["model"], add_dirs=agent["folders"])
            except Exception as e:  # keep going with the other items
                console.print(f"    [red]failed:[/] {e}")
                conn.execute("UPDATE runs SET error = COALESCE(error || char(10), '') || ? WHERE id = ?",
                             (f"{row['key']}: {e}", run_id))
                conn.execute("UPDATE items SET session_id = ?, cwd = ? WHERE agent = ? AND key = ?",
                             (None if first else session_id, cwd, name, row["key"]))
                conn.commit()
                continue
            cost += c
            done += 1
            conn.execute(
                """UPDATE items SET session_id=?, cwd=?, summary=?, approach=?, verdict=?, findings=?, draft=?,
                   last_run_at=?, done_version=version, run_count=run_count+1 WHERE agent=? AND key=?""",
                (session_id, cwd, out["summary"], out["approach"], out["verdict"], json.dumps(out["findings"]),
                 out["draft"], now(), name, row["key"]))
            conn.execute("UPDATE decisions SET status='superseded' WHERE agent=? AND item_key=? AND status='pending'",
                         (name, row["key"]))
            for d in out["decisions"]:
                conn.execute(
                    """INSERT INTO decisions (agent, item_key, run_id, question, context, options, recommendation,
                       created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (name, row["key"], run_id, d["question"], d["context"], json.dumps(d["options"]),
                     d["recommendation"], now()))
            new_decisions += len(out["decisions"])
            conn.commit()
            if out.get("alert"):
                notify_event(agent, "alert", f"{short_key(row['key'])}: {out['alert']}", out["summary"][:2000], row["key"], conn)
            if out["decisions"]:
                notify_event(agent, "decision", f"{short_key(row['key'])}: {len(out['decisions'])} decision(s) waiting",
                             "\n".join("- " + d["question"] for d in out["decisions"]), row["key"], conn)
            console.print(f"    → {out['verdict']}, {len(out['findings'])} findings, {len(out['decisions'])} decisions")
        status = "ok"
    except Exception as e:
        status = "error"
        conn.execute("UPDATE runs SET error = ? WHERE id = ?", (str(e), run_id))
        console.print(f"[red]{name} run failed:[/] {e}")
    conn.execute("""UPDATE runs SET finished_at=?, status=?, current=NULL, items_done=?, new_decisions=?, cost_usd=?
                    WHERE id=?""", (now(), status, done, new_decisions, cost, run_id))
    conn.commit()
    err = conn.execute("SELECT error FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
    if err:
        notify_event(agent, "error", f"run #{run_id} {'failed' if status == 'error' else 'had errors'}", err, None, conn)


def close_dropped(agent, conn, rows):
    """Items that left the scan: close them when they're merged/closed/resolved, and drop their pending decisions."""
    for row in rows:
        reason = "resolved"
        if agent["closed_check"]:
            cmd = agent["closed_check"].format_map(_Blank(json.loads(row["data"] or "{}")))
            r = subprocess.run(cmd, shell=True, cwd=agent["dir"], capture_output=True, text=True, timeout=60)
            state = r.stdout.strip().upper()
            if r.returncode != 0 or not state or state == "OPEN":
                continue  # still open (or unknown) — leave it, just out of the queue
            reason = state.lower()
        n = conn.execute("UPDATE decisions SET status = 'closed' WHERE agent = ? AND item_key = ? AND status = 'pending'",
                         (agent["name"], row["key"])).rowcount
        conn.execute("UPDATE items SET closed_at = ?, closed_reason = ? WHERE agent = ? AND key = ?",
                     (now(), reason, agent["name"], row["key"]))
        conn.commit()
        notify_event(agent, "closed", f"{short_key(row['key'])} {reason} — closed"
                     + (f", dropped {n} pending decision(s)" if n else ""), row["title"] or "", row["key"], conn)


def open_threads():
    """Keys of sessions currently open in the crew UI (tmux)."""
    r = subprocess.run(["tmux", "-L", "crew", "list-panes", "-a", "-F", "#{@crew_key}\t#{pane_dead}"],
                       capture_output=True, text=True)
    return {k for k, dead in (l.split("\t") for l in r.stdout.splitlines() if "\t" in l) if k and dead == "0"}


# ---------------------------------------------------------------- agentic agents (Overtime-style)
# An agentic agent is a folder: AGENT.md (its job, rules and how you like things, kept current by the agent), INDEX.md
# (its own map of the folder), KNOWLEDGE.md (what `crew learn` found), notes/, work/ and skills in .claude/skills/.
# Each wake is a fresh session: it decides what deserves attention and does the work (code in its own worktrees),
# asks you only what's yours to decide, and picks when it wakes next: after a delay, or on a watch.

AGENTIC_SCHEMA = {
    "type": "object",
    "properties": {
        "report": {"type": "string", "description": "Markdown, humanized: what you did this wake and what you found, briefly. Lead with anything that needs the user."},
        "decisions": {
            "type": "array",
            "description": "Only what is genuinely the user's call. Empty is normal.",
            "items": {
                "type": "object",
                "properties": {
                    "about": {"type": "string", "description": "owner/repo#number when it's about a PR or issue; otherwise a short kebab-case topic key."},
                    "title": {"type": "string", "description": "The PR's title, or a short title for the topic."},
                    "category": {"type": "string", "description": "Short kebab-case kind of question, the same across wakes (e.g. post-review, push-fix, rerun-ci, send-slack), so repeated answers can become a standing rule. 'standing-rule' when proposing one."},
                    "question": {"type": "string", "description": "One plain sentence a busy person understands at a glance, e.g. 'Push the 5 fixes and post your 4 replies on #12780?'. No hashes or ids."},
                    "context": {"type": "string", "description": "2-4 short plain lines: what this is and why it's their call. No commit hashes, thread ids, file paths or script names; a PR number or link is fine."},
                    "options": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 4, "description": "Short choices, under 8 words each, your recommended one first. Each is something you then do yourself; never 'run it yourself' or 'unblock a tool'."},
                    "recommendation": {"type": "string", "description": "One short sentence: which option and why."},
                    "details": {"type": "string", "description": "Markdown. Everything else the user might want before deciding (evidence, specifics, commits). Shown only when they ask. Empty if nothing."},
                    "drafts": {"type": "string", "description": "Markdown. The exact comments/reviews/messages you would post if approved, each with where it goes (file:line, PR conversation, channel). Empty if nothing would be posted."},
                    "grant": {"type": "array", "items": {"type": "string"}, "description": "Only for 'standing-rule': the blocked tool rules you'd use on your own from then on, exactly as listed to you. Empty otherwise."},
                },
                "required": ["about", "title", "category", "question", "context", "options", "recommendation", "details", "drafts", "grant"],
            },
        },
        "resolved": {"type": "array", "items": {"type": "integer"}, "description": "Ids of waiting decisions that no longer need the user (merged, closed, already handled, superseded by a new one)."},
        "next_wake_minutes": {"type": "integer", "minimum": 1, "maximum": 4320},
        "why_then": {"type": "string", "description": "One short line: why that wake time."},
        "watches": {
            "type": "array",
            "description": "Every watch you want running from now on (repeat the ones you keep; empty for none). crew runs each command without the model, and every line it prints wakes you, so print only when something happened. A quiet watch costs nothing.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "short kebab-case"},
                    "command": {"type": "string", "description": "A long-running shell command, run in your folder. E.g. `tail -F app.log | grep --line-buffered ERROR`, or a polling loop that prints only on change."},
                },
                "required": ["name", "command"],
            },
        },
        "status": {"type": "string", "description": "One short line for the sidebar: what you're on or watching."},
    },
    "required": ["report", "decisions", "resolved", "next_wake_minutes", "why_then", "watches", "status"],
}


def agent_state(agent, **update):
    p = agent["dir"] / ".state.json"
    try:
        st = json.loads(p.read_text())
    except (OSError, ValueError):
        st = {"awake": False}
    if update:
        st.update(update)
        p.write_text(json.dumps(st, indent=2))
    return st


def knowledge(agent):
    p = agent["dir"] / "KNOWLEDGE.md"
    return p.read_text() if p.exists() else "(nothing learned yet)"


def own_skills(agent):
    """The agent's own skills (.claude/skills/<name>/SKILL.md), as 'name: description' lines."""
    out = []
    for f in sorted((agent["dir"] / ".claude/skills").glob("*/SKILL.md")):
        m = re.search(r"^description:\s*(.+)$", f.read_text(), re.M)
        out.append(f"{f.parent.name}: {m[1].strip() if m else ''}  ({f})")
    return out


def agentic_tools(agent):
    """`crew learn`: read anything, write only its own folder, nothing outward-facing."""
    allowed, denied = chat_tools(agent)
    no_write = [t for t in expand_tools(["@no-write"]) if t not in ("Edit", "Write", "NotebookEdit")]
    return allowed, denied + no_write + ["Bash(crew run:*)", "Bash(crew tick:*)", "Bash(crew learn:*)",
                                         "Bash(crew split:*)", "Bash(crew answered:*)"]


def wake_tools(agent):
    """A background wake can use the machine like you would, except what's public, done as you, destructive or costs
    money. Those stay blocked unless you gave the agent a standing rule for them (agent.json "freedoms")."""
    freedoms = [t for t in agent.get("freedoms", []) if t in GRANTABLE]
    allowed = ["Bash", "Edit", "Write", "NotebookEdit", *expand_tools(agent["tools"]["run"]["allow"]), *freedoms]
    denied = [t for t in expand_tools(["@public", "@never"]) if t not in freedoms] + config()["blocked_tools"]
    return list(dict.fromkeys(allowed)), denied


def protected(agent):
    return [*config()["protect"], *agent.get("protect", [])]


def budget(agent):
    return agent.get("budget_usd") or config()["daily_budget_usd"]


def spent_today(conn, name):
    day = dt.date.today().isoformat()
    return (conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE agent = ? AND started_at >= ?",
                         (name, day)).fetchone()[0]
            + conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM chat WHERE agent = ? AND ts >= ?",
                           (name, day)).fetchone()[0])


def trust_hints(conn, name):
    """Kinds of question the user keeps answering, with their latest answers, so the agent can offer a standing rule.
    A kind already put to the user as a standing rule in the last 30 days isn't offered again."""
    since = (dt.datetime.now().astimezone() - dt.timedelta(days=30)).isoformat(timespec="seconds")
    offered = {r[0].removeprefix("rule:") for r in conn.execute(
        "SELECT item_key FROM decisions WHERE agent = ? AND category = 'standing-rule' AND created_at >= ?", (name, since))}
    seen = {}
    for r in conn.execute("""SELECT category, answer FROM decisions WHERE agent = ? AND category IS NOT NULL
                             AND category != 'standing-rule' AND answer IS NOT NULL AND answered_at >= ?
                             ORDER BY id DESC""", (name, since)):
        seen.setdefault(r["category"], []).append(r["answer"].strip().replace("\n", " ")[:120])
    return [f"- {c}: answered {len(a)} times. Latest: " + " | ".join(a[:5])
            for c, a in seen.items() if len(a) >= 3 and c not in offered]


def apply_grant(d, answer):
    """A yes to a standing-rule question lets the agent use those tools in its wakes from now on."""
    if d["category"] != "standing-rule" or not answer.strip().lower().startswith("yes"):
        return
    raw = json.loads((AGENTS_DIR / d["agent"] / "agent.json").read_text())
    grant = [t for t in json.loads(d["grant_tools"] or "[]") if t in GRANTABLE]
    save_agent_field(d["agent"], "freedoms", list(dict.fromkeys([*raw.get("freedoms", []), *grant])))


def is_running(agent):
    with open(HOME / f".{agent['name']}.lock", "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
        return False


# ---- watches: a cheap shell command per watch, run without the model; every line it prints wakes the agent

def watch_dir(agent):
    p = agent["dir"] / ".watches"
    p.mkdir(exist_ok=True)
    return p


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reconcile_watches(agent):
    """Start the watches the agent asked for and stop the rest. A stopped agent has none running."""
    wd = watch_dir(agent)
    st = agent_state(agent)
    want = {w["name"]: w["command"] for w in st.get("watches", [])} if st.get("awake") else {}
    for pf in wd.glob("*.pid"):
        pid, _, cmd = pf.read_text().partition("\t")
        if want.get(pf.stem) != cmd or not pid_alive(int(pid)):
            try:
                os.killpg(int(pid), signal.SIGTERM)
            except OSError:
                pass
            pf.unlink()
    for ex in wd.glob("*.exit"):
        if want.get(ex.stem) != ex.read_text().partition("\t")[0]:
            ex.unlink()  # changed or dropped: start fresh
    for name, cmd in want.items():
        if (wd / f"{name}.pid").exists() or (wd / f"{name}.exit").exists():
            continue  # running, or it exited and the agent sees why on its next wake
        log = open(wd / f"{name}.log", "a")
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch-run", agent["name"], name, cmd],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
        (wd / f"{name}.pid").write_text(f"{p.pid}\t{cmd}")


def watches_text(agent):
    wd = watch_dir(agent)
    out = []
    for w in agent_state(agent).get("watches", []):
        ex = wd / f"{w['name']}.exit"
        state = ("stopped, " + ex.read_text().partition("\t")[2]) if ex.exists() else "running"
        out.append(f"{w['name']} (`{w['command']}`): {state}")
    return out


def take_watch_events(agent):
    """Lines the watches printed since the last wake (and clears them)."""
    wd = watch_dir(agent)
    if not (wd / "events.log").exists():
        return ""
    os.replace(wd / "events.log", wd / "events.reading")
    lines = (wd / "events.reading").read_text().splitlines()
    (wd / "events.reading").unlink()
    return "\n".join(lines[:5] + ([f"… {len(lines) - 25} more …"] if len(lines) > 25 else []) + lines[5:][-20:])


def cmd_watch_run(args):
    """One watch: run its command and wake the agent on what it prints. crew starts and stops these itself."""
    agent = load_agent(args.agent)
    wd = watch_dir(agent)
    with open(wd / f"{args.name}.stderr", "w") as err:
        proc = subprocess.Popen(args.command, shell=True, cwd=agent["dir"], stdout=subprocess.PIPE, stderr=err,
                                stdin=subprocess.DEVNULL, text=True)
        last = 0.0
        for line in proc.stdout:
            if not line.strip():
                continue
            with open(wd / "events.log", "a") as f:
                f.write(f"{now()} [{args.name}] {line.rstrip()[:500]}\n")
            if time.time() - last > 30 and not is_running(agent):  # a burst becomes one wake
                last = time.time()
                start_background_run(agent["name"], trigger=f"its watch {args.name}")
        proc.wait()
    tail = (wd / f"{args.name}.stderr").read_text().strip()[-300:]
    (wd / f"{args.name}.exit").write_text(f"{args.command}\texited {proc.returncode}" + (f": {tail}" if tail else ""))


def run_agentic(agent, trigger="manual", note=None):
    name = agent["name"]
    lock = open(HOME / f".{name}.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        console.print(f"[yellow]{name} is already awake.[/]")
        return
    conn = db()
    st = agent_state(agent)
    if trigger != "you" and (st.get("paused_until") or "") > now():
        return  # waiting out a usage limit or the day's budget; watch events keep until then
    spent, limit = spent_today(conn, name), budget(agent)
    if spent >= limit:
        tomorrow = (dt.datetime.now().astimezone() + dt.timedelta(days=1)).replace(hour=0, minute=1, second=0,
                                                                                   microsecond=0)
        agent_state(agent, next_wake_at=tomorrow.isoformat(timespec="seconds"),
                    paused_until=tomorrow.isoformat(timespec="seconds"), why="today's budget is used up")
        notify_event(agent, "paused", f"Spent ${spent:.2f} of its ${limit:g} budget today. It picks up again tomorrow.",
                     f"Raise it with budget_usd in {agent['dir']}/agent.json, or daily_budget_usd in {CONFIG_PATH}.",
                     None, conn)
        console.print(Text(f"{name}: today's ${limit:g} budget is used up (${spent:.2f}); it wakes tomorrow.", style=GRAPHITE))
        return
    sid = str(uuid.uuid4())
    run_id = conn.execute("INSERT INTO runs (agent, started_at, status, trigger, session_id) VALUES (?, ?, 'running', ?, ?)",
                          (name, now(), trigger, sid)).lastrowid
    conn.commit()
    last = conn.execute("SELECT started_at, report FROM runs WHERE agent = ? AND status = 'ok' ORDER BY id DESC LIMIT 1",
                        (name,)).fetchone()
    waiting = conn.execute("SELECT id, item_key, question FROM decisions WHERE agent = ? AND status = 'pending'",
                           (name,)).fetchall()
    d = agent["dir"]
    for sub in ("notes", "work", ".claude/skills"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    me = whoami()
    job_file = agent["job_file"]
    index = (d / "INDEX.md").read_text().strip() if (d / "INDEX.md").exists() else "(none yet: write it this wake)"
    events = take_watch_events(agent)
    watches = watches_text(agent)
    skills = own_skills(agent)
    hints = trust_hints(conn, name)
    freedoms = [t for t in agent.get("freedoms", []) if t in GRANTABLE]
    protect = protected(agent)
    prompt = "\n".join(x for x in [
        f"## Your job ({d}/{job_file})",
        fill(agent["prompt"], _Blank({"me": me})).strip(),
        "",
        f"## What you've learned about @{me} ({d}/KNOWLEDGE.md)",
        knowledge(agent).strip(),
        "",
        f"## Your folder ({d}; INDEX.md is your map of it)",
        index,
        "",
        "## This wake",
        f"- Now: {now()}. Woken by: {trigger}." + (f" The user asked: {note}" if note else ""),
        f"- Last wake: {last['started_at'] if last else 'never'}." + (f" You reported then: {last['report'][:1500]}" if last and last["report"] else ""),
        f"- What your watches printed since then:\n{events}" if events else None,
        "- Your watches: " + ("; ".join(watches) if watches else "none"),
        "- Your own skills: " + ("; ".join(skills) if skills else "none yet"),
        f"- Spent today: ${spent:.2f} of your ${limit:g} daily budget.",
        "- Decisions already waiting on the user (don't repeat them; list in `resolved` any that no longer apply): "
        + ("; ".join(f"#{w['id']} {short_key(w['item_key'])}: {w['question']}" for w in waiting) or "none"),
        "- Kinds of question the user keeps answering (last 30 days):\n" + "\n".join(hints) if hints else None,
        "",
        "## How to work",
        "- You work on your own, like a senior colleague. Decide what deserves attention now and do the work: "
        "investigate, write and test code, get things fully ready. Don't stop to wait for the user; ask only what's "
        "genuinely theirs to decide, and carry on with everything else.",
        "- Ask first (as a decision) before anything public or done as the user: posting, commenting, reviewing, "
        "pushing, opening PRs, sending messages, changing tickets; before destroying anything outside your folder; and "
        "before spending money (e.g. creating environments). Those tools are blocked in your wakes"
        + (f", except the standing rules the user gave you: {', '.join(freedoms)}" if freedoms else "")
        + ". Put the exact text you'd post into the decision's `drafts`, worded the way the user writes. When the "
        "user answers, crew resumes you with permission to do exactly what they approved (push, post, message), so "
        "never ask them to run commands or scripts themselves, or to unblock tools.",
        "- Write questions for a busy person: one plain sentence, 2-4 lines of why, short options with yours first. "
        "Hashes, ids, paths and evidence go in `details`, which they open only if they want to.",
        f"- Code changes go in your own git worktree under {d}/work/ (`git worktree add {d}/work/<name> -b <branch>` "
        "from the repo, or the PR's branch), never in the user's checkouts"
        + (f" ({', '.join(protect)} is read-only to you)" if protect else "") + ". Run the tests, commit locally; "
        "pushing is the user's call.",
        "- For big work, split it: run helpers in parallel with the Agent tool, each with only what it needs (code "
        "changes each in their own worktree), and check what comes back before you rely on it.",
        f"- Keep your folder in order. {job_file} is your job description: keep it current with your role, what good "
        "looks like, your rules and how the user likes things (on your first wake, a brief from the user becomes a "
        f"proper job description). INDEX.md maps your folder for your next wake; update it when you add things. "
        "Notes go in notes/.",
        f"- When you work out how to do something you'll do again, write it down as a skill in "
        f"{d}/.claude/skills/<name>/SKILL.md (front matter with name and description, then the steps).",
        "- Use other skills when they fit (pr-review-toolkit:review-pr, code-review:code-review, receiving-code-review, "
        "systematic-debugging, and the user's own). If a skill tries to post, keep its output as a draft.",
        "- When you learn something durable about how the user works, add it to KNOWLEDGE.md.",
        "- Give every decision a `category` that stays the same across wakes. When the user has answered one kind of "
        "question the same way every time (see above), offer a standing rule instead of asking again: a decision with "
        "category 'standing-rule', about 'rule:<that category>', the rule in plain words, and in `grant` the exact "
        f"blocked tool rules it needs, from: {', '.join(GRANTABLE)}. If they agree, crew unblocks them and you write "
        f"the rule into {job_file} under '## Standing rules'.",
        "- Wake yourself: pick next_wake_minutes (soon if something is moving, hours or days if it's quiet), and use "
        "`watches` for anything worth reacting to within minutes.",
        "- Write report, context and drafts like a Claude Code reply, then pass them through the /humanizer skill.",
    ] if x is not None)
    allowed, denied = wake_tools(agent)
    try:
        out, cost = run_claude(prompt, session_id=sid, resume=False, cwd=str(d), allowed=allowed, denied=denied,
                               schema=AGENTIC_SCHEMA, model=agent["model"], add_dirs=agent["folders"], protect=protect,
                               timeout=config()["wake_timeout_minutes"] * 60)
        for i in out["resolved"]:
            conn.execute("UPDATE decisions SET status = 'closed' WHERE id = ? AND agent = ? AND status = 'pending'", (i, name))
        for q in out["decisions"]:
            key = q["about"].replace("https://github.com/", "").replace("/pull/", "#").replace("/issues/", "#")
            m = re.fullmatch(r"(.+/.+)#(\d+)", key)
            url = f"https://github.com/{m[1]}/pull/{m[2]}" if m else None
            grant = []
            if q["category"] == "standing-rule":
                q["options"] = ["Yes, decide these yourself from now on", "No, keep asking me"]
                grant = [t for t in q["grant"] if t in GRANTABLE]
            conn.execute("""INSERT INTO items (agent, key, title, url, data, version, in_queue, run_count, last_run_at)
                            VALUES (?, ?, ?, ?, '{}', ?, 1, 1, ?) ON CONFLICT(agent, key) DO UPDATE SET title = excluded.title,
                            in_queue = 1, closed_at = NULL, run_count = run_count + 1, last_run_at = excluded.last_run_at""",
                         (name, key, q["title"], url, now(), now()))
            conn.execute("""INSERT INTO decisions (agent, item_key, run_id, question, context, options, recommendation,
                            drafts, created_at, category, grant_tools) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                         (name, key, run_id, q["question"],
                          q["context"].strip() + (f"\n\n<!--details-->\n\n" + q["details"].strip() if q["details"].strip() else ""),
                          json.dumps(q["options"]), q["recommendation"],
                          q["drafts"], now(), q["category"], json.dumps(grant)))
        nxt = dt.datetime.now().astimezone() + dt.timedelta(minutes=out["next_wake_minutes"])
        watches = [{"name": re.sub(r"[^a-z0-9-]+", "-", w["name"].lower()).strip("-") or "watch", "command": w["command"]}
                   for w in out["watches"]][:10]
        agent_state(agent, next_wake_at=nxt.isoformat(timespec="seconds"), why=out["why_then"], status=out["status"],
                    watches=watches, failures=0, paused_until=None)
        conn.execute("""UPDATE runs SET finished_at=?, status='ok', current=NULL, new_decisions=?, cost_usd=?, report=?
                        WHERE id=?""", (now(), len(out["decisions"]), cost, out["report"], run_id))
        conn.commit()
        for q in out["decisions"]:
            notify_event(agent, "decision", f"{q['about'].split('#')[-1]}: {q['question']}", q["context"][:1500], None, conn)
        console.print(Markdown(out["report"]))
        console.print(Text(f"next wake in {out['next_wake_minutes']}m — {out['why_then']}", style=GRAPHITE))
    except Exception as e:
        conn.execute("UPDATE runs SET finished_at=?, status='error', current=NULL, error=? WHERE id=?", (now(), str(e), run_id))
        conn.commit()
        reset = usage_reset(str(e))
        if reset:  # out of plan usage: wait for the exact reset, then carry on by itself
            agent_state(agent, next_wake_at=reset.isoformat(timespec="seconds"), paused_until=reset.isoformat(timespec="seconds"),
                        why="waiting for the usage limit to reset")
            notify_event(agent, "paused", f"Claude usage limit reached. It picks up again at {reset:%H:%M}.", str(e), None, conn)
            console.print(Text(f"{name} paused until {reset:%H:%M}: usage limit", style=GRAPHITE))
            return
        failures = agent_state(agent).get("failures", 0) + 1
        backoff = [5, 15, 60, 240][min(failures, 4) - 1]
        agent_state(agent, failures=failures, why=f"retrying after an error ({failures})",
                    next_wake_at=(dt.datetime.now().astimezone() + dt.timedelta(minutes=backoff)).isoformat(timespec="seconds"))
        notify_event(agent, "failing" if failures == 3 else "error", f"Its last {failures} wakes failed: {str(e)[:160]}",
                     str(e), None, conn)
        console.print(Text(f"{name}: {e}", style=PENCIL))
    finally:
        reconcile_watches(agent)
        if (watch_dir(agent) / "events.log").exists():  # its watches fired while it worked: go again soon
            agent_state(agent, next_wake_at=now())


def usage_reset(message):
    """When a Claude usage-limit error says it resets ("resets 11:30am", "resets Oct 7, 9pm"), that moment; else None."""
    if "limit" not in message.lower():
        return None
    m = re.search(r"resets\s+(?:([A-Z][a-z]{2})\s+(\d{1,2}),?\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", message, re.I)
    nowt = dt.datetime.now().astimezone()
    if not m:
        return nowt + dt.timedelta(minutes=15)  # a limit with no stated reset: look again in a bit
    hour = int(m[3]) % 12 + (12 if m[5].lower() == "pm" else 0)
    at = nowt.replace(hour=hour, minute=int(m[4] or 0), second=0, microsecond=0)
    if m[1]:
        month = dt.datetime.strptime(m[1], "%b").month
        at = at.replace(month=month, day=int(m[2]))
    elif at <= nowt:
        at += dt.timedelta(days=1)
    return at + dt.timedelta(minutes=1)


def cmd_brief(args):
    """The morning AI brief. With --hook (Claude Code SessionStart): show today's once, or start making it."""
    if "morning" not in agent_names():
        return
    agent = load_agent("morning")
    conn = db()
    today = dt.date.today().isoformat()
    r = conn.execute("""SELECT * FROM runs WHERE agent = 'morning' AND status = 'ok' AND report IS NOT NULL
                        ORDER BY id DESC LIMIT 1""").fetchone()
    fresh = r and r["started_at"][:10] == today
    if not args.hook:
        console.print(Markdown(r["report"]) if r else Text("No brief yet. Wake it with: crew run morning", style=GRAPHITE))
        return
    st = agent_state(agent)
    if fresh and st.get("shown_on") != today:
        agent_state(agent, shown_on=today)
        lines = [l for l in r["report"].splitlines() if l.strip()]
        print(json.dumps({
            "systemMessage": "☀ Morning AI brief\n" + "\n".join(lines[:12])
                             + ("\n… full brief: crew brief, or ask me about it" if len(lines) > 12 else ""),
            "hookSpecificOutput": {"hookEventName": "SessionStart",
                                   "additionalContext": "The user's morning AI brief for today (they've just seen its "
                                                        "top lines; discuss it if they ask):\n\n" + r["report"]}}))
    elif not fresh and dt.datetime.now().hour >= 5 and st.get("preparing_on") != today:
        running = conn.execute("SELECT 1 FROM runs WHERE agent = 'morning' AND status = 'running'").fetchone()
        agent_state(agent, preparing_on=today)
        if not running:
            start_background_run("morning")
        print(json.dumps({"systemMessage": "☀ Your morning AI brief is being put together. It'll show at the start "
                                           "of your next Claude session (or run: crew brief)."}))


def cmd_tick(args):
    """Wake every awake agent whose time has come (launchd calls this every few minutes)."""
    for n in agent_names():
        a = load_agent(n)
        if a.get("mode") != "agentic":
            continue
        reconcile_watches(a)
        if not is_running(a):  # a wake that died without finishing (killed, crashed, laptop asleep) isn't working
            conn = db()
            conn.execute("""UPDATE runs SET status = 'error', finished_at = ?, error = 'interrupted before it finished'
                            WHERE agent = ? AND status = 'running'""", (now(), n))
            conn.commit()
        st = agent_state(a)
        if st.get("awake") and st.get("next_wake_at", "") <= now() and not is_running(a):
            start_background_run(n, trigger="its own schedule")


TICK_LABEL = "io.crew.tick"


def set_awake(agent, awake):
    """Awake agents wake themselves; stopped ones only run when you say so."""
    agent_state(agent, awake=awake, **({"next_wake_at": now(), "why": "just started"} if awake else {}))
    plist = LAUNCH_AGENTS / f"{TICK_LABEL}.plist"
    if not plist.exists():
        LOGS.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps({
            "Label": TICK_LABEL,
            "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "tick"],
            "StartInterval": 120, "RunAtLoad": True,
            "StandardOutPath": str(LOGS / "tick.log"), "StandardErrorPath": str(LOGS / "tick.log"),
            "EnvironmentVariables": {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())},
        }))
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True)
    reconcile_watches(agent)


def wake_text(agent):
    st = agent_state(agent)
    if st.get("paused_until") and st["paused_until"] > now():
        return f"usage limit · back {dt.datetime.fromisoformat(st['paused_until']):%H:%M}"
    if not st.get("awake"):
        return "stopped"
    at = st.get("next_wake_at")
    if not at or at <= now():
        return "waking"
    t = dt.datetime.fromisoformat(at)
    return f"wakes {t:%H:%M}" if t.date() == dt.date.today() else f"wakes {t:%a %H:%M}"


def cmd_learn(args):
    """Teach an agent how the user works, from their real GitHub history, into KNOWLEDGE.md."""
    agent = load_agent(args.agent)
    me = whoami()
    prompt = (f"{agent['prompt'].format_map(_Blank({'me': me})).strip()}\n\n"
              f"Before you start working, learn how @{me} actually works, so your reviews and drafted comments sound like "
              f"them and focus on what they care about. Study their real GitHub history relevant to your job, roughly the "
              "last 3-6 months: e.g. review comments and reviews they left on other people's PRs, how they reply to and "
              "resolve reviewers' comments on their own PRs, what they push back on, what they let go, tone and length, "
              "how they phrase suggestions. Use gh (search/api, GET only). Read enough to be confident: 40-80 real "
              f"comments across many PRs.\n\nThen write {agent['dir']}/KNOWLEDGE.md (replace the placeholder; keep it "
              "under ~150 lines): what they look for, what they ignore, how they word things (with 5-10 short real "
              "examples, quoted), how they handle feedback on their own PRs, and anything else durable. Facts only, from "
              "what you read. Finish by replying with a 3-line summary.")
    allowed, denied = agentic_tools(agent)
    with console.status(f"{agent['name']} is studying how you work…"):
        proc = subprocess.run(["claude", "-p", prompt, "--permission-mode", "dontAsk",
                               *claude_args(allowed=allowed, denied=denied, model=agent["model"])],
                              cwd=agent["dir"], capture_output=True, text=True, timeout=CLAUDE_TIMEOUT)
    console.print(proc.stdout.strip()[-2000:] or proc.stderr.strip()[-800:])


def cmd_run(args):
    names = [args.agent] if args.agent else agent_names()
    if args.background:
        for n in names:
            start_background_run(n, trigger=args.trigger if args.trigger != "manual" else None, note=args.note)
            print(f"{n} is working on it in the background; it shows in its feed.")
        return
    for n in names:
        a = load_agent(n)
        if a.get("mode") == "agentic":
            run_agentic(a, trigger=args.trigger if args.trigger != "manual" else "you", note=args.note)
        else:
            run_agent(a, item_ref=args.item, force=args.force, trigger=args.trigger)


# ---------------------------------------------------------------- schedule

def label(name):
    return f"io.crew.{name}"


def schedule_on(name):
    return subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label(name)}"], capture_output=True).returncode == 0


def unschedule(name):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label(name)}"], capture_output=True)
    (LAUNCH_AGENTS / f"{label(name)}.plist").unlink(missing_ok=True)


def cmd_schedule(args):
    agent = load_agent(args.agent)
    name = agent["name"]
    if args.interval == "off":
        unschedule(name)
        console.print(f"{name}: schedule [bold]off[/].")
        return
    if args.interval:
        parse_interval(args.interval)
        save_agent_field(name, "interval", args.interval)
        agent["interval"] = args.interval
    install_schedule(agent)
    console.print(f"{name}: schedule [bold green]on[/], every {agent['interval']}.")


def install_schedule(agent):
    name = agent["name"]
    plist = LAUNCH_AGENTS / f"{label(name)}.plist"
    LOGS.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": label(name),
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "run", name, "--trigger", "schedule"],
        "StartInterval": parse_interval(agent["interval"]),
        "RunAtLoad": False,
        "StandardOutPath": str(LOGS / f"{name}.schedule.log"),
        "StandardErrorPath": str(LOGS / f"{name}.schedule.log"),
        "EnvironmentVariables": {"PATH": os.environ.get("PATH", ""), "HOME": str(Path.home())},
    }))
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{label(name)}"], capture_output=True)
    subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], check=True, capture_output=True)


# ---------------------------------------------------------------- views

def agent_stats(conn, name):
    q = lambda sql: conn.execute(sql, (name,)).fetchone()[0]
    return {
        "runs": q("SELECT COUNT(*) FROM runs WHERE agent = ?"),
        "cost": q("SELECT COALESCE(SUM(cost_usd), 0) FROM runs WHERE agent = ?"),
        "pending": q("SELECT COUNT(*) FROM decisions WHERE agent = ? AND status = 'pending'"),
        "items": q("SELECT COUNT(*) FROM items WHERE agent = ? AND in_queue = 1"),
        "last": conn.execute("SELECT * FROM runs WHERE agent = ? ORDER BY id DESC LIMIT 1", (name,)).fetchone(),
    }


def next_run_text(agent, last):
    if not schedule_on(agent["name"]):
        return "—"
    if not last:
        return f"within {agent['interval']}"
    nxt = dt.datetime.fromisoformat(last["started_at"]) + dt.timedelta(seconds=parse_interval(agent["interval"]))
    mins = int((nxt - dt.datetime.now().astimezone()).total_seconds() // 60)
    return f"in ~{mins}m" if mins > 0 else "any moment"


def last_run_text(last):
    if not last:
        return "—"
    if last["status"] == "running":
        return f"[yellow]running: {last['current'] or 'scanning'}[/]"
    color = "green" if last["status"] == "ok" else "red"
    return f"{ago(last['started_at'])} [{color}]{last['status']}[/]"


def runs_table(conn, name=None, limit=6, title="Recent runs"):
    t = Table(title=title, expand=True, title_justify="left")
    cols = (["Agent"] if not name else []) + ["#", "Started", "Took", "Trigger", "Status", "Seen", "Worked", "Decisions", "Cost", "Errors"]
    for c in cols:
        t.add_column(c, overflow="fold", no_wrap=c != "Errors")
    sql = "SELECT * FROM runs" + (" WHERE agent = ?" if name else "") + " ORDER BY id DESC LIMIT ?"
    for r in conn.execute(sql, ((name,) if name else ()) + (limit,)):
        took = "—"
        if r["finished_at"]:
            took = f"{int((dt.datetime.fromisoformat(r['finished_at']) - dt.datetime.fromisoformat(r['started_at'])).total_seconds())}s"
        st = {"ok": "green", "error": "red", "running": "yellow"}.get(r["status"], "white")
        row = ([r["agent"]] if not name else []) + [
            str(r["id"]), ago(r["started_at"]), took, r["trigger"] or "", f"[{st}]{r['status']}[/]",
            str(r["items_seen"]), str(r["items_done"]), str(r["new_decisions"]), f"${r['cost_usd']:.2f}",
            (r["error"] or "")[:120]]
        t.add_row(*row)
    return t


def overview():
    conn = db()
    t = Table(title="Agents", expand=True, title_justify="left")
    for c in ("Agent", "What it does", "Schedule", "Runs", "Last run", "Next", "Items", "Pending", "Usage"):
        t.add_column(c, ratio=1 if c == "What it does" else None, no_wrap=True, overflow="ellipsis")
    total_pending = 0
    for n in agent_names():
        a, s = load_agent(n), agent_stats(conn, n)
        total_pending += s["pending"]
        t.add_row(f"[bold]{n}[/]", a["description"],
                  f"[green]every {a['interval']}[/]" if schedule_on(n) else "[dim]off[/]", str(s["runs"]),
                  last_run_text(s["last"]), next_run_text(a, s["last"]), str(s["items"]),
                  f"[bold red]{s['pending']}[/]" if s["pending"] else "0", f"${s['cost']:.2f}")
    head = Text.from_markup(f"[bold {PAPER}]crew[/]   [{GRAPHITE}]{len(agent_names())} agents[/]   "
                            + (f"[bold {PENCIL}]{total_pending} open queries[/]" if total_pending
                               else f"[{GRAPHITE}]nothing waiting[/]"))
    hint = ("[dim]crew <agent> · crew decide · crew chat <agent> [item] · crew new <name> --describe \"…\" · "
            "crew schedule <agent> 30m · crew skills <agent> add <skill>[/]")
    return Group(head, t, runs_table(conn, None, 6), hint)


def agent_dashboard(name):
    a = load_agent(name)
    conn = db()
    s = agent_stats(conn, name)
    head = "\n".join([
        f"{a['description']}",
        f"Schedule: {'[green]every ' + a['interval'] + '[/]' if schedule_on(name) else '[dim]off[/]'}  ·  Runs: [bold]{s['runs']}[/]"
        f"  ·  Last: {last_run_text(s['last'])}  ·  Next: {next_run_text(a, s['last'])}",
        f"Pending decisions: [bold {'red' if s['pending'] else 'green'}]{s['pending']}[/]  ·  Usage: ${s['cost']:.2f}  ·  "
        f"Skills: {', '.join(a['skills']) or '[dim]none[/]'}",
    ])
    t = Table(title=a["labels"]["item"] + "s", expand=True, title_justify="left")
    cols = [a["labels"]["item"], *[c.title() for c in a["columns"]], "Title", "Verdict", "Runs", "Pending", "Last", "State"]
    for c in cols:
        t.add_column(c, ratio=1 if c == "Title" else None, min_width=20 if c == "Title" else None,
                     no_wrap=True, overflow="ellipsis")
    for r in conn.execute(
            """SELECT i.*, (SELECT COUNT(*) FROM decisions d WHERE d.agent = i.agent AND d.item_key = i.key
                            AND d.status = 'pending') AS pend
               FROM items i WHERE agent = ? ORDER BY in_queue DESC, pend DESC, version DESC LIMIT 30""", (name,)):
        data = json.loads(r["data"] or "{}")
        state = ("[dim]gone[/]" if not r["in_queue"] else
                 "[yellow]changed[/]" if r["done_version"] != r["version"] else "up to date")
        t.add_row(short_key(r["key"]), *[str(data.get(c, "")) for c in a["columns"]], r["title"] or "",
                  f"[{VERDICT_COLORS.get(r['verdict'], 'white')}]{r['verdict'] or '—'}[/]", str(r["run_count"]),
                  f"[bold red]{r['pend']}[/]" if r["pend"] else "0", ago(r["last_run_at"]), state)
    hint = f"[dim]crew show {name} <item> · crew decide {name} · crew chat {name} [item] · crew run {name}[/]"
    return Group(Text.from_markup(f"[bold {PAPER}]{name}[/]"), Text.from_markup(head), t, runs_table(conn, name, 6), hint)


def short_key(key):
    return key.split("/", 1)[1] if "/" in key and "#" in key else key


def cmd_status(args):
    console.print(agent_dashboard(args.agent) if args.agent else overview())


def cmd_watch(args):
    render = (lambda: agent_dashboard(args.agent)) if args.agent else overview
    with Live(render(), console=console, refresh_per_second=1, screen=True) as live:
        try:
            while True:
                time.sleep(5)
                live.update(render())
        except KeyboardInterrupt:
            pass


def find_item(conn, agent, ref):
    ref = str(ref)
    rows = conn.execute("SELECT * FROM items WHERE agent = ?", (agent,)).fetchall()
    exact = [r for r in rows if ref in (r["key"], r["url"])]
    matches = exact or [r for r in rows if r["key"].endswith("#" + ref.lstrip("#")) or r["key"].endswith("/" + ref)] \
        or [r for r in rows if ref.lower() in r["key"].lower()]
    if not matches:
        raise SystemExit(f"No {agent} item matching {ref!r}. Run `crew {agent}` to see them.")
    if len(matches) > 1:
        raise SystemExit(f"{ref!r} is ambiguous: {', '.join(r['key'] for r in matches[:8])}")
    return matches[0]


def show_item(agent, row):
    """The galley: what the item is, how the agent read it, what it found, and its draft — flush left, no boxes."""
    lab = agent["labels"]
    num = short_key(row["key"]).split("#")[-1]
    console.print()
    head = Text()
    head.append(f"{num}  ", style=f"bold {PAPER}")
    head.append(row["title"] or "", style=f"bold {PAPER}")
    console.print(head)
    meta = Text(f"{agent['name']}  ·  {galley.verdict_word(row['verdict']) if row['verdict'] else 'not reviewed yet'}"
                f"  ·  {ago(row['last_run_at'])}" + (f"  ·  {row['url']}" if row["url"] else ""), style=GRAPHITE)
    console.print(meta)
    if not row["summary"]:
        return
    for title, body in ((lab["summary"], row["summary"]), (lab["approach"], row["approach"])):
        console.print()
        console.print(galley.label(title))
        console.print(Markdown(body))
    findings = json.loads(row["findings"] or "[]")
    if findings:
        console.print()
        console.print(galley.label("Findings"))
        sev_style = {"blocking": f"bold {PAPER}", "suggestion": PAPER, "nit": GRAPHITE, "info": GRAPHITE}
        for f in findings:
            line = Text()
            line.append(f"{f['severity']:<11}", style=sev_style.get(f["severity"], PAPER))
            line.append(f["title"], style=f"bold {PAPER}")
            where = f"{f.get('file', '')}:{f['line']}" if f.get("line") else f.get("file", "")
            if where:
                line.append(f"  {where}", style=GRAPHITE)
            console.print(line)
            console.print(Markdown(f["detail"]))
            console.print()
    if row["draft"]:
        console.print(galley.label(lab["draft"]))
        console.print(Markdown(row["draft"]))


def cmd_show(args):
    agent = load_agent(args.agent)
    conn = db()
    row = find_item(conn, agent["name"], args.item)
    show_item(agent, row)
    for d in conn.execute("SELECT * FROM decisions WHERE agent=? AND item_key=? AND status!='superseded' ORDER BY id",
                          (agent["name"], row["key"])):
        console.print(f"  [{'red' if d['status'] == 'pending' else 'dim'}]#{d['id']} {d['status']}[/] {d['question']}"
                      + (f"  → {d['answer']}" if d["answer"] else ""))


# ---------------------------------------------------------------- chat

def chat_tools(agent):
    """The main chat: reads freely; anything that posts or pushes isn't pre-approved, so Claude Code asks the user."""
    own = f"//{str(agent['dir']).lstrip('/')}/**"
    allowed = expand_tools(agent["tools"]["run"]["allow"]) + [
        "Bash(crew:*)", "Bash(sqlite3:*)", f"Edit({own})"]  # Edit rules cover every file-writing tool
    denied = ["Bash(crew decide:*)", "Bash(crew chat:*)", "Bash(crew ui:*)", "Bash(crew remove:*)", "Bash(crew watch:*)",
              "Bash(gh pr merge:*)", "Bash(git push --force:*)", "Bash(git push -f:*)"]
    return allowed, denied


def chat_system(agent):
    d = agent["dir"]
    return (f"You are the crew agent '{agent['name']}': {agent['description']}\n"
            f"Your job ({agent['job_file']}, which you keep current):\n---\n{agent['prompt']}\n---\n"
            f"Your settings are in {d}/agent.json and your job in {d}/{agent['job_file']}; edit them when the user asks "
            "you to change your job, rules or settings. Between chats you work on your own in background wakes (see "
            f"`crew runs {agent['name']}`); your folder map is {d}/INDEX.md, your notes are in {d}/notes/"
            + (f", and your own skills: {'; '.join(own_skills(agent))}" if own_skills(agent) else "") + ".\n"
            f"Inspect your state with `crew status {agent['name']}`, `crew show {agent['name']} <item>`, "
            f"`crew runs {agent['name']}`, `crew inbox {agent['name']}`, or sqlite3 {DB_PATH} "
            f"(tables runs, items, decisions, notifications; agent = '{agent['name']}'). "
            f"Trigger a fresh run with `crew run {agent['name']} [--item <key>]`.\n"
            f"For status questions (\"what's the status\", \"what's left\", \"which PRs need me\"), run "
            f"`crew threads {agent['name']}` first: it lists every {agent['labels']['item']} you track with its state, "
            "open queries, and its own chat thread (whether a chat is open, the last thing the user asked there and "
            f"your last answer). For one item's full conversation run `crew threads {agent['name']} <number> --full`. "
            "Answer from that data: lead with what needs the user, then each item in a line or two.\n"
            "Separate chats: the user mostly talks to you here. When they ask to take something into its own chat "
            "(\"let's do #12307 in a separate chat\", \"split this out\"), run "
            f"`crew split {agent['name']} \"<short title>\" --item <PR number, if it's about one> --context \"<what you two "
            "have said about it so far, 3-6 sentences, including any decisions>\"`. crew opens that chat under you in the "
            "rail and switches the user to it; reply with one line saying it's open. List them with "
            f"`crew splits {agent['name']}`.\n"
            + recent_reports(agent) +
            f"What you've learned about how the user works is in {agent['dir']}/KNOWLEDGE.md:\n---\n{knowledge(agent)}\n---\n"
            "When the user tells you how they like things (what to focus on, what to skip, how to word comments, "
            "how they handle feedback), add it to KNOWLEDGE.md so every future wake uses it. You decide what to work "
            "on; the user steers you by talking to you.\n"
            f"Things waiting on the user (`crew asks {agent['name']}` has them in full):\n{waiting_text(agent)}\n"
            "When the user's message answers, approves or declines one of those ('yes push all', 'skip it', 'post it "
            "but drop the nit'), run `crew answer <id> \"<their words>\"`: crew has you carry it out with the "
            "permission their answer gives (pushing, posting) and prints what was done. Tell them that in a line or "
            "two. Don't push or post yourself from this chat, and never ask them to run commands or unblock tools.\n"
            "For anything that takes more than a minute (a review, a fix, an investigation), run "
            f"`crew run {agent['name']} --background --note \"<what they asked for>\"` and tell them you've started "
            "it; they watch it live in your feed and you report there when it's done. Every comment you'd post is a draft "
            "until the user approves it.\n"
            "The user is messaging you about your work. Be direct; read-only unless they ask you to change your own files. "
            "Explain things the way Claude Code would in a chat — plain sentences, concrete specifics — and use the "
            "/humanizer skill on longer explanations before you send them.")


def recent_reports(agent, n=3):
    rows = db().execute("""SELECT started_at, report FROM runs WHERE agent = ? AND status = 'ok' AND report IS NOT NULL
                           ORDER BY id DESC LIMIT ?""", (agent["name"], n)).fetchall()
    if not rows:
        return ""
    return ("What you reported on your latest wakes (newest first; the user has seen these in your feed):\n"
            + "\n".join(f"--- {r['started_at']} ---\n{r['report'][:1500]}" for r in rows) + "\n---\n")


def waiting_text(agent):
    rows = db().execute("SELECT id, item_key, question, options FROM decisions WHERE agent = ? AND status = 'pending' "
                        "ORDER BY id", (agent["name"],)).fetchall()
    return "\n".join(f"- #{r['id']} ({short_key(r['item_key'])}) {r['question']} Options: "
                     + "; ".join(json.loads(r["options"] or "[]")) for r in rows) or "- nothing right now"


def session_exists(session_id):
    """Claude Code only writes a session file once the first message is sent; an id alone can't be resumed."""
    import glob
    return bool(session_id) and bool(glob.glob(str(Path.home() / ".claude/projects/*" / f"{session_id}.jsonl")))


def chat_session(agent):
    """(session_id, cwd) of the agent's own chat, or (None, WORK) before the first message."""
    marker = agent["dir"] / ".chat_session"
    if not marker.exists():
        return None, WORK
    sid, _, cwd = marker.read_text().strip().partition("\t")
    return sid, Path(cwd) if cwd else agent["dir"]  # older chats were started in the agent's folder


def save_chat_session(agent, sid, cwd):
    (agent["dir"] / ".chat_session").write_text(f"{sid}\t{cwd}")


def send_message(agent, message, on_event=None):
    """Send one message to an agent's own chat session and return its reply. on_event(kind, text) streams progress:
    session (its id, to stop it), delta (reply text as it's written), step (a tool it used)."""
    conn = db()
    conn.execute("INSERT INTO chat (agent, role, text, ts) VALUES (?, 'user', ?, ?)", (agent["name"], message, now()))
    conn.commit()
    sid, cwd = chat_session(agent)
    new_sid = sid or str(uuid.uuid4())
    allowed, denied = chat_tools(agent)
    cwd.mkdir(parents=True, exist_ok=True)
    cmd = ["claude", "-p", message, "--output-format", "stream-json", "--verbose", "--include-partial-messages",
           "--permission-mode", "dontAsk"]
    cmd += claude_args(session_id=new_sid, resume=session_exists(sid), allowed=allowed, denied=denied, model=agent["model"],
                       add_dirs=agent["folders"], system=chat_system(agent))
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            text=True, start_new_session=True)
    LIVE[new_sid] = proc
    on_event and on_event("session", new_sid)
    texts, final, cost, error = [], None, 0.0, None
    try:
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = ev.get("type")
            if kind == "stream_event" and not ev.get("parent_tool_use_id"):
                delta = (ev.get("event") or {}).get("delta") or {}
                if delta.get("type") == "text_delta":
                    on_event and on_event("delta", delta.get("text", ""))
            elif kind == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block["text"].strip():
                        texts.append(block["text"])
                        on_event and on_event("text", block["text"])
                    elif block.get("type") == "tool_use":
                        step = describe_step(block)
                        step and on_event and on_event("step", ("  ↳ " if ev.get("parent_tool_use_id") else "") + step)
            elif kind == "result":
                cost = ev.get("total_cost_usd") or 0
                final = ev.get("result")
                if ev.get("is_error"):
                    error = str(ev.get("result"))
        proc.wait()
    finally:
        LIVE.pop(new_sid, None)
    if not sid:
        save_chat_session(agent, new_sid, cwd)
    if new_sid in STOPPED:
        STOPPED.discard(new_sid)
        text = (texts[-1] + "\n\n_(stopped)_") if texts else "_Stopped._"
    else:
        if proc.returncode and not texts and not final:
            error = error or proc.stderr.read().strip()[-400:]
        if error and not texts:
            conn.execute("INSERT INTO chat (agent, role, text, ts) VALUES (?, 'agent', ?, ?)",
                         (agent["name"], f"I couldn't reply: {error[:300]}", now()))
            conn.commit()
            raise RuntimeError(error)
        text = final if isinstance(final, str) and final.strip() else (texts[-1] if texts else "")
    conn.execute("INSERT INTO chat (agent, role, text, ts, cost_usd) VALUES (?, 'agent', ?, ?, ?)",
                 (agent["name"], text, now(), cost))
    conn.commit()
    return text


def chat_item(row):
    if not row["session_id"]:
        raise SystemExit("This item has no session yet — run the agent on it first.")
    console.print(f"[dim]Opening the session for {row['key']} in Claude Code (exit to come back)…[/]")
    subprocess.run([sys.executable, str(Path(__file__).resolve()), "thread", row["agent"], row["key"]])


def agent_status_text(agent):
    """One line of live state: what it's doing and what's waiting on the user."""
    conn = db()
    needs = conn.execute("SELECT COUNT(*) FROM decisions WHERE agent = ? AND status = 'pending'", (agent["name"],)).fetchone()[0]
    running = conn.execute("SELECT 1 FROM runs WHERE agent = ? AND status = 'running'", (agent["name"],)).fetchone()
    state = "working now" if running else (wake_text(agent) if agent.get("mode") == "agentic" else "manual")
    spend = f"${spent_today(conn, agent['name']):.2f} of ${budget(agent):g} today · " if agent.get("mode") == "agentic" else ""
    return f"{agent['name']} · {state} · {spend}" + (f"{needs} waiting on you" if needs else "nothing waiting on you")


def identity_args(agent, title=None):
    """Make a Claude Code session visibly the agent: its name on the input box and terminal title, a line about it at
    startup, its live state in the status line, and no generic placeholder. Claude Code's own instructions stay."""
    me = str(Path(__file__).resolve())
    settings = {
        "companyAnnouncements": [f"{agent['name']}: {agent['description']}  ·  {agent_status_text(agent).split(' · ', 1)[1]}"
                                 + (f"\nThis chat: {title}" if title else "")],
        "statusLine": {"type": "command", "command": f"{shlex.quote(sys.executable)} {shlex.quote(me)} agent-status {agent['name']}"},
        "promptSuggestionEnabled": False,
        "spinnerTipsOverride": {"excludeDefault": True, "tips": [
            f"Ask {agent['name']}: what needs me right now?", f"Tell {agent['name']} how you like things; it remembers.",
            "Say \"take this into its own chat\" to split a topic off."]},
    }
    name = f"{agent['name']} · {title}" if title else agent["name"]
    return ["-n", name, "--settings", json.dumps(settings)]


def cmd_agent_status(args):
    try:
        print(agent_status_text(load_agent(args.agent)))
    except (Exception, SystemExit):
        print(args.agent)


def open_agent_session(agent):
    """The agent's chat session in full interactive Claude Code (same conversation as `crew ask` and the UI)."""
    sid, cwd = chat_session(agent)
    new_sid = sid or str(uuid.uuid4())
    allowed, denied = chat_tools(agent)
    cwd.mkdir(parents=True, exist_ok=True)
    if not sid:
        save_chat_session(agent, new_sid, cwd)
    subprocess.run(["claude", *identity_args(agent),
                    *claude_args(session_id=new_sid, resume=session_exists(sid), allowed=allowed, denied=denied,
                                 model=agent["model"], add_dirs=agent["folders"], system=chat_system(agent))],
                   cwd=cwd)


def transcript(session_id):
    """[(role, text)] of a Claude Code session, text turns only (tool calls and results skipped)."""
    import glob
    files = glob.glob(str(Path.home() / ".claude/projects/*" / f"{session_id}.jsonl"))
    turns = []
    if not files:
        return turns
    for line in open(files[0], errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        m = e.get("message") or {}
        content = m.get("content")
        if e.get("type") == "user" and not e.get("isMeta"):
            texts = [content] if isinstance(content, str) else [b.get("text", "") for b in content or []
                                                                  if isinstance(b, dict) and b.get("type") == "text"]
            for t in texts:
                if t.strip() and not t.startswith("<"):
                    turns.append(("user", t.strip()))
        elif e.get("type") == "assistant":
            for b in content or []:
                if isinstance(b, dict) and b.get("type") == "text" and b["text"].strip():
                    turns.append(("agent", b["text"].strip()))
    return turns


def thread_summary(row, agent):
    """The item's own chat: whether it's open in crew, the last thing you asked, and the agent's last answer."""
    if not row["session_id"]:
        return {"thread": "none yet"}
    turns = transcript(row["session_id"])
    live = bool(row["key"]) and f"item:{agent}:{row['key']}" in open_threads()
    asked = [t for r, t in turns if r == "user" and not t.startswith(("You are ", "The human answered"))]
    said = [t for r, t in turns if r == "agent"]
    return {"thread": "open in crew" if live else "saved",
            "you_last_asked": asked[-1][:300] if asked else None,
            "agent_last_said": said[-1][:600] if said else None,
            "turns": len(turns)}


def cmd_threads(args):
    """Every item an agent tracks, with its state, open queries and its own chat thread."""
    agent = load_agent(args.agent)
    conn = db()
    if args.item:
        row = find_item(conn, agent["name"], args.item)
        console.print(Text(f"{short_key(row['key'])}  {row['title']}", style=f"bold {PAPER}"))
        for role, text in transcript(row["session_id"]) if row["session_id"] else []:
            console.print(galley.label("you" if role == "user" else agent["name"]))
            console.print(Markdown(text if args.full else text[:1500]))
        return
    rows = conn.execute(
        """SELECT i.*, (SELECT COUNT(*) FROM decisions d WHERE d.agent = i.agent AND d.item_key = i.key
                        AND d.status = 'pending') AS pend,
                  (SELECT GROUP_CONCAT(question, ' | ') FROM decisions d WHERE d.agent = i.agent AND d.item_key = i.key
                        AND d.status = 'pending') AS questions
           FROM items i WHERE agent = ? AND closed_at IS NULL AND (in_queue = 1 OR session_id IS NOT NULL)
           ORDER BY pend DESC, (picked = '1') DESC, version DESC""", (agent["name"],)).fetchall()
    out = []
    for r in rows:
        out.append({"pr": short_key(r["key"]), "title": r["title"], "url": r["url"],
                    "picked": r["picked"] == "1" or agent["auto_pick"],
                    "verdict": galley.verdict_word(r["verdict"]) if r["run_count"] else "not reviewed yet",
                    "last_review": r["last_run_at"], "changed_since_you_looked": bool(
                        r["run_count"] and r["seen_version"] and r["seen_version"] != r["version"]),
                    "open_queries": r["questions"].split(" | ") if r["questions"] else [],
                    "summary": (r["summary"] or "")[:400] or None, **thread_summary(r, agent["name"])})
    if args.json or not sys.stdout.isatty():
        print(json.dumps(out, indent=1))
        return
    for o in out:
        line = Text()
        line.append(f"{o['pr'].split('#')[-1]:<7}", style=PAPER)
        line.append(o["title"] or "", style=f"bold {PAPER}")
        console.print(line)
        meta = f"       {o['verdict']}  ·  thread {o['thread']}" + ("  ·  changed" if o["changed_since_you_looked"] else "")
        console.print(Text(meta, style=GRAPHITE))
        for q in o["open_queries"]:
            console.print(Text(f"     ▍ {q}", style=PENCIL))
        if o.get("you_last_asked"):
            console.print(Text(f"       you asked: {o['you_last_asked'][:140]}", style=GRAPHITE))


def cmd_asks(args):
    """What an agent is waiting on the user for, with drafts — for the agent's own chat."""
    agent = load_agent(args.agent)
    rows = db().execute("SELECT * FROM decisions WHERE agent = ? AND status = 'pending' ORDER BY id", (agent["name"],))
    print(json.dumps([{"id": d["id"], "pr": short_key(d["item_key"]), "question": d["question"], "context": d["context"],
                       "options": json.loads(d["options"]), "recommendation": d["recommendation"],
                       "drafts": d["drafts"], "asked": d["created_at"]} for d in rows], indent=1))


def cmd_answered(args):
    conn = db()
    d = conn.execute("SELECT * FROM decisions WHERE id = ?", (args.id,)).fetchone()
    if not d:
        raise SystemExit(f"No question #{args.id}.")
    conn.execute("UPDATE decisions SET status = 'done', answer = ?, outcome = ?, answered_at = ? WHERE id = ?",
                 (args.what, args.what, now(), args.id))
    conn.commit()
    apply_grant(d, args.what)
    print(f"Marked #{args.id} answered.")


def cmd_split(args):
    """Split a topic off the main agent chat into its own chat, nested under the agent in the rail."""
    agent = load_agent(args.agent)
    conn = db()
    item_key, item_text = None, ""
    if args.item:
        row = find_item(conn, agent["name"], args.item)
        item_key = row["key"]
        item = item_fields(row)
        item_text = (f"\nIt's about {row['url'] or row['key']} — \"{row['title']}\"."
                     + (f"\nYour last review of it: {row['summary'][:1500]}" if row["summary"] else ""))
    intro = (f"This is a separate chat the user split off from their main conversation with you ({agent['name']}), "
             f"to focus on: {args.title}.{item_text}"
             + (f"\nWhat you two had said so far: {args.context}" if args.context else "")
             + "\nPick up from there. Say in a sentence or two where things stand and what you'd do next, then "
               "wait for the user. Don't post, push or change anything outward-facing unless they say so.")
    sid = str(uuid.uuid4())
    sid_row = conn.execute(
        "INSERT INTO splits (agent, title, item_key, session_id, cwd, intro, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (agent["name"], args.title, item_key, sid, str(WORK), intro, now())).lastrowid
    conn.commit()
    print(f"Opened chat #{sid_row} \"{args.title}\" under {agent['name']}. The user is being switched to it.")


def cmd_splits(args):
    agent = load_agent(args.agent)
    rows = db().execute("SELECT * FROM splits WHERE agent = ? AND closed_at IS NULL ORDER BY id", (agent["name"],))
    out = [{"id": r["id"], "title": r["title"], "pr": short_key(r["item_key"]) if r["item_key"] else None,
            "started": r["created_at"], "open_in_crew": f"split:{r['id']}" in open_threads(),
            **{k: v for k, v in thread_summary({"session_id": r["session_id"], "key": ""}, agent["name"]).items()
               if k != "thread"}} for r in rows]
    print(json.dumps(out, indent=1))


def cmd_open_split(args):
    """Open a split chat in Claude Code: resume it, or start it with its intro on first open."""
    conn = db()
    r = conn.execute("SELECT * FROM splits WHERE id = ?", (args.id,)).fetchone()
    if not r:
        raise SystemExit(f"No chat #{args.id}.")
    agent = load_agent(r["agent"])
    conn.execute("UPDATE splits SET opened = 1 WHERE id = ?", (r["id"],))
    conn.commit()
    allowed, denied = chat_tools(agent)
    Path(r["cwd"]).mkdir(parents=True, exist_ok=True)
    os.chdir(r["cwd"])
    base = identity_args(agent, r["title"]) + claude_args(allowed=allowed, denied=denied, model=agent["model"],
                                                          system=chat_system(agent))
    if session_exists(r["session_id"]):
        os.execvp("claude", ["claude", "--resume", r["session_id"], *base])
    os.execvp("claude", ["claude", "--session-id", r["session_id"], r["intro"], *base])


def cmd_chat(args):
    agent = load_agent(args.agent)
    if args.item:
        chat_item(find_item(db(), agent["name"], args.item))
    else:
        console.print(f"[dim]Opening a chat with {agent['name']} (exit to come back)…[/]")
        open_agent_session(agent)


def cmd_ask(args):
    agent = load_agent(args.agent)
    message = " ".join(args.message)
    with console.status(f"{agent['name']} is thinking…") as status:
        reply = send_message(agent, message, lambda k, t: k == "tool" and status.update(f"{agent['name']}: {t}"))
    console.print(galley.label(agent["name"]))
    console.print(Markdown(reply or "_(no reply)_"))


# ---------------------------------------------------------------- decide

def act(conn, agent, row, d, answer, quiet=False, on_step=None):
    """The agent handles the user's reply to its question: carries out a choice with the permission it grants, or
    answers a question about it and keeps it open. Resumes the session that asked, so it has the whole context."""
    options = "; ".join(f"{i}. {o}" for i, o in enumerate(json.loads(d["options"] or "[]"), 1))
    prompt = (f"You asked the user: {d['question']}\nYour options: {options}\nThe user replied: {answer}\n\n"
              "Decide what their reply is.\n"
              "- A choice or instruction (an option, 'yes', 'do it', 'push all', 'post it but drop the nit'): carry out "
              "exactly that now and nothing beyond it. You have permission for the outward-facing actions it calls "
              "for: push, post, comment, review, message. done = true, keep_open = false.\n"
              "- A question about it, or unclear ('explain', 'why', 'what does that mean'): don't act. Answer in "
              "`reply` plainly, like to a busy colleague, without hashes or ids. done = false, keep_open = true.\n"
              "- Not about this question at all (they're telling or asking you something else): reply to that as "
              "you would in a chat, and leave the question for later. done = false, keep_open = true.\n"
              "- Something that needs long work first ('review it more deeply', 'fix the rest'): don't start it here; "
              "the user is waiting on your reply. Say you're on it in `reply`, describe the work in `follow_up`, "
              "done = false, keep_open = false. crew runs it in the background and they watch it in your feed.\n"
              + (f"If you post, post these drafts exactly as written, with any changes the user asked for:\n{d['drafts']}\n"
                 if d["drafts"] else "")
              + f"If their reply tells you something durable about how they like things, add it to "
                f"{agent['dir']}/KNOWLEDGE.md.\n"
              + (f"This was a standing-rule question: if they agreed, write the rule into {agent['dir']}/"
                 f"{agent['job_file']} under '## Standing rules'.\n" if d["category"] == "standing-rule" else "")
              + (agent["act_instructions"] + "\n" if agent["act_instructions"] else "")
              + "Write `reply` like a short Claude Code chat reply.")
    run = conn.execute("SELECT session_id FROM runs WHERE id = ?", (d["run_id"],)).fetchone()
    sid, cwd = (run["session_id"], str(agent["dir"])) if run and session_exists(run["session_id"]) \
        else ((row["session_id"], row["cwd"]) if row and session_exists(row["session_id"]) else (None, str(agent["dir"])))
    resume = bool(sid)
    sid = sid or str(uuid.uuid4())
    allowed = list(dict.fromkeys(wake_tools(agent)[0] + GRANTABLE + expand_tools(agent["tools"]["act"]["allow"])))
    call = lambda: run_claude(prompt, session_id=sid, resume=resume, cwd=cwd, allowed=allowed,
                              denied=expand_tools(["@never"]), schema=ACT_SCHEMA, model=agent["model"],
                              add_dirs=agent["folders"], protect=protected(agent), on_step=on_step)
    if quiet:
        out, c = call()
    else:
        with console.status(f"{agent['name']} is on it…"):
            out, c = call()
    out["summary"] = out["reply"]
    conn.execute("UPDATE runs SET cost_usd = cost_usd + ? WHERE id = (SELECT MAX(id) FROM runs WHERE agent = ?)",
                 (c, agent["name"]))
    if not out["keep_open"]:
        notify_event(agent, "done", f"{short_key(d['item_key'])}: {'done' if out['done'] else 'not done'} — "
                                    f"{d['question'][:80]}", out["reply"], d["item_key"], conn)
    return out


def say(conn, agent_name, role, text):
    """Add a line to the agent's conversation (its feed)."""
    conn.execute("INSERT INTO chat (agent, role, text, ts) VALUES (?, ?, ?, ?)", (agent_name, role, text, now()))
    conn.commit()


def answer_decision(conn, agent, d, answer, quiet=True, on_step=None):
    """Hand the user's reply to the agent. A choice gets carried out; a question about it gets answered and the
    question stays open. The exchange shows in the agent's feed like a conversation. Returns the act result."""
    row = conn.execute("SELECT * FROM items WHERE agent = ? AND key = ?", (d["agent"], d["item_key"])).fetchone()
    say(conn, d["agent"], "user", f"> {d['question']}\n{answer}")
    conn.execute("UPDATE decisions SET status='answered', answer=?, answered_at=? WHERE id=?", (answer, now(), d["id"]))
    conn.commit()
    try:
        out = act(conn, agent, row, d, answer, quiet=quiet, on_step=on_step)
    except Exception as e:
        conn.execute("UPDATE decisions SET status='pending', answer=NULL, answered_at=NULL WHERE id=?", (d["id"],))
        conn.commit()
        say(conn, d["agent"], "agent", "Stopped. The question is still open." if str(e) == "stopped" else
            f"I couldn't do that, so the question is still open: {str(e)[:300]}")
        notify_event(agent, "error", f"{short_key(d['item_key'])}: could not carry out your answer", str(e),
                     d["item_key"], conn)
        raise
    if out["keep_open"]:
        conn.execute("UPDATE decisions SET status='pending', answer=NULL, answered_at=NULL WHERE id=?", (d["id"],))
    else:
        apply_grant(d, answer)
        conn.execute("UPDATE decisions SET status=?, outcome=? WHERE id=?",
                     ("done" if out["done"] else "answered", out["reply"], d["id"]))
    conn.commit()
    say(conn, d["agent"], "agent", out["reply"])
    if out.get("follow_up", "").strip():
        start_background_run(d["agent"], trigger="your answer", note=out["follow_up"].strip())
    return out


def cmd_answer(args):
    """Answer a waiting question in your own words; the agent carries it out (or answers you, if you asked something)."""
    conn = db()
    d = conn.execute("SELECT * FROM decisions WHERE id = ? AND status = 'pending'", (args.id,)).fetchone()
    if not d:
        raise SystemExit(f"Question #{args.id} isn't waiting on an answer.")
    out = answer_decision(conn, load_agent(d["agent"]), d, " ".join(args.answer))
    print(out["reply"])


def cmd_decide(args):
    conn = db()
    q, params = "SELECT * FROM decisions WHERE status = 'pending'", []
    if args.agent:
        q += " AND agent = ?"
        params.append(load_agent(args.agent)["name"])
        if args.item:
            q += " AND item_key = ?"
            params.append(find_item(conn, args.agent, args.item)["key"])
    decisions = conn.execute(q + " ORDER BY agent, item_key, id", params).fetchall()
    if not decisions:
        console.print(Text("Nothing is waiting on you here.", style=GRAPHITE))
        return
    shown, agents = set(), {}
    for n, d in enumerate(decisions, 1):
        agent = agents.setdefault(d["agent"], load_agent(d["agent"]))
        row = conn.execute("SELECT * FROM items WHERE agent = ? AND key = ?", (d["agent"], d["item_key"])).fetchone()
        if (d["agent"], d["item_key"]) not in shown:  # always explain the item and approach before asking
            show_item(agent, row)
            shown.add((d["agent"], d["item_key"]))
            console.rule(style=RULE)
        while True:
            options = json.loads(d["options"])
            console.print()
            tag = Text()
            tag.append("▍ ", style=PENCIL)
            tag.append(f"QUERY {n} OF {len(decisions)}", style=f"bold {PENCIL}")
            console.print(tag)
            q_line = Text("▍ ", style=PENCIL)
            q_line.append(d["question"], style=f"bold {PAPER}")
            console.print(q_line)
            console.print()
            console.print(Markdown(d["context"]))
            if d["drafts"]:
                console.print()
                console.print(galley.label("Drafted comments (not posted)"))
                console.print(Markdown(d["drafts"]))
            console.print()
            for i, o in enumerate(options, 1):
                opt = Text(f"  {i}  ", style=f"bold {PAPER}")
                opt.append(o, style=PAPER)
                console.print(opt)
            console.print()
            rec = Text("  Suggested  ", style=GRAPHITE)
            rec.append(d["recommendation"], style=PAPER)
            console.print(rec)
            console.print()
            keys = Text("  ")
            for k, word in (("1-9", "answer"), ("t", "write your own"), ("c", "ask about it"), ("s", "skip"),
                            ("x", "dismiss"), ("q", "stop")):
                keys.append(k, style=PAPER)
                keys.append(f" {word}    ", style=GRAPHITE)
            console.print(keys)
            choice = Prompt.ask(Text("  your call", style=PAPER)).strip().lower()
            if choice == "q":
                return
            if choice == "s":
                break
            if choice == "c":
                chat_item(row)
                continue
            if choice == "x":
                conn.execute("UPDATE decisions SET status='dismissed', answered_at=? WHERE id=?", (now(), d["id"]))
                conn.commit()
                console.print(Text("  dismissed", style=f"strike {GRAPHITE}"))
                break
            if choice == "t":
                answer = Prompt.ask(Text("  your answer", style=PAPER))
            elif choice.isdigit() and 1 <= int(choice) <= len(options):
                answer = options[int(choice) - 1]
                note = Prompt.ask(Text("  anything to add (enter to skip)", style=GRAPHITE), default="", show_default=False)
                answer += f" — note: {note}" if note else ""
            else:
                continue
            try:
                out = answer_decision(conn, agent, d, answer, quiet=False)
                done = Text()
                done.append("  answered  ", style=DONE)
                done.append(d["question"], style=f"strike {GRAPHITE}")
                console.print(done)
                console.print(Markdown(out["summary"]))
            except Exception as e:
                console.print(Text(f"  Couldn't carry that out: {e}", style=PENCIL))
            break


# ---------------------------------------------------------------- manage agents

GENERATE_SCHEMA = {
    "type": "object",
    "properties": {
        "description": {"type": "string", "description": "One line: what the agent does."},
        "prompt": {"type": "string", "description": "prompt.md body. Uses {field} placeholders from the scanned item (and {me} = the user's GitHub login)."},
        "scan_script": {"type": "string", "description": "Python 3 script (stdlib only, may shell out to gh) printing a JSON array of items, each with key, title and optionally url, updated_at and other fields. Empty string if the agent has no list of items and should just run once per schedule tick."},
        "columns": {"type": "array", "items": {"type": "string"}, "description": "Item fields worth a dashboard column."},
        "verdicts": {"type": "array", "items": {"type": "string"}, "minItems": 2},
        "item_label": {"type": "string"},
        "summary_label": {"type": "string"},
        "approach_label": {"type": "string"},
        "draft_label": {"type": "string"},
        "interval": {"type": "string", "description": "e.g. 30m, 2h, 1d"},
    },
    "required": ["description", "prompt", "scan_script", "columns", "verdicts", "item_label", "summary_label",
                 "approach_label", "draft_label", "interval"],
}


def create_agent(name, responsibilities="", *, interval=None, folders=(), skills=(), generate=True, description=None):
    """Create ~/.crew/agents/<name>. With generate, Claude turns the responsibilities into a prompt, scan and labels."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
        raise ValueError("Agent names are lowercase letters, digits and dashes.")
    d = AGENTS_DIR / name
    if d.exists():
        raise ValueError(f"Agent {name} already exists.")
    cfg = {"description": description or responsibilities.strip().split("\n")[0][:120], "interval": interval or "1h",
           "scan": None, "skills": list(skills), "folders": [str(Path(f).expanduser()) for f in folders], "settings": {}}
    prompt = responsibilities.strip() or ("Describe here what this agent should do on each run.\n"
                                          "Item fields from the scan are available as {placeholders}, e.g. {title}.")
    scan_script = ""
    if generate and responsibilities.strip():
        example = AGENTS_DIR / "pr-review"
        ex = ""
        if example.exists():
            ex = "\n\nA working example agent (pr-review):\n" + "\n".join(
                f"--- {p.name} ---\n{p.read_text()}" for p in sorted(example.iterdir())
                if p.is_file() and p.suffix in (".json", ".md", ".py"))
        folders_note = f"\nFolders it can read: {', '.join(cfg['folders'])}." if cfg["folders"] else ""
        gen_prompt = (f"Design a crew agent named '{name}' with these responsibilities:\n\n{responsibilities}\n{folders_note}\n\n"
                      "crew runs the agent on a schedule with headless Claude Code, read-only. A scan script lists work "
                      "items; for each changed item the agent's prompt runs and returns a summary, approach, verdict, "
                      "findings, a draft (anything to post, not sent), an optional one-line alert, and decisions for the "
                      "human. Outward-facing actions only happen after the human answers a decision." + ex)
        g, _ = run_claude(gen_prompt, session_id=None, resume=False, cwd=str(HOME), allowed=["Read", "Glob", "Grep"],
                          denied=[], schema=GENERATE_SCHEMA, model=None, add_dirs=cfg["folders"])
        cfg.update(description=description or g["description"], interval=interval or g["interval"],
                   columns=g["columns"], verdicts=g["verdicts"],
                   labels={"item": g["item_label"], "summary": g["summary_label"], "approach": g["approach_label"],
                           "draft": g["draft_label"]})
        prompt, scan_script = g["prompt"], g["scan_script"].strip()
    d.mkdir(parents=True)
    (d / "work").mkdir()
    if scan_script:
        (d / "scan.py").write_text(scan_script + "\n")
        cfg["scan"] = "python3 scan.py"
    (d / "agent.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (d / "prompt.md").write_text(prompt + "\n")
    return load_agent(name)


def create_brief_agent(name, brief, *, budget_usd=None, folders=()):
    """Overtime-style: a name and a brief in plain words. On its first wake the agent turns the brief into its own
    AGENT.md, then gets to work."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", name or ""):
        raise ValueError("Agent names are lowercase letters, digits and dashes.")
    d = AGENTS_DIR / name
    if d.exists():
        raise ValueError(f"Agent {name} already exists.")
    for sub in ("notes", "work", ".claude/skills"):
        (d / sub).mkdir(parents=True)
    cfg = {"mode": "agentic", "description": brief.strip().split("\n")[0][:120], "labels": {"item": "Task"},
           "folders": [str(Path(f).expanduser()) for f in folders]}
    if budget_usd:
        cfg["budget_usd"] = budget_usd
    (d / "agent.json").write_text(json.dumps(cfg, indent=2) + "\n")
    (d / "AGENT.md").write_text(f"# {name}\n\n## Brief from @{{me}}\n\n{brief.strip()}\n")
    agent = load_agent(name)
    set_awake(agent, True)
    start_background_run(name, trigger="you, just created it")
    return agent


def cmd_new(args):
    if not args.name:
        return new_agent_wizard()
    if not args.describe and not args.items:
        brief = args.brief or ask_brief()
        try:
            agent = create_brief_agent(args.name, brief, budget_usd=args.budget, folders=args.folder or [])
        except ValueError as e:
            raise SystemExit(str(e))
        console.print(f"Created [bold]{agent['name']}[/] in {agent['dir']}. It's awake: it writes its own job "
                      "description from your brief and gets to work.")
        console.print(Text(f"Talk to it: crew chat {agent['name']}  ·  stop it: s in the rail  ·  its budget: "
                           f"${budget(agent):g} a day", style=GRAPHITE))
        return
    try:
        with console.status("Designing the agent with Claude…" if args.describe else "Creating…"):
            agent = create_agent(args.name, args.describe or "", interval=args.interval, folders=args.folder or [],
                                 generate=bool(args.describe), description=args.description)
    except ValueError as e:
        raise SystemExit(str(e))
    console.print(f"Created agent [bold]{agent['name']}[/] in {agent['dir']}")
    for p in sorted(agent["dir"].iterdir()):
        console.print(f"  {p.name}")
    console.print(f"[dim]Next: crew run {agent['name']}  ·  crew schedule {agent['name']} {agent['interval']}  ·  "
                  f"crew ask {agent['name']} \"…\"[/]")


def start_background_run(name, item=None, force=False, trigger=None, note=None):
    """Kick off `crew run` detached, so it keeps going if the UI closes."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "run", name]
    cmd += ["--trigger", trigger] if trigger else []
    cmd += ["--note", note] if note else []
    cmd += ["--item", item] if item else []
    cmd += ["--force"] if force else []
    LOGS.mkdir(parents=True, exist_ok=True)
    log = open(LOGS / f"{name}.manual.log", "a")
    subprocess.Popen(cmd, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)


def remove_item(agent_name, key):
    """Hide an item for good: it stays hidden on later scans, and its pending decisions are dropped."""
    conn = db()
    conn.execute("UPDATE decisions SET status = 'closed' WHERE agent = ? AND item_key = ? AND status = 'pending'",
                 (agent_name, key))
    conn.execute("UPDATE items SET closed_at = ?, closed_reason = 'removed', picked = '0' WHERE agent = ? AND key = ?",
                 (now(), agent_name, key))
    conn.commit()


def cmd_restore(args):
    agent = load_agent(args.agent)
    conn = db()
    row = find_item(conn, agent["name"], args.item)
    conn.execute("UPDATE items SET closed_at = NULL, closed_reason = NULL WHERE agent = ? AND key = ?",
                 (agent["name"], row["key"]))
    conn.commit()
    console.print(f"{short_key(row['key'])} is back in {agent['name']}'s list.")


def set_picked(agent_name, keys, picked=True):
    conn = db()
    for k in keys:
        conn.execute("UPDATE items SET picked = ? WHERE agent = ? AND key = ?", ("1" if picked else "0", agent_name, k))
    conn.commit()


def refresh_list(agent):
    """Scan without working anything, so the pick list is current."""
    conn = db()
    found = scan(agent)
    was_open = {r["key"]: r for r in conn.execute(
        "SELECT * FROM items WHERE agent = ? AND in_queue = 1 AND closed_at IS NULL", (agent["name"],))}
    conn.execute("UPDATE items SET in_queue = 0 WHERE agent = ?", (agent["name"],))
    for it in found:
        conn.execute(
            """INSERT INTO items (agent, key, title, url, data, version, in_queue) VALUES (?, ?, ?, ?, ?, ?, 1)
               ON CONFLICT(agent, key) DO UPDATE SET title=excluded.title, url=excluded.url, data=excluded.data,
               version=excluded.version, in_queue=1,
                   closed_at=CASE WHEN closed_reason = 'removed' THEN closed_at END,
                   closed_reason=CASE WHEN closed_reason = 'removed' THEN closed_reason END""",
            (agent["name"], it["key"], it["title"], it.get("url"), json.dumps(it), item_version(it)))
    conn.commit()
    close_dropped(agent, conn, [r for k, r in was_open.items() if k not in {i["key"] for i in found}])


def cmd_pick(args):
    """Choose which items an agent works on."""
    agent = load_agent(args.agent)
    if args.add or args.remove:
        conn = db()
        for refs, flag in ((args.add or [], True), (args.remove or [], False)):
            for ref in refs:
                set_picked(agent["name"], [find_item(conn, agent["name"], ref)["key"]], flag)
        print(f"Picked: +{len(args.add or [])} -{len(args.remove or [])}. Run `crew run {agent['name']}` to review them.")
        return
    with console.status(f"Fetching {agent['name']}'s list…"):
        refresh_list(agent)
    conn = db()
    rows = conn.execute("""SELECT * FROM items WHERE agent = ? AND in_queue = 1 AND closed_at IS NULL
                           ORDER BY picked = '1' DESC, version DESC""", (agent["name"],)).fetchall()
    if not rows:
        console.print("Nothing open.")
        return
    t = Table(title=f"{agent['name']} — pick what it should work on", expand=True, title_justify="left")
    for c in ("#", "", agent["labels"]["item"], *[c.title() for c in agent["columns"]], "Title", "Updated"):
        t.add_column(c, no_wrap=c != "Title", overflow="ellipsis", ratio=1 if c == "Title" else None)
    for i, r in enumerate(rows, 1):
        data = json.loads(r["data"] or "{}")
        t.add_row(str(i), f"[{PAPER}]picked[/]" if r["picked"] == "1" else "", short_key(r["key"]),
                  *[str(data.get(c, "")) for c in agent["columns"]], r["title"] or "", ago(data.get("updated_at")))
    console.print(t)
    raw = Prompt.ask("Numbers to toggle [dim](e.g. 1 3 5-7, 'all', 'none', Enter to keep)[/]", default="", show_default=False)
    picked = {r["key"] for r in rows if r["picked"] == "1"}
    if raw.strip() == "all":
        picked = {r["key"] for r in rows}
    elif raw.strip() == "none":
        picked = set()
    else:
        for part in raw.replace(",", " ").split():
            a, _, b = part.partition("-")
            for n in range(int(a), int(b or a) + 1):
                if 1 <= n <= len(rows):
                    picked ^= {rows[n - 1]["key"]}
    for r in rows:
        set_picked(agent["name"], [r["key"]], r["key"] in picked)
    console.print(f"[green]{len(picked)}[/] picked. Run them now with [bold]crew run {agent['name']}[/].")


def cmd_folders(args):
    agent = load_agent(args.agent)
    folders = list(agent["folders"])
    for f in args.paths:
        f = str(Path(f).expanduser().resolve())
        if args.action == "add" and f not in folders:
            if not Path(f).is_dir():
                raise SystemExit(f"Not a folder: {f}")
            folders.append(f)
        elif args.action == "remove" and f in folders:
            folders.remove(f)
    if args.action != "list":
        save_agent_field(agent["name"], "folders", folders)
    console.print(f"{agent['name']} can read: " + (", ".join(folders) or "[dim]only its own folder[/]"))


def unread_count(conn, agent=None):
    sql = "SELECT COUNT(*) FROM notifications WHERE read = 0" + (" AND agent = ?" if agent else "")
    return conn.execute(sql, (agent,) if agent else ()).fetchone()[0]


def cmd_inbox(args):
    conn = db()
    name = args.agent and load_agent(args.agent)["name"]
    if args.clear:
        conn.execute("UPDATE notifications SET read = 1" + (" WHERE agent = ?" if name else ""), (name,) if name else ())
        conn.commit()
        console.print("Inbox marked read.")
        return
    sql = "SELECT * FROM notifications WHERE 1=1" + (" AND agent = ?" if name else "") + ("" if args.all else " AND read = 0")
    rows = conn.execute(sql + " ORDER BY id DESC LIMIT 50", (name,) if name else ()).fetchall()
    if not rows:
        console.print("[green]Inbox empty.[/]" + ("" if args.all else " [dim](--all shows read ones)[/]"))
        return
    icon = {"decision": f"[{PENCIL}]query[/]", "alert": f"[{PENCIL}]alert[/]", "error": f"[{PENCIL}]failed[/]",
            "done": f"[{DONE}]done[/]", "closed": f"[{DONE}]closed[/]"}
    t = Table(expand=True, title=f"Inbox ({unread_count(conn, name)} unread)", title_justify="left")
    for c in ("", "When", "Agent", "Event"):
        t.add_column(c, overflow="fold", no_wrap=c != "Event")
    for r in rows:
        t.add_row(icon.get(r["kind"], "·"), ago(r["ts"]), r["agent"],
                  ("" if r["read"] else "[bold]") + r["title"] + ("" if r["read"] else "[/]"))
    console.print(t)
    console.print("[dim]crew inbox --clear marks them read · crew decide answers decisions[/]")


def cmd_ui(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import crew_tmux
    crew_tmux.launch()


def cmd_nav(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from crew_nav import NavApp
    NavApp().run()


def cmd_sidebar(args):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import crew_tmux
    crew_tmux.sidebar(args.action)


def cmd_thread(args):
    """Open an item's own Claude Code session (starting one if the agent hasn't worked it yet)."""
    agent = load_agent(args.agent)
    conn = db()
    row = find_item(conn, agent["name"], args.item)
    if session_exists(row["session_id"]):
        os.chdir(row["cwd"])
        os.execvp("claude", ["claude", "--resume", row["session_id"], *claude_args()])
    # never started, or opened and closed before a first message: start it fresh
    item = item_fields(row)
    sid, cwd = str(uuid.uuid4()), row["cwd"] or item.get("cwd") or str(WORK)
    Path(cwd).mkdir(parents=True, exist_ok=True)
    conn.execute("UPDATE items SET session_id = ?, cwd = ? WHERE agent = ? AND key = ?", (sid, cwd, agent["name"], row["key"]))
    conn.commit()
    fields = _Blank(item)
    if "{me}" in agent["prompt"]:
        fields["me"] = whoami()
    intro = (agent["prompt"].format_map(fields).strip() +
             f"\n\nI just opened this {agent['labels']['item']} in crew. Walk me through it: what it is, how you'd approach "
             "it, what you found, and anything that needs my decision. Don't post, push or change anything outward-facing "
             "unless I say so.")
    os.chdir(cwd)
    os.execvp("claude", ["claude", "--session-id", sid, intro, *claude_args()])


def cmd_feed(args):
    """One agent's conversation: wake reports, messages, answers, and its waiting questions (number keys answer)."""
    from crew_feed import FeedApp
    FeedApp(load_agent(args.agent)["name"]).run()


def cmd_home(args):
    """The crew home view: open queries, then what changed since you last looked."""
    from crew_home import HomeApp
    HomeApp().run()


def ask_brief():
    console.print("What's it for? Tell it in plain words, like briefing a colleague. [dim](finish with an empty line)[/]")
    lines = []
    while True:
        line = input()
        if not line.strip():
            break
        lines.append(line)
    if not lines:
        raise SystemExit("An agent needs a brief.")
    return "\n".join(lines)


def new_agent_wizard():
    console.rule("[bold]New agent")
    name = Prompt.ask("Name [dim](lowercase, dashes)[/]").strip()
    brief = ask_brief()
    spend = Prompt.ask("Daily budget in $", default=f"{config()['daily_budget_usd']:g}")
    try:
        agent = create_brief_agent(name, brief, budget_usd=float(spend) if spend.strip() else None)
    except ValueError as e:
        raise SystemExit(str(e))
    console.print(f"\n[green]Created [bold]{agent['name']}[/][/]. It's awake: it writes its own job description from "
                  "your brief, then gets to work and checks in only when something is yours to decide.\n")
    console.print(Text("Select it in the rail: enter to talk to it, r to wake it now, s to stop it, e to edit its job.",
                       style=GRAPHITE))


def cmd_agents(args):
    for n in agent_names():
        a = load_agent(n)
        console.print(f"[bold]{n}[/]  {a['description']}  [dim]({a['dir']})[/]")


def cmd_remove(args):
    agent = load_agent(args.agent)
    if not Confirm.ask(f"Delete agent {agent['name']} ({agent['dir']}) and its history?"):
        return
    unschedule(agent["name"])
    if agent.get("mode") == "agentic":
        agent_state(agent, awake=False)
        reconcile_watches(agent)
    conn = db()
    for t, col in (("runs", "agent"), ("items", "agent"), ("decisions", "agent")):
        conn.execute(f"DELETE FROM {t} WHERE {col} = ?", (agent["name"],))
    conn.commit()
    shutil.rmtree(agent["dir"])
    console.print(f"Removed {agent['name']}.")


def cmd_skills(args):
    agent = load_agent(args.agent)
    skills_dir = Path.home() / ".claude/skills"
    if args.action == "list":
        console.print(f"{agent['name']} uses: " + (", ".join(agent["skills"]) or "[dim]none[/]"))
        avail = sorted(p.name for p in skills_dir.iterdir() if (p / "SKILL.md").exists()) if skills_dir.exists() else []
        console.print("[dim]Available in ~/.claude/skills: " + ", ".join(avail) + "[/]")
        return
    skills = list(agent["skills"])
    for s in args.names:
        if args.action == "add" and s not in skills:
            if not (skills_dir / s / "SKILL.md").exists():
                console.print(f"[yellow]Note:[/] ~/.claude/skills/{s} not found — fine if it comes from a plugin or project.")
            skills.append(s)
        elif args.action == "remove" and s in skills:
            skills.remove(s)
    save_agent_field(agent["name"], "skills", skills)
    console.print(f"{agent['name']} uses: " + (", ".join(skills) or "none"))


def cmd_edit(args):
    agent = load_agent(args.agent)
    path = agent["dir"] / (agent["job_file"] if args.prompt else "agent.json")
    subprocess.run([os.environ.get("EDITOR", "nano"), str(path)])


def cmd_runs(args):
    console.print(runs_table(db(), args.agent and load_agent(args.agent)["name"], args.limit,
                             f"Runs{' · ' + args.agent if args.agent else ''}"))


def cmd_statusline(args):
    try:
        conn = db()
        n = conn.execute("SELECT COUNT(*) FROM decisions WHERE status = 'pending'").fetchone()[0]
        running = conn.execute("SELECT agent, current FROM runs WHERE status = 'running' ORDER BY id DESC").fetchall()
        busy = "    ".join(f"{r['agent']} reviewing {short_key(r['current'] or '…').split('#')[-1]}" for r in running)
        queries = f"{n} open {'query' if n == 1 else 'queries'}" if n else "nothing waiting"
        print("    ".join(x for x in (busy, queries) if x))
    except Exception:
        print("")


def repoint_launchd():
    """After an install or upgrade moves crew, point its launchd jobs (the wake tick, schedules) at this copy."""
    here = [sys.executable, str(Path(__file__).resolve())]
    for plist in LAUNCH_AGENTS.glob("io.crew.*.plist"):
        try:
            data = plistlib.loads(plist.read_bytes())
        except Exception:
            continue
        if data.get("ProgramArguments", [])[:2] == here:
            continue
        data["ProgramArguments"] = here + data.get("ProgramArguments", [])[2:]
        data.setdefault("EnvironmentVariables", {})["PATH"] = os.environ.get("PATH", "")
        plist.write_bytes(plistlib.dumps(data))
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{data['Label']}"], capture_output=True)
        subprocess.run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(plist)], capture_output=True)


def main():
    argv = sys.argv[1:]
    remember_terminal()
    if argv[:1] not in (["tick"], ["watch-run"], ["agent-status"], ["statusline"]):
        repoint_launchd()
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        argv = ["ui"]
    if argv and argv[0] in agent_names():  # `crew pr-review` = that agent's dashboard
        argv = ["status", *argv]
    p = argparse.ArgumentParser(prog="crew", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd")
    s = sub.add_parser("status", help="overview of all agents, or one agent's dashboard (default)")
    s.add_argument("agent", nargs="?")
    sub.add_parser("watch", help="live dashboard, refreshes every 5s").add_argument("agent", nargs="?")
    sub.add_parser("agents", help="list agents")
    n = sub.add_parser("new", help="create an agent")
    n.add_argument("name", nargs="?")
    n.add_argument("brief", nargs="?", help="what it's for, in plain words; it writes its own job description from this")
    n.add_argument("--budget", type=float, help="daily budget in $ (default from ~/.crew/config.json)")
    n.add_argument("--items", action="store_true", help="old-style item agent: Claude drafts a prompt and scan script")
    n.add_argument("--describe", help="with --items: what it should do")
    n.add_argument("--description", help="one-line description")
    n.add_argument("--interval")
    n.add_argument("--folder", action="append", help="folder the agent may read (repeatable)")
    sub.add_parser("ui", help="the crew workspace: sidebar + live Claude Code panes (default with no args)")
    sub.add_parser("nav", help=argparse.SUPPRESS)
    sb = sub.add_parser("sidebar", help=argparse.SUPPRESS)
    sb.add_argument("action", choices=["fit", "grow", "shrink", "reset", "save"])
    sub.add_parser("open-feed", help=argparse.SUPPRESS).add_argument("agent")
    an = sub.add_parser("answer", help="answer a waiting question in your own words; the agent carries it out")
    an.add_argument("id", type=int)
    an.add_argument("answer", nargs="+")
    sub.add_parser("feed", help="one agent's conversation: reports, messages and its questions").add_argument("agent")
    sub.add_parser("home", help="live home view: agents, decisions waiting on you, inbox")
    tl = sub.add_parser("threads", help="status of every item an agent tracks, with its own chat thread")
    tl.add_argument("agent")
    tl.add_argument("item", nargs="?")
    tl.add_argument("--full", action="store_true", help="whole conversation, untruncated")
    tl.add_argument("--json", action="store_true")
    th = sub.add_parser("thread", help="open an item's own Claude Code session")
    th.add_argument("agent")
    th.add_argument("item")
    a = sub.add_parser("ask", help="message an agent and print its reply")
    a.add_argument("agent")
    a.add_argument("message", nargs="+")
    ib = sub.add_parser("inbox", help="notifications from your agents")
    ib.add_argument("agent", nargs="?")
    ib.add_argument("--all", action="store_true")
    ib.add_argument("--clear", action="store_true")
    rs = sub.add_parser("restore", help="bring back an item you removed from the sidebar")
    rs.add_argument("agent")
    rs.add_argument("item")
    pk = sub.add_parser("pick", help="choose which items (PRs) an agent works on")
    pk.add_argument("agent")
    pk.add_argument("--add", nargs="+", help="PR numbers to pick")
    pk.add_argument("--remove", nargs="+", help="PR numbers to unpick")
    ak = sub.add_parser("asks", help="what an agent is waiting on you for")
    ak.add_argument("agent")
    aw = sub.add_parser("answered", help=argparse.SUPPRESS)
    aw.add_argument("id", type=int)
    aw.add_argument("what")
    sp = sub.add_parser("split", help="split a topic off an agent's main chat into its own chat")
    sp.add_argument("agent")
    sp.add_argument("title")
    sp.add_argument("--item", help="the PR it's about")
    sp.add_argument("--context", help="what was said so far")
    sl = sub.add_parser("splits", help="an agent's split-off chats")
    sl.add_argument("agent")
    so = sub.add_parser("open-split", help=argparse.SUPPRESS)
    so.add_argument("id", type=int)
    fo = sub.add_parser("folders", help="list/add/remove folders an agent can read")
    fo.add_argument("agent")
    fo.add_argument("action", choices=["list", "add", "remove"])
    fo.add_argument("paths", nargs="*")
    sub.add_parser("remove", help="delete an agent and its history").add_argument("agent")
    r = sub.add_parser("run", help="run one agent (or all) now")
    r.add_argument("agent", nargs="?")
    r.add_argument("--item", help="only this item (key, number or url)")
    r.add_argument("--force", action="store_true", help="re-work even if unchanged")
    r.add_argument("--trigger", default="manual", help=argparse.SUPPRESS)
    r.add_argument("--note", help="what you want it to look at this time")
    r.add_argument("--background", action="store_true", help="start it and return right away")
    sub.add_parser("tick", help=argparse.SUPPRESS)
    wr = sub.add_parser("watch-run", help=argparse.SUPPRESS)
    wr.add_argument("agent")
    wr.add_argument("name")
    wr.add_argument("command")
    sub.add_parser("agent-status", help=argparse.SUPPRESS).add_argument("agent")
    sub.add_parser("brief", help="today's morning AI brief").add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("learn", help="teach an agent how you review / address comments, from your GitHub history").add_argument("agent")
    sh = sub.add_parser("show", help="explanation, approach, findings and decisions for one item")
    sh.add_argument("agent")
    sh.add_argument("item")
    dc = sub.add_parser("decide", help="walk through pending decisions (all agents, or one)")
    dc.add_argument("agent", nargs="?")
    dc.add_argument("item", nargs="?")
    ch = sub.add_parser("chat", help="chat with an agent, or inside one item's session")
    ch.add_argument("agent")
    ch.add_argument("item", nargs="?")
    ru = sub.add_parser("runs", help="run history")
    ru.add_argument("agent", nargs="?")
    ru.add_argument("--limit", type=int, default=20)
    sc = sub.add_parser("schedule", help="turn an agent's schedule on (e.g. 30m, 2h) or off")
    sc.add_argument("agent")
    sc.add_argument("interval", nargs="?")
    k = sub.add_parser("skills", help="list/add/remove skills an agent applies")
    k.add_argument("agent")
    k.add_argument("action", choices=["list", "add", "remove"])
    k.add_argument("names", nargs="*")
    e = sub.add_parser("edit", help="open an agent's agent.json (or --prompt for its AGENT.md) in $EDITOR")
    e.add_argument("agent")
    e.add_argument("--prompt", action="store_true")
    sub.add_parser("statusline", help="one-line summary for a Claude Code status line")
    args = p.parse_args(argv)
    if args.cmd is None:
        args.agent = None
    {"status": cmd_status, "watch": cmd_watch, "agents": cmd_agents, "new": cmd_new, "remove": cmd_remove,
     "run": cmd_run, "show": cmd_show, "decide": cmd_decide, "chat": cmd_chat, "runs": cmd_runs,
     "schedule": cmd_schedule, "skills": cmd_skills, "edit": cmd_edit, "statusline": cmd_statusline,
     "ui": cmd_ui, "nav": cmd_nav, "sidebar": cmd_sidebar, "home": cmd_home, "feed": cmd_feed, "open-feed": cmd_open_feed, "answer": cmd_answer, "thread": cmd_thread, "threads": cmd_threads, "ask": cmd_ask, "pick": cmd_pick, "tick": cmd_tick, "watch-run": cmd_watch_run, "agent-status": cmd_agent_status, "brief": cmd_brief, "learn": cmd_learn, "split": cmd_split, "asks": cmd_asks, "answered": cmd_answered, "splits": cmd_splits, "open-split": cmd_open_split, "restore": cmd_restore, "inbox": cmd_inbox, "folders": cmd_folders,
     None: cmd_status}[args.cmd](args)


if __name__ == "__main__":
    main()
