"""The crew workspace on its own tmux server: a sidebar on the left, live sessions on the right.

Every agent chat / item thread / decision runs in its own pane that keeps living in a hidden window;
showing one swaps it into the right-hand slot, so switching is instant and nothing restarts.
"""
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("CREW_HOME", Path.home() / ".crew"))
CONF = HOME / "tmux.conf"
SESSION = "crew"
CREW = str(Path(__file__).resolve().parent / "crew.py")
SIDEBAR_WIDTH = 42  # default; your own width is remembered in ~/.crew/sidebar_width
WIDTH_FILE = HOME / "sidebar_width"

TMUX_CONF = """
set -g mouse on
set -s escape-time 0
set -g focus-events on
set -s extended-keys on
set -as terminal-features 'xterm*:extkeys'
set -g default-terminal "tmux-256color"
set -as terminal-overrides ',*:Tc'
set -g history-limit 50000
set -g remain-on-exit on
set -g remain-on-exit-format " finished — pick something in the sidebar "
set -g status-position bottom
set -g status-style "bg=#1b1e24,fg=#8b92a5"
set -g status-left "#[fg=#e8e6e1,bold]  crew  "
set -g status-left-length 20
set -g status-right "#(%(crew)s statusline)    alt ←→ move    alt+z focus    alt+q hide  "
set -g status-right-length 140
set -g status-interval 5
set -g window-status-format ""
set -g window-status-current-format ""
set -g window-style "bg=#16181d"
set -g window-active-style "bg=#16181d"
set -g pane-border-status top
set -g pane-border-lines single
set -g pane-border-format "  #{?#{@crew_title},#{@crew_title},#{pane_title}}  "
set -g pane-border-style "fg=#2a2d35,bg=#16181d"
set -g pane-active-border-style "fg=#8b92a5,bg=#16181d"
set -g message-style "bg=#262a33,fg=#e8e6e1"
set -g mode-style "bg=#262a33,fg=#e8e6e1"
bind -n M-Left select-pane -L
bind -n M-Right select-pane -R
bind -n M-h select-pane -L
bind -n M-l select-pane -R
bind -n M-z resize-pane -Z
bind -n M-q detach-client
bind -n M-S-Left run-shell -b "%(crew)s sidebar shrink"
bind -n M-S-Right run-shell -b "%(crew)s sidebar grow"
bind -n M-= run-shell -b "%(crew)s sidebar reset"
set-hook -gw window-resized 'run-shell -b "%(crew)s sidebar fit"'
set-hook -g client-resized 'run-shell -b "%(crew)s sidebar fit"'
set-hook -g after-resize-pane 'run-shell -b "%(crew)s sidebar save"'
"""


def tmux(*args, check=False):
    r = subprocess.run(["tmux", "-L", "crew", "-f", str(CONF), *args], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"tmux {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def crew_cmd(*args):
    return " ".join(shlex.quote(a) for a in [sys.executable, CREW, *args])


def nav_pane():
    return tmux("show-options", "-t", SESSION, "-v", "@nav")


def display_pane():
    nav = nav_pane()
    for pid in tmux("list-panes", "-t", f"{SESSION}:main", "-F", "#{pane_id}").splitlines():
        if pid != nav:
            return pid
    return None


def panes():
    """crew_key -> (pane_id, dead)"""
    out = {}
    for line in tmux("list-panes", "-s", "-t", SESSION, "-F", "#{pane_id}\t#{@crew_key}\t#{pane_dead}").splitlines():
        pid, key, dead = (line.split("\t") + ["", ""])[:3]
        if key:
            out[key] = (pid, dead == "1")
    return out


def shown_key():
    d = display_pane()
    return tmux("display-message", "-p", "-t", d, "#{@crew_key}") if d else None


def show(key, command, title, cwd=None, focus=True, restart=False):
    """Bring the session `key` into the right-hand slot, starting it (or restarting a finished one) if needed."""
    existing = panes().get(key)
    if existing and (existing[1] or restart):
        tmux("respawn-pane", "-k", "-t", existing[0], "-c", str(cwd or Path.home()), command)
        pid = existing[0]
    elif existing:
        pid = existing[0]
    else:
        pid = tmux("new-window", "-d", "-t", SESSION, "-c", str(cwd or Path.home()), "-P", "-F", "#{pane_id}", command,
                   check=True)
        tmux("set-option", "-p", "-t", pid, "@crew_key", key)
    tmux("set-option", "-p", "-t", pid, "@crew_title", title)
    target = display_pane()
    if target and target != pid:
        tmux("swap-pane", "-d", "-s", pid, "-t", target)
    if focus:
        tmux("select-pane", "-t", pid)


def close(key):
    """Kill a session's pane (e.g. a merged PR's thread); the home view takes the slot if it was showing."""
    p = panes().get(key)
    if not p:
        return
    if shown_key() == key:
        show("home", crew_cmd("home"), "home", focus=False)
    tmux("kill-pane", "-t", p[0])


def sidebar_width():
    try:
        return max(24, min(160, int(WIDTH_FILE.read_text().strip())))
    except (OSError, ValueError):
        return SIDEBAR_WIDTH


def sidebar(action):
    """fit: hold the sidebar at its width after the window resizes · grow/shrink/reset · save: remember a dragged width."""
    nav = nav_pane()
    if not nav:
        return
    width = sidebar_width()
    if action == "save":
        current = tmux("display-message", "-p", "-t", nav, "#{pane_width}")
        if current.isdigit() and int(current) != width and int(current) >= 24:
            WIDTH_FILE.write_text(current)
        return
    width = {"grow": width + 4, "shrink": width - 4, "reset": SIDEBAR_WIDTH}.get(action, width)
    width = max(24, min(160, width))
    WIDTH_FILE.write_text(str(width))
    tmux("resize-pane", "-t", nav, "-x", str(width))


def focus_sidebar():
    tmux("select-pane", "-t", nav_pane())


def launch():
    if not shutil.which("tmux"):
        raise SystemExit("crew's workspace needs tmux: brew install tmux")
    HOME.mkdir(parents=True, exist_ok=True)
    CONF.write_text(TMUX_CONF % {"crew": shlex.quote(sys.executable) + " " + shlex.quote(CREW)})
    running = subprocess.run(["tmux", "-L", "crew", "has-session", "-t", SESSION], capture_output=True).returncode == 0
    if running and tmux("show-options", "-t", SESSION, "-v", "@crew_code") != code_id():
        # the workspace was started by another crew (an older install or upgrade): start it fresh on this one.
        # Agent chats are saved Claude Code sessions, so they pick up where they were when reopened.
        tmux("kill-server")
        running = False
    if not running:
        create(*os.get_terminal_size())
    else:
        tmux("source-file", str(CONF))
    env = {k: v for k, v in os.environ.items() if k != "TMUX"}
    os.execvpe("tmux", ["tmux", "-L", "crew", "-f", str(CONF), "attach", "-t", SESSION], env)


def code_id():
    """Which crew this is: its path and version, so a workspace started by an older one is noticed."""
    return f"{CREW}:{os.path.getmtime(CREW):.0f}"


def create(columns, lines):
    CONF.write_text(TMUX_CONF % {"crew": shlex.quote(sys.executable) + " " + shlex.quote(CREW)})
    if True:
        size = os.terminal_size((columns, lines))
        tmux("new-session", "-d", "-s", SESSION, "-n", "main", "-x", str(size.columns), "-y", str(size.lines - 1),
             crew_cmd("nav"), check=True)
        nav = tmux("display-message", "-p", "-t", f"{SESSION}:main", "#{pane_id}")
        tmux("set-option", "-t", SESSION, "@nav", nav)
        tmux("set-option", "-t", SESSION, "@crew_code", code_id())
        tmux("set-option", "-p", "-t", nav, "@crew_title", "crew")
        home = tmux("split-window", "-h", "-d", "-t", nav, "-P", "-F", "#{pane_id}", crew_cmd("home"), check=True)
        tmux("set-option", "-p", "-t", home, "@crew_key", "home")
        tmux("set-option", "-p", "-t", home, "@crew_title", "home")
        tmux("resize-pane", "-t", nav, "-x", str(sidebar_width()))
        tmux("select-pane", "-t", nav)
