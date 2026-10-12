"""crew feed: one agent's conversation, Overtime-style. Its wake reports, your messages and its replies, what you
answered, and the questions waiting on you, answered with a number key. Enter on an empty line opens the full
Claude Code chat."""
import json

from rich.console import Group
from rich.markdown import Markdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.widgets import Input, Static

import crew
import crew_tmux as tx
import galley  # noqa: F401  (flush-left markdown headings)
from galley import DONE, GRAPHITE, INK, PAPER, PENCIL, RULE, SELECT

HISTORY = 40  # entries shown above the waiting questions


class AnswerInput(Input):
    """Typing goes to the agent; a digit on an empty line answers the question in front of you."""

    def __init__(self, on_digit, **kw):
        super().__init__(**kw)
        self.on_digit = on_digit

    async def _on_key(self, event):
        if not self.value and event.character and event.character.isdigit() and self.on_digit(int(event.character)):
            event.stop()
            event.prevent_default()
            return
        await super()._on_key(event)


class FeedApp(App):
    CSS = f"""
    Screen {{ background: {INK}; padding: 1 3 0 3; }}
    #head {{ height: 1; color: {PAPER}; text-style: bold; }}
    #state {{ height: 1; color: {GRAPHITE}; margin-bottom: 1; }}
    #log {{ height: 1fr; scrollbar-size-vertical: 1; scrollbar-color: {RULE}; scrollbar-background: {INK}; }}
    .entry {{ margin-bottom: 1; }}
    /* yours: indented, on a lifted ground with a green rule; the agent's: flush left on the page */
    .mine {{ margin: 0 0 1 6; background: {SELECT}; border-left: thick {DONE}; padding: 0 1; }}
    .working {{ border-left: thick {PENCIL}; padding: 0 1; }}
    .theirs {{ margin-right: 4; }}
    .answer {{ margin-left: 6; border-left: thick {RULE}; padding: 0 1; }}
    #asks {{ height: auto; max-height: 60%; overflow-y: auto; border-top: solid {RULE}; padding-top: 1; }}
    #busy {{ height: auto; color: {GRAPHITE}; }}
    Input {{ background: {INK}; border: tall {RULE}; color: {PAPER}; margin-top: 1; }}
    Input:focus {{ border: tall {GRAPHITE}; }}
    #hint {{ height: 1; color: {GRAPHITE}; }}
    """
    BINDINGS = [Binding("ctrl+n", "next_question", "next question"),
                Binding("ctrl+x", "dismiss", "dismiss question"),
                Binding("ctrl+l", "clear", "clear the feed"),
                Binding("ctrl+o", "toggle_steps", "show or hide what it did")]

    def __init__(self, agent_name):
        super().__init__()
        self.name_ = agent_name
        self.signature = None
        self.active = 0  # which waiting question the number keys answer
        self.pending = []
        self.busy = ""
        self.show_steps = False  # every step of finished wakes; a running wake always shows its latest
        self.signature_seen = False

    def compose(self) -> ComposeResult:
        yield Static(self.name_, id="head")
        yield Static("", id="state")
        yield VerticalScroll(id="log")
        yield Static("", id="asks")
        yield Static("", id="busy")
        yield AnswerInput(self.answer_digit, placeholder="Message it, or press a number to answer", id="msg")
        yield Static("", id="hint")

    def on_mount(self):
        self.agent = crew.load_agent(self.name_)
        self.render_feed()
        self.query_one("#msg").focus()
        self.set_interval(3, self.render_feed)

    # ---------------------------------------------------------- data

    def agent_head(self, when):
        head = Text()
        head.append("∴ ", style=f"bold {PENCIL}")
        head.append(self.name_, style=f"bold {PAPER}")
        head.append(f"   {when}", style=GRAPHITE)
        return head

    def steps_text(self, steps):
        t = Text()
        for i, (at, who, text) in enumerate(steps):
            t.append("\n" if i else "")
            t.append(f"{at[:5]}  ", style=GRAPHITE)
            if who == "helper":
                t.append("  ↳ ", style=GRAPHITE)
            t.append(text, style=GRAPHITE if text.startswith("says:") else PAPER)
        return t

    def entries(self, conn):
        """(ts, kind, renderable) for wakes, chat messages and answered questions, oldest first, since the last clear.
        kind is mine (you), theirs (the agent) or answer (a question you answered)."""
        out = []
        since = crew.agent_state(self.agent).get("feed_since") or ""
        for r in conn.execute("""SELECT * FROM runs WHERE agent = ? AND (report IS NOT NULL OR status IN ('error', 'running'))
                                 ORDER BY id DESC LIMIT ?""", (self.name_, HISTORY)):
            steps = crew.read_activity(r["session_id"]) if r["session_id"] else []
            why = f" · {r['trigger']}" if r["trigger"] else ""
            if r["status"] == "running":
                head = self.agent_head(f"working now · started {crew.ago(r['started_at'])}{why} · {len(steps)} steps")
                body = self.steps_text(steps[-12:]) if steps else Text("getting started…", style=GRAPHITE)
                out.append((r["started_at"], "theirs working", Group(head, body)))
                continue
            head = self.agent_head(f"woke {crew.ago(r['started_at'])}{why}")
            body = Markdown(r["report"]) if r["status"] == "ok" else \
                Text(f"That wake failed: {(r['error'] or '')[:300]}", style=PENCIL)
            parts = [head, body]
            if steps and self.show_steps:
                parts += [Text(f"what it did ({len(steps)} steps)", style=f"bold {GRAPHITE}"), self.steps_text(steps)]
            elif steps:
                parts.append(Text(f"{len(steps)} steps · ctrl+o to see what it did", style=GRAPHITE))
            out.append((r["started_at"], "theirs", Group(*parts)))
        for m in conn.execute("SELECT * FROM chat WHERE agent = ? ORDER BY id DESC LIMIT ?", (self.name_, HISTORY)):
            if m["role"] == "user":
                head = Text("you", style=f"bold {DONE}")
                head.append(f"   {crew.ago(m['ts'])}", style=GRAPHITE)
                out.append((m["ts"], "mine", Group(head, Text(m["text"], style=PAPER))))
            else:
                out.append((m["ts"], "theirs", Group(self.agent_head(crew.ago(m["ts"])),
                                                     Markdown(m["text"] or "_(no reply)_"))))
        for d in conn.execute("""SELECT * FROM decisions WHERE agent = ? AND status NOT IN ('pending', 'closed', 'dismissed')
                                 AND answered_at IS NOT NULL ORDER BY id DESC LIMIT ?""", (self.name_, HISTORY)):
            t = Text()
            t.append("you answered", style=f"bold {DONE}")
            t.append(f"   {crew.ago(d['answered_at'])}\n", style=GRAPHITE)
            t.append(d["question"], style=f"strike {GRAPHITE}")
            t.append(f"\n{d['answer'] or d['status']}", style=PAPER)
            parts = [t]
            if d["outcome"] and d["outcome"] != d["answer"]:
                parts.append(Markdown(d["outcome"]))
            out.append((d["answered_at"], "answer", Group(*parts)))
        out = [e for e in out if (e[0] or "") >= since]
        out.sort(key=lambda e: e[0] or "")
        return out[-HISTORY:]

    def render_feed(self):
        conn = crew.db()
        self.agent = crew.load_agent(self.name_)
        self.pending = conn.execute("SELECT * FROM decisions WHERE agent = ? AND status = 'pending' ORDER BY id",
                                    (self.name_,)).fetchall()
        self.active = min(self.active, max(0, len(self.pending) - 1))
        self.query_one("#state", Static).update(Text(crew.agent_status_text(self.agent).split(" · ", 1)[1]
                                                     + self.doing(), style=GRAPHITE))
        running = conn.execute("SELECT session_id FROM runs WHERE agent = ? AND status = 'running'",
                               (self.name_,)).fetchall()
        live = [crew.activity_path(r["session_id"]).stat().st_size if r["session_id"] and
                crew.activity_path(r["session_id"]).exists() else 0 for r in running]
        sig = (conn.execute("SELECT MAX(id), COUNT(*), GROUP_CONCAT(status) FROM runs WHERE agent = ?",
                            (self.name_,)).fetchone()[:], live, self.show_steps,
               conn.execute("SELECT MAX(id) FROM chat WHERE agent = ?", (self.name_,)).fetchone()[0],
               conn.execute("SELECT MAX(answered_at) FROM decisions WHERE agent = ?", (self.name_,)).fetchone()[0],
               [d["id"] for d in self.pending], self.active, self.busy, self.size.width)
        if sig == self.signature:
            return
        self.signature = sig
        log = self.query_one("#log", VerticalScroll)
        log.remove_children()
        entries = self.entries(conn)
        if not entries:
            entries = [("", "theirs", Text(f"Nothing yet. {self.name_} reports here after each wake; press r in the rail to "
                                 "wake it now, or message it below.", style=GRAPHITE))]
        at_end = log.scroll_y >= log.max_scroll_y - 1
        log.mount_all([Static(r, classes=f"entry {kind}") for _, kind, r in entries])
        if at_end or not self.signature_seen:  # follow new steps, unless you've scrolled up to read
            log.call_after_refresh(log.scroll_end, animate=False)
        self.signature_seen = True
        self.render_asks()
        self.query_one("#busy", Static).update(Text(self.busy, style=GRAPHITE) if self.busy else "")
        self.render_hint()

    def doing(self):
        st = crew.agent_state(self.agent)
        doing = st.get("status") or st.get("why")
        return f" · {doing}" if doing else ""

    def render_asks(self):
        asks = self.query_one("#asks", Static)
        if not self.pending:
            asks.display = False
            return
        asks.display = True
        d = self.pending[self.active]
        options = json.loads(d["options"])
        head = Text()
        head.append("▍ ", style=PENCIL)
        head.append(f"{self.name_} needs you", style=f"bold {PENCIL}")
        if len(self.pending) > 1:
            head.append(f"   question {self.active + 1} of {len(self.pending)}", style=GRAPHITE)
        head.append(f"   {crew.short_key(d['item_key'])}", style=GRAPHITE)
        q = Text("▍ ", style=PENCIL)
        q.append(d["question"], style=f"bold {PAPER}")
        opts = Text()  # the choices sit right under the question, so the keys are always in view
        for i, o in enumerate(options, 1):
            opts.append(f"\n  {i}  ", style=f"bold {PAPER}")
            opts.append(o, style=PAPER)
        rec = Text("\n  Recommended  ", style=GRAPHITE)
        rec.append(d["recommendation"], style=PAPER)
        parts = [head, q, opts, rec, Text(""), Markdown(d["context"])]
        if d["drafts"]:
            parts += [Text(""), Text("Drafted (not posted yet)", style=f"bold {GRAPHITE}"), Markdown(d["drafts"])]
        asks.update(Group(*parts))
        asks.scroll_home(animate=False)

    def render_hint(self):
        keys = []
        if self.pending:
            n = len(json.loads(self.pending[self.active]["options"]))
            keys.append((f"1–{n}" if n > 1 else "1", "answer"))
            if len(self.pending) > 1:
                keys.append(("ctrl+n", "next question"))
            keys.append(("ctrl+x", "dismiss"))
        keys += [("ctrl+o", "hide steps" if self.show_steps else "what it did"), ("ctrl+l", "clear")]
        keys += [("enter", "send (empty: full chat)"), ("alt ←", "rail")]
        t = Text()
        for k, word in keys:
            t.append(k, style=PAPER)
            t.append(f" {word}    ", style=GRAPHITE)
        self.query_one("#hint", Static).update(t)

    # ---------------------------------------------------------- actions

    def answer_digit(self, n):
        if not self.pending or self.busy:
            return False
        d = self.pending[self.active]
        options = json.loads(d["options"])
        if not 1 <= n <= len(options):
            return False
        self.busy = f"{self.name_} is carrying out: {options[n - 1]}…"
        self.signature = None
        self.render_feed()
        self.carry_out(d, options[n - 1])
        return True

    @work(thread=True, exclusive=True, group="busy")
    def carry_out(self, d, answer):
        conn = crew.db()
        try:
            crew.answer_decision(conn, self.agent, d, answer, quiet=True)
            self.busy = ""
        except Exception as e:
            self.busy = f"Couldn't carry that out: {str(e)[:200]}"
        self.signature = None
        self.call_from_thread(self.render_feed)

    def on_text_selected(self, event):
        """Select text with the mouse and it's copied, like a terminal."""
        text = self.screen.get_selected_text()
        if text and text.strip():
            crew.copy_text(text)
            self.notify("copied", timeout=1.5)

    def action_toggle_steps(self):
        self.show_steps = not self.show_steps
        self.signature = None
        self.render_feed()

    def action_clear(self):
        """Start the feed fresh. History stays in crew (crew runs, crew inbox); waiting questions stay."""
        crew.agent_state(self.agent, feed_since=crew.now())
        self.signature = None
        self.render_feed()

    def action_next_question(self):
        if self.pending:
            self.active = (self.active + 1) % len(self.pending)
            self.render_feed()

    def action_dismiss(self):
        if not self.pending or self.busy:
            return
        conn = crew.db()
        conn.execute("UPDATE decisions SET status = 'dismissed', answered_at = ? WHERE id = ?",
                     (crew.now(), self.pending[self.active]["id"]))
        conn.commit()
        self.signature = None
        self.render_feed()

    def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""
        if not text:
            tx.show(f"agent:{self.name_}", tx.crew_cmd("chat", self.name_), self.name_, cwd=crew.AGENTS_DIR / self.name_)
            return
        if self.busy:
            self.notify(f"{self.name_} is still busy with the last thing.")
            event.input.value = text
            return
        self.busy = f"{self.name_} is thinking…"
        self.signature = None
        self.send(text)
        # send_message stores your message first; show it right away
        self.set_timer(0.3, self.render_feed)

    @work(thread=True, exclusive=True, group="busy")
    def send(self, text):
        def progress(kind, hint):
            if kind == "tool":
                self.busy = f"{self.name_}: {hint}"
                self.call_from_thread(self.render_feed)
        try:
            crew.send_message(self.agent, text, progress)
            self.busy = ""
        except Exception as e:
            self.busy = f"No reply: {str(e)[:200]}"
        self.signature = None
        self.call_from_thread(self.render_feed)
