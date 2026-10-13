"""crew feed: talking to one agent. The conversation (its wake reports, your messages, its replies, streamed as it
writes), what it's doing right now (live, stoppable with esc), and the question waiting on you, answered with a
number key or in your own words. You can always type: while it's busy, your message waits its turn.
ctrl+t opens the full Claude Code chat."""
import hashlib
import json
import time

from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
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

HISTORY = 40  # conversation entries kept on screen
DETAILS = "<!--details-->"


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


def digest(*parts):
    return hashlib.sha1(repr(parts).encode()).hexdigest()


class FeedApp(App):
    CSS = f"""
    Screen {{ background: {INK}; padding: 1 3 0 3; }}
    #head {{ height: 1; color: {PAPER}; text-style: bold; }}
    #state {{ height: 1; color: {GRAPHITE}; margin-bottom: 1; }}
    #log {{ height: 1fr; scrollbar-size-vertical: 1; scrollbar-color: {RULE}; scrollbar-background: {INK}; }}
    .entry {{ margin-bottom: 1; }}
    /* yours: indented on a lifted ground with a green rule; the agent's: flush left; live work: a coral rule */
    .mine {{ margin: 0 0 1 6; background: {SELECT}; border-left: thick {DONE}; padding: 0 1; }}
    .theirs {{ margin: 0 4 1 0; }}
    .working {{ border-left: thick {PENCIL}; padding: 0 1; margin: 0 4 1 0; }}
    /* the question: a card of its own, set apart from the conversation */
    #asks {{ height: auto; max-height: 55%; border: round {PENCIL}; background: {SELECT}; padding: 0 1;
             margin-top: 1; scrollbar-size-vertical: 1; scrollbar-color: {RULE}; scrollbar-background: {SELECT}; }}
    #ask {{ background: {SELECT}; }}
    #queue {{ height: auto; color: {GRAPHITE}; margin-top: 1; }}
    Input {{ background: {INK}; border: tall {RULE}; color: {PAPER}; margin-top: 1; }}
    Input:focus {{ border: tall {GRAPHITE}; }}
    #hint {{ height: auto; color: {GRAPHITE}; }}
    """
    BINDINGS = [Binding("escape", "stop", "stop what it's doing"),
                Binding("up", "prev_question", "previous question", show=False),
                Binding("down", "next_question", "next question", show=False),
                Binding("ctrl+n", "next_question", "next question"),
                Binding("ctrl+t", "full_chat", "the full Claude Code chat"),
                Binding("ctrl+x", "dismiss", "dismiss question"),
                Binding("ctrl+e", "toggle_details", "the question's details"),
                Binding("ctrl+o", "toggle_steps", "what each wake did"),
                Binding("ctrl+l", "clear", "clear the feed")]

    def __init__(self, agent_name):
        super().__init__()
        self.name_ = agent_name
        self.active = 0  # which waiting question the number keys answer
        self.pending = []
        self.live = None  # what it's doing for you right now: {"what", "sid", "started", "steps", "text"}
        self.queue = []  # what you typed while it was busy, sent in turn
        self.show_steps = False
        self.show_details = False
        self.mounted = {}  # entry key -> (digest, widget): the conversation updates in place, no flicker
        self.ask_digest = None
        self.answered = None  # (question, your answer, when): confirmed at the top of the card for a moment

    def compose(self) -> ComposeResult:
        yield Static(self.name_, id="head")
        yield Static("", id="state")
        yield VerticalScroll(id="log")
        with VerticalScroll(id="asks"):
            yield Static("", id="ask")
        yield Static("", id="queue")
        yield AnswerInput(self.answer_digit, placeholder=f"Message {self.name_}", id="msg")
        yield Static("", id="hint")

    def on_mount(self):
        self.agent = crew.load_agent(self.name_)
        self.refresh_all()
        self.query_one("#log").scroll_end(animate=False)
        self.query_one("#msg").focus()
        self.set_interval(2, self.refresh_all)
        self.set_interval(1, self.tick_live)

    # ---------------------------------------------------------- the conversation

    def agent_head(self, when):
        head = Text()
        head.append("∴ ", style=f"bold {PENCIL}")
        head.append(self.name_, style=f"bold {PAPER}")
        head.append(f"   {when}", style=GRAPHITE)
        return head

    def steps_text(self, steps):
        t = Text()
        for i, step in enumerate(steps):
            t.append("\n" if i else "")
            t.append(step, style=GRAPHITE if step.startswith(("says:", "  ↳")) else PAPER)
        return t

    def entries(self, conn):
        """[(ts, key, digest, classes, renderable)] since the last clear, oldest first."""
        out = []
        since = crew.agent_state(self.agent).get("feed_since") or ""
        for r in conn.execute("""SELECT * FROM runs WHERE agent = ? AND (report IS NOT NULL OR status IN ('error', 'running'))
                                 AND started_at >= ? ORDER BY id DESC LIMIT ?""", (self.name_, since, HISTORY)):
            steps = [f"{a[:5]}  {'  ↳ ' if w == 'helper' else ''}{t}" for a, w, t in crew.read_activity(r["session_id"])] \
                if r["session_id"] else []
            why = f" · {r['trigger']}" if r["trigger"] else ""
            if r["status"] == "running":
                head = self.agent_head(f"working in the background · started {crew.ago(r['started_at'])}{why} · "
                                       f"{len(steps)} steps")
                body = self.steps_text(steps[-8:]) if steps else Text("getting started…", style=GRAPHITE)
                out.append((r["started_at"], f"run:{r['id']}", digest("run", len(steps), crew.ago(r["started_at"])),
                            "entry working", Group(head, body)))
                continue
            head = self.agent_head(f"woke {crew.ago(r['started_at'])}{why}")
            body = Markdown(r["report"]) if r["status"] == "ok" else \
                Text(f"That wake didn't finish: {(r['error'] or '')[:300]}", style=PENCIL)
            parts = [head, body]
            if steps and self.show_steps:
                parts += [Text(f"\nwhat it did ({len(steps)} steps)", style=f"bold {GRAPHITE}"), self.steps_text(steps)]
            elif steps:
                parts.append(Text(f"{len(steps)} steps · ctrl+o to see them", style=GRAPHITE))
            out.append((r["started_at"], f"run:{r['id']}",
                        digest(r["status"], r["report"], len(steps), self.show_steps, crew.ago(r["started_at"])),
                        "entry theirs", Group(*parts)))
        for m in conn.execute("SELECT * FROM chat WHERE agent = ? AND ts >= ? ORDER BY id DESC LIMIT ?",
                              (self.name_, since, HISTORY)):
            when = crew.ago(m["ts"])
            if m["role"] == "user":
                head = Text("you", style=f"bold {DONE}")
                head.append(f"   {when}", style=GRAPHITE)
                text = m["text"]
                if text.startswith("> "):  # your answer to one of its questions
                    asked, _, text = text[2:].partition("\n")
                    head.append(f"   answering: {asked[:90]}", style=GRAPHITE)
                out.append((m["ts"], f"chat:{m['id']}", digest(when), "entry mine",
                            Group(head, Text(text, style=PAPER))))
            else:
                out.append((m["ts"], f"chat:{m['id']}", digest(when), "entry theirs",
                            Group(self.agent_head(when), Markdown(m["text"] or "_(no reply)_"))))
        out.sort(key=lambda e: (e[0] or "", e[1]))
        return out[-HISTORY:]

    def live_entry(self):
        """What it's doing for you right now: streamed reply text, or its latest steps."""
        lv = self.live
        secs = int(time.time() - lv["started"])
        head = self.agent_head(f"{lv['what']} · {secs // 60}:{secs % 60:02d} · esc to stop")
        parts = [head]
        if lv["steps"]:
            parts.append(self.steps_text(lv["steps"][-6:]))
        if lv["text"].strip():
            parts.append(Markdown(lv["text"]))
        elif not lv["steps"]:
            parts.append(Text("thinking…", style=GRAPHITE))
        return ("~", "live", digest(secs, len(lv["steps"]), len(lv["text"])), "entry working", Group(*parts))

    def sync_log(self, items):
        """Add what's new, update what changed, drop what's gone. Follows the bottom unless you scrolled up."""
        log = self.query_one("#log", VerticalScroll)
        at_end = log.scroll_y >= log.max_scroll_y - 2
        keys = {k for _, k, _, _, _ in items}
        for k in [k for k in self.mounted if k not in keys]:
            self.mounted.pop(k)[1].remove()
        prev = None
        changed = False
        for _, key, dig, classes, renderable in items:
            if key in self.mounted:
                old, w = self.mounted[key]
                if old != dig:
                    w.update(renderable)
                    w.set_classes(classes)
                    self.mounted[key] = (dig, w)
                    changed = True
            else:
                w = Static(renderable, classes=classes)
                if prev is not None:
                    log.mount(w, after=prev)
                elif log.children:
                    log.mount(w, before=log.children[0])
                else:
                    log.mount(w)
                self.mounted[key] = (dig, w)
                changed = True
            prev = self.mounted[key][1]
        if changed and at_end:
            log.call_after_refresh(log.scroll_end, animate=False)

    def refresh_all(self):
        conn = crew.db()
        self.agent = crew.load_agent(self.name_)
        self.pending = conn.execute("SELECT * FROM decisions WHERE agent = ? AND status = 'pending' ORDER BY id",
                                    (self.name_,)).fetchall()
        self.active = min(self.active, max(0, len(self.pending) - 1))
        st = crew.agent_state(self.agent)
        doing = st.get("status") or st.get("why")
        self.query_one("#state", Static).update(Text(
            crew.agent_status_text(self.agent).split(" · ", 1)[1] + (f" · {doing}" if doing else ""), style=GRAPHITE))
        items = self.entries(conn)
        if self.live:
            items.append(self.live_entry())
        if not items:
            items = [("", "empty", "", "entry theirs",
                      Text(f"Nothing here yet. {self.name_} reports here after each wake. Message it below, or press r "
                           "in the rail to wake it now.", style=GRAPHITE))]
        self.sync_log(items)
        self.render_asks()
        self.render_queue()
        self.render_hint()

    def tick_live(self):
        if self.live:
            self.sync_log(self.entries(crew.db()) + [self.live_entry()])

    # ---------------------------------------------------------- the question

    def render_asks(self):
        """Every waiting question in one card: the selected one open with its choices, the rest one line each."""
        asks = self.query_one("#asks", VerticalScroll)
        msg = self.query_one("#msg", Input)
        recent = self.answered if self.answered and time.time() - self.answered[2] < 20 else None
        if not self.pending and not recent:
            asks.display = False
            msg.placeholder = f"Message {self.name_}"
            return
        asks.display = True
        dig = digest([d["id"] for d in self.pending], self.active, self.show_details, recent,
                     [(d["question"], d["context"], d["drafts"]) for d in self.pending])
        if dig == self.ask_digest:
            return
        self.ask_digest = dig
        parts = []
        if recent:
            done = Text()
            done.append("✓ answered  ", style=f"bold {DONE}")
            done.append(f"{recent[0][:70]}", style=f"strike {GRAPHITE}")
            done.append(f"  →  {recent[1][:60]}", style=PAPER)
            parts += [done, Text("")]
        if not self.pending:
            msg.placeholder = f"Message {self.name_}"
            parts.append(Text("No more questions. Its reply shows in the conversation above.", style=GRAPHITE))
            self.query_one("#ask", Static).update(Group(*parts))
            return
        n = len(self.pending)
        title = Text()
        title.append(f"{n} question{'s' if n > 1 else ''} from {self.name_}", style=f"bold {PENCIL}")
        if n > 1:
            title.append("   ↑ ↓ to move between them", style=GRAPHITE)
        parts += [title, Text("")]
        for i, d in enumerate(self.pending):
            about = f"  ·  {crew.short_key(d['item_key'])}" if d["item_key"] else ""
            if i != self.active:
                row = Text()
                row.append("  ○  ", style=GRAPHITE)
                row.append(d["question"][:110], style=GRAPHITE)
                row.append(about, style=GRAPHITE)
                parts.append(row)
                continue
            options = json.loads(d["options"])
            msg.placeholder = f"Press 1–{len(options)}, or type your answer"
            row = Text()
            row.append("  ●  ", style=f"bold {PENCIL}")
            row.append(d["question"], style=f"bold {PAPER}")
            row.append(about, style=GRAPHITE)
            parts.append(row)
            why, _, details = (d["context"] or "").partition(DETAILS)
            lines = [l for l in why.strip().splitlines() if l.strip()]
            short = "\n".join(lines if self.show_details else lines[:3]) + \
                ("\n…" if len(lines) > 3 and not self.show_details else "")
            body = []
            if short.strip():
                body.append(Markdown(short))
            opts = Text()
            for k, o in enumerate(options, 1):
                opts.append("\n" if k > 1 else "")
                opts.append(f" {k} ", style=f"bold {INK} on {PENCIL}")
                opts.append(f"  {o}", style=PAPER)
            body += [Text(""), opts]
            if d["recommendation"]:
                body.append(Text(f"\nSuggested: {d['recommendation']}", style=GRAPHITE))
            if self.show_details:
                if details.strip():
                    body += [Text("\nDetails", style=f"bold {GRAPHITE}"), Markdown(details.strip())]
                if d["drafts"]:
                    body += [Text("\nWhat it would post", style=f"bold {GRAPHITE}"), Markdown(d["drafts"])]
            elif details.strip() or d["drafts"]:
                body.append(Text("\nctrl+e for details" + (" and what it would post" if d["drafts"] else ""),
                                 style=GRAPHITE))
            parts.append(Padding(Group(*body), (1, 0, 1, 5)))
        self.query_one("#ask", Static).update(Group(*parts))

    def render_queue(self):
        q = self.query_one("#queue", Static)
        q.display = bool(self.queue)
        if self.queue:
            t = Text()
            for i, text in enumerate(self.queue):
                t.append("\n" if i else "")
                t.append("waiting to send  ", style=GRAPHITE)
                t.append(text[:120], style=PAPER)
            q.update(t)

    def render_hint(self):
        keys = []
        if self.live:
            keys.append(("esc", "stop"))
        if self.pending:
            n = len(json.loads(self.pending[self.active]["options"]))
            keys.append((f"1–{n}", "answer"))
            if len(self.pending) > 1:
                keys.append(("↑↓", "other questions"))
            keys += [("ctrl+e", "less" if self.show_details else "details"), ("ctrl+x", "dismiss")]
        keys += [("enter", "send"), ("ctrl+o", "hide steps" if self.show_steps else "steps"), ("ctrl+l", "clear"),
                 ("ctrl+t", "full chat")]
        t = Text()
        for k, word in keys:
            t.append(k, style=PAPER)
            t.append(f" {word}    ", style=GRAPHITE)
        self.query_one("#hint", Static).update(t)

    # ---------------------------------------------------------- talking

    def start_live(self, what):
        self.live = {"what": what, "sid": None, "started": time.time(), "steps": [], "text": ""}
        self.refresh_all()

    def finish_live(self):
        self.live = None
        self.refresh_all()
        if self.queue:
            self.dispatch(self.queue.pop(0))

    def on_live_event(self, kind, text):
        """From a worker thread: the session to stop, a step, or reply text as it's written."""
        lv = self.live
        if not lv:
            return
        if kind == "session":
            lv["sid"] = text
        elif kind == "step":
            lv["steps"].append(text)
        elif kind == "delta":
            lv["text"] += text
        elif kind == "text":
            lv["text"] = text

    def dispatch(self, text):
        """With a question in front of you, what you type answers it; otherwise it's a message."""
        if self.pending:
            d = self.pending[self.active]
            self.answered = (d["question"], text, time.time())
            self.pending = [x for x in self.pending if x["id"] != d["id"]]
            self.active = min(self.active, max(0, len(self.pending) - 1))
            self.show_details = False
            self.start_live("on it")
            self.carry_out(d, text)
        else:
            self.start_live("replying")
            self.send(text)

    def answer_digit(self, n):
        if not self.pending:
            return False
        options = json.loads(self.pending[self.active]["options"])
        if not 1 <= n <= len(options):
            return False
        if self.live:
            self.queue.append(options[n - 1])
            self.render_queue()
            return True
        self.dispatch(options[n - 1])
        return True

    def on_input_submitted(self, event: Input.Submitted):
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if self.live:  # never block typing: it goes out when the current reply is done
            self.queue.append(text)
            self.render_queue()
            self.render_hint()
            return
        self.dispatch(text)

    @work(thread=True, group="talk")
    def carry_out(self, d, answer):
        try:
            crew.answer_decision(crew.db(), self.agent, d, answer, quiet=True,
                                 on_step=lambda k, t: self.on_live_event(k, t))
        except Exception:
            pass  # answer_decision already put what went wrong into the conversation
        self.call_from_thread(self.finish_live)

    @work(thread=True, group="talk")
    def send(self, text):
        try:
            crew.send_message(self.agent, text, lambda k, t: self.on_live_event(k, t))
        except Exception:
            pass  # send_message already put what went wrong into the conversation
        self.call_from_thread(self.finish_live)

    # ---------------------------------------------------------- keys

    def action_stop(self):
        if self.live and self.live.get("sid"):
            crew.stop_work(self.live["sid"])
            self.live["what"] = "stopping"
        elif self.queue:
            self.queue.clear()
            self.render_queue()

    def action_next_question(self):
        if len(self.pending) > 1:
            self.active = (self.active + 1) % len(self.pending)
            self.show_details = False
            self.refresh_all()

    def action_prev_question(self):
        if len(self.pending) > 1:
            self.active = (self.active - 1) % len(self.pending)
            self.show_details = False
            self.refresh_all()

    def action_full_chat(self):
        tx.show(f"agent:{self.name_}", tx.crew_cmd("chat", self.name_), self.name_, cwd=crew.AGENTS_DIR / self.name_)

    def action_dismiss(self):
        if not self.pending:
            return
        conn = crew.db()
        conn.execute("UPDATE decisions SET status = 'dismissed', answered_at = ? WHERE id = ?",
                     (crew.now(), self.pending[self.active]["id"]))
        conn.commit()
        self.refresh_all()

    def action_toggle_details(self):
        self.show_details = not self.show_details
        self.refresh_all()

    def action_toggle_steps(self):
        self.show_steps = not self.show_steps
        self.refresh_all()

    def action_clear(self):
        """Start the feed fresh. History stays in crew (crew runs, crew inbox); waiting questions stay."""
        crew.agent_state(self.agent, feed_since=crew.now())
        self.refresh_all()

    def on_text_selected(self, event):
        """Select text with the mouse and it's copied, like a terminal."""
        text = self.screen.get_selected_text()
        if text and text.strip():
            crew.copy_text(text)
            self.notify("copied", timeout=1.5)
