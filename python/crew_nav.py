"""The crew rail: open queries first, then each agent's picked PRs. Selecting something shows its live session."""
import os
import time

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

import crew
import crew_tmux as tx
from galley import GRAPHITE, INK_RAIL, PAPER, PENCIL, RULE, SELECT

KEYS = [("enter", "open"), ("d", "answer"), ("r", "run"), ("s", "schedule"), ("n", "new agent"),
        ("w", "close chat"), ("x", "remove")]
def fit(text, width):
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


class NavApp(App):
    CSS = f"""
    Screen {{ background: {INK_RAIL}; }}
    #brand {{ height: 1; padding: 0 2; margin: 1 0; color: {PAPER}; text-style: bold; }}
    OptionList {{ background: {INK_RAIL}; border: none; padding: 0; height: 1fr; scrollbar-size-vertical: 1;
                  scrollbar-color: {RULE}; scrollbar-background: {INK_RAIL}; }}
    OptionList:focus {{ border: none; background-tint: transparent; }}
    OptionList > .option-list--option-highlighted {{ background: {SELECT}; text-style: none; }}
    OptionList:focus > .option-list--option-highlighted {{ background: {SELECT}; text-style: none; }}
    OptionList > .option-list--option-hover {{ background: {SELECT}; }}
    OptionList > .option-list--option-disabled {{ color: {PAPER}; }}
    #keys {{ height: auto; padding: 1 2; color: {GRAPHITE}; border-top: solid {RULE}; background: {INK_RAIL}; }}
    """
    BINDINGS = [
        Binding("d", "decide", "answer"),
        Binding("r", "run", "run"),
        Binding("s", "schedule", "schedule"),
        Binding("w", "close_chat", "close chat"),
        Binding("W", "close_all_chats", "close all chats"),
        Binding("x", "remove_item", "remove"),
        Binding("n", "new", "new agent"),
        Binding("e", "edit", "edit"),
        Binding("[", "narrower", "narrower"),
        Binding("]", "wider", "wider"),
        Binding("ctrl+r", "refresh", "refresh"),
        Binding("q", "detach", "hide"),
    ]

    def compose(self) -> ComposeResult:
        brand = Text()
        brand.append("∴ ", style=f"bold {PENCIL}")  # the icon's three agents
        brand.append("crew", style=f"bold {PAPER}")
        yield Static(brand, id="brand")
        yield OptionList(id="rail")
        yield Static("", id="keys")

    def context_keys(self):
        """The keys that do something on the highlighted row, in the words of what they'll do."""
        d = self.current() if getattr(self, "data", None) else {}
        kind = d.get("kind")
        if kind == "agent":
            a = crew.load_agent(d["agent"])
            if a.get("mode") == "agentic":
                on = crew.agent_state(a).get("awake")
                return [("enter", f"open {a['name']}"), ("r", "wake it now"),
                        ("s", "stop it" if on else "start it (it picks its own wake times)"), ("e", "edit"),
                        ("n", "new agent")]
            on = crew.schedule_on(a["name"])
            return [("enter", f"talk to {a['name']}"), ("r", "run now"),
                    ("s", "stop schedule" if on else f"start schedule (every {a['interval']})"), ("e", "edit"),
                    ("n", "new agent")]
        if kind == "home":
            return [("enter", "show activity"), ("n", "new agent")]
        if kind == "new":
            return [("enter", "create an agent")]
        if kind == "split":
            return [("enter", "open this chat"), ("w", "close it (kept)"), ("x", "remove it")]
        return KEYS

    def on_option_list_option_highlighted(self, event):
        self.render_keys()

    def render_keys(self):
        """Pack the key hints into lines that fit, never splitting a key from its word."""
        room = max(16, self.size.width - 4)
        lines, line, used = [], Text(), 0
        for k, word in self.context_keys() + [("[ ]", "width")]:
            n = len(k) + 1 + len(word)
            if used and used + 3 + n > room:
                lines.append(line)
                line, used = Text(), 0
            if used:
                line.append("   ")
                used += 3
            line.append(k, style=PAPER)
            line.append(f" {word}", style=GRAPHITE)
            used += n
        lines.append(line)
        self.query_one("#keys", Static).update(Text("\n").join(lines))

    def on_mount(self):
        self.data = {}
        self.signature = None
        self.render_keys()
        self.refresh_rail()
        self.query_one(OptionList).focus()
        self.set_interval(3, self.refresh_rail)

    # ---------------------------------------------------------- model

    def width(self):
        return max(24, self.size.width - 1)

    def status_line(self, conn, a):
        """What the agent is doing, in words, like Overtime's second line."""
        name = a["name"]
        last = conn.execute("SELECT * FROM runs WHERE agent = ? ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        if last and last["status"] == "running":
            return "working now", PAPER
        st = crew.agent_state(a)
        if st.get("paused_until") and st["paused_until"] > crew.now():
            return crew.wake_text(a), GRAPHITE
        if not st.get("awake"):
            return "stopped", GRAPHITE
        doing = st.get("status") or st.get("why")
        return crew.wake_text(a) + (f" · {doing}" if doing else ""), GRAPHITE

    def model(self):
        conn = crew.db()
        w = self.width()
        open_keys = set(tx.panes())
        rows = [("home", Text("  Activity", style=f"bold {PAPER}" if "home" in open_keys else GRAPHITE),
                 {"kind": "home"}, False), ("gap:top", Text(""), {}, True)]
        for name in crew.agent_names():
            a = crew.load_agent(name)
            needs = conn.execute("SELECT COUNT(*) FROM decisions WHERE agent = ? AND status = 'pending'",
                                 (name,)).fetchone()[0]
            badge = f"{needs} needs you" if needs else ""
            t = Text()
            t.append("▍ " if needs else "  ", style=PENCIL)
            t.append(f"{fit(name, w - 4 - len(badge)):<{w - 3 - len(badge)}}", style=f"bold {PAPER}")
            t.append(f"{badge} ", style=f"bold {PENCIL}")
            status, sstyle = self.status_line(conn, a)
            t.append("\n")
            t.append("▍ " if needs else "  ", style=PENCIL)
            t.append(fit(status, w - 3), style=sstyle)
            rows.append((f"agent:{name}", t, {"kind": "agent", "agent": name}, False))
            for c in conn.execute("SELECT * FROM splits WHERE agent = ? AND closed_at IS NULL ORDER BY id", (name,)):
                live = f"split:{c['id']}" in open_keys
                num = crew.short_key(c["item_key"]).split("#")[-1] + " " if c["item_key"] else ""
                t = Text("    ")
                t.append(fit(f"{num}{c['title']}", w - 5), style=PAPER if live else GRAPHITE)
                rows.append((f"split:{c['id']}", t, {"kind": "split", "agent": name, "id": c["id"],
                                                    "title": c["title"]}, False))
            rows.append((f"gap:{name}", Text(""), {}, True))
        rows.append(("new", Text("  + New agent", style=GRAPHITE), {"kind": "new"}, False))
        return rows

    def follow_new_splits(self):
        """When an agent splits a chat off, take the user straight to it."""
        for c in crew.db().execute("SELECT * FROM splits WHERE opened = 0 AND closed_at IS NULL ORDER BY id"):
            self.open({"kind": "split", "agent": c["agent"], "id": c["id"], "title": c["title"]})

    def refresh_rail(self):
        if not hasattr(self, "data"):
            return
        ol = self.query_one(OptionList)
        rows = self.model()
        signature = [(k, p.plain, str(p.spans)) for k, p, _, _ in rows]
        if signature == self.signature:
            return
        current = ol.get_option_at_index(ol.highlighted).id if ol.highlighted is not None else None
        self.data = {k: d for k, _, d, _ in rows}
        ol.clear_options()
        ol.add_options([Option(p, id=k, disabled=dis) for k, p, _, dis in rows])
        ids = [k for k, *_ in rows]
        ol.highlighted = ids.index(current) if current in ids else next(
            (i for i, (_, _, _, dis) in enumerate(rows) if not dis), 0)
        self.signature = signature
        self.reap_closed()
        self.follow_new_splits()

    def reap_closed(self):
        """Close panes whose chat was removed."""
        for key in tx.panes():
            if key.startswith("split:") and key not in self.data:
                tx.close(key)

    def on_resize(self, event):
        if not hasattr(self, "data"):
            return
        self.render_keys()
        self.signature = None  # columns depend on width
        self.refresh_rail()

    # ---------------------------------------------------------- selection

    def current(self):
        ol = self.query_one(OptionList)
        if ol.highlighted is None:
            return {}
        return self.data.get(ol.get_option_at_index(ol.highlighted).id, {})

    def on_text_selected(self, event):
        """Select text with the mouse and it's copied, like a terminal."""
        text = self.screen.get_selected_text()
        if text and text.strip():
            crew.copy_text(text)
            self.notify("copied", timeout=1.5)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        self.open(self.data.get(event.option.id, {}))

    def mark_seen(self, agent, key):
        conn = crew.db()
        conn.execute("UPDATE items SET seen_version = version WHERE agent = ? AND key = ?", (agent, key))
        conn.commit()

    def open(self, d):
        kind = d.get("kind")
        if kind == "home":
            tx.show("home", tx.crew_cmd("home"), "activity", focus=False)
        elif kind == "agent":
            a = d["agent"]
            if crew.load_agent(a).get("mode") == "agentic":
                tx.show(f"feed:{a}", tx.crew_cmd("feed", a), a, focus=True)
            else:
                tx.show(f"agent:{a}", tx.crew_cmd("chat", a), a, cwd=crew.AGENTS_DIR / a)
        elif kind == "item":
            self.mark_seen(d["agent"], d["key"])
            num = crew.short_key(d["key"]).split("#")[-1]
            tx.show(f"item:{d['agent']}:{d['key']}", tx.crew_cmd("thread", d["agent"], d["key"]),
                    f"{d['agent']}  ·  {num}  {d['title'][:80]}")
        elif kind == "split":
            tx.show(f"split:{d['id']}", tx.crew_cmd("open-split", str(d["id"])), f"{d['agent']}  ·  {d['title'][:80]}")
        elif kind == "new":
            tx.show("new", tx.crew_cmd("new"), "new agent", restart=True)

    def decide(self, agent, key):
        """Questions are answered in the agent's feed, where the whole list is."""
        self.mark_seen(agent, key)
        tx.show(f"feed:{agent}", tx.crew_cmd("feed", agent), agent)

    # ---------------------------------------------------------- actions

    def action_new(self):
        self.open({"kind": "new"})

    def action_decide(self):
        d = self.current()
        if d.get("kind") == "item":
            self.decide(d["agent"], d["key"])
            return
        sql, args = "SELECT agent, item_key FROM decisions WHERE status = 'pending'", ()
        if d.get("agent"):
            sql, args = sql + " AND agent = ?", (d["agent"],)
        r = crew.db().execute(sql + " ORDER BY id LIMIT 1", args).fetchone()
        if r:
            self.decide(r["agent"], r["item_key"])
        else:
            self.notify("Nothing is waiting on you.")

    def action_run(self):
        d = self.current()
        if d.get("agent"):
            crew.start_background_run(d["agent"])
            self.notify(f"{d['agent']} is awake and working.")

    def action_schedule(self):
        d = self.current()
        if d.get("kind") != "agent":
            self.notify("Select an agent to schedule it.")
            return
        a = crew.load_agent(d["agent"])
        if a.get("mode") == "agentic":
            on = crew.agent_state(a).get("awake")
            crew.set_awake(a, not on)
            self.notify(f"{a['name']} stopped. It only works when you press r or ask it." if on
                        else f"{a['name']} started. It wakes now, then picks its own next wake time.")
            self.signature = None
            self.refresh_rail()
            self.render_keys()
            return
        if crew.schedule_on(a["name"]):
            crew.unschedule(a["name"])
            self.notify(f"{a['name']} is back to manual: it runs only when you press r.")
        else:
            crew.install_schedule(a)
            self.notify(f"{a['name']} runs every {a['interval']}. Press s again to stop it.")
        self.signature = None
        self.refresh_rail()
        self.render_keys()

    def action_edit(self):
        d = self.current()
        if d.get("agent"):
            a = d["agent"]
            tx.show(f"edit:{a}", f"{os.environ.get('EDITOR', 'nano')} {crew.load_agent(a)['job_file']} agent.json", f"{a}  ·  edit",
                    cwd=crew.AGENTS_DIR / a, restart=True)

    def chat_keys(self, d):
        if d.get("kind") == "agent":
            return [f"agent:{d['agent']}"]
        if d.get("kind") == "split":
            return [f"split:{d['id']}"]
        return []

    def action_close_chat(self):
        open_keys = [k for k in self.chat_keys(self.current()) if k in tx.panes()]
        if not open_keys:
            self.notify("No chat is open here.")
            return
        for k in open_keys:
            tx.close(k)
        self.notify("Chat closed. Enter reopens the same conversation.")
        self.signature = None
        self.refresh_rail()

    def action_close_all_chats(self):
        keys = [k for k in tx.panes() if k != "home"]
        for k in keys:
            tx.close(k)
        self.notify(f"Closed {len(keys)} chat{'s' if len(keys) != 1 else ''}.")
        self.signature = None
        self.refresh_rail()

    def action_remove_item(self):
        d = self.current()
        if d.get("kind") != "split":
            self.notify("Select a split chat to remove it.")
            return
        armed = getattr(self, "_armed", (None, 0))
        if armed[0] != d["id"] or time.time() - armed[1] > 4:
            self._armed = (d["id"], time.time())
            self.notify(f"Press x again to remove \"{d['title']}\".", severity="warning")
            return
        self._armed = (None, 0)
        tx.close(f"split:{d['id']}")
        conn = crew.db()
        conn.execute("UPDATE splits SET closed_at = ? WHERE id = ?", (crew.now(), d["id"]))
        conn.commit()
        self.notify(f"Removed \"{d['title']}\". Its conversation is still saved.")
        self.signature = None
        self.refresh_rail()

    def action_narrower(self):
        tx.sidebar("shrink")

    def action_wider(self):
        tx.sidebar("grow")

    def action_refresh(self):
        self.signature = None
        self.refresh_rail()

    def action_detach(self):
        tx.tmux("detach-client")
