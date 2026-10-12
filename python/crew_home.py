"""crew home: Activity — what needs you, then what each agent did on its latest wakes."""
import textwrap

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

import crew
import crew_tmux as tx
from galley import GRAPHITE, INK, PAPER, PENCIL, RULE, SELECT



def fit(text, width):
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


class HomeApp(App):
    CSS = f"""
    Screen {{ background: {INK}; padding: 1 3; }}
    .head {{ height: 1; color: {PAPER}; text-style: bold; }}
    .sub {{ height: 1; color: {GRAPHITE}; margin-bottom: 1; }}
    #queries {{ height: auto; max-height: 70%; }}
    #since {{ height: 1fr; }}
    OptionList {{ background: {INK}; border: none; padding: 0; scrollbar-size-vertical: 1;
                  scrollbar-color: {RULE}; scrollbar-background: {INK}; }}
    OptionList:focus {{ border: none; background-tint: transparent; }}
    OptionList > .option-list--option-highlighted {{ background: {SELECT}; text-style: none; }}
    OptionList:focus > .option-list--option-highlighted {{ background: {SELECT}; text-style: none; }}
    OptionList > .option-list--option-hover {{ background: {SELECT}; }}
    OptionList > .option-list--option-disabled {{ color: {PAPER}; }}
    OptionList > .option-list--separator {{ color: {RULE}; }}
    #gap {{ height: 1; }}
    #hint {{ height: 1; color: {GRAPHITE}; }}
    """

    def compose(self) -> ComposeResult:
        yield Static("Activity", classes="head")
        yield Static("What your agents did, newest first. Anything that needs you is at the top; "
                     "enter opens that agent, where you answer with a number key.", classes="sub")
        yield OptionList(id="feed")
        yield Static(Text.from_markup(f"[{PAPER}]enter[/] open that agent    [{PAPER}]alt ←[/] back to the rail"),
                     id="hint")

    def on_mount(self):
        self.signature = None
        self.render_feed()
        self.query_one("#feed").focus()
        self.set_interval(4, self.render_feed)

    def width(self):
        return max(40, min(self.size.width - 7, 112))

    def render_feed(self):
        conn = crew.db()
        w = self.width()
        needs = conn.execute("SELECT * FROM decisions WHERE status = 'pending' ORDER BY id DESC").fetchall()
        wakes = conn.execute("""SELECT * FROM runs WHERE report IS NOT NULL OR status = 'error'
                                ORDER BY id DESC LIMIT 30""").fetchall()
        signature = ([d["id"] for d in needs], [r["id"] for r in wakes], w)
        if signature == self.signature:
            return
        self.signature = signature
        f = self.query_one("#feed", OptionList)
        f.clear_options()
        for d in needs:
            num = crew.short_key(d["item_key"]).split("#")[-1]
            t = Text()
            t.append("▍ ", style=PENCIL)
            t.append(f"{d['agent']} needs you", style=f"bold {PENCIL}")
            t.append(f"   {crew.ago(d['created_at'])}", style=GRAPHITE)
            t.append("\n▍ ", style=PENCIL)
            t.append(fit(f"{num}  {d['question']}", w - 2), style=f"bold {PAPER}")
            if d["drafts"]:
                t.append("\n▍ ", style=PENCIL)
                t.append("draft ready for your approval", style=GRAPHITE)
            f.add_option(Option(t, id=f"a:{d['agent']}"))
            f.add_option(None)
        for r in wakes:
            t = Text()
            t.append(f"{r['agent']}", style=f"bold {PAPER}")
            t.append(f"   {crew.ago(r['started_at'])}", style=GRAPHITE)
            body = r["report"] or f"Wake failed: {r['error'] or ''}"
            lines = [l.strip("#* ").strip() for l in body.splitlines() if l.strip()][:3]
            for line in lines:
                for part in textwrap.wrap(line, w - 2, max_lines=2, placeholder="…"):
                    t.append("\n  ")
                    t.append(part, style=PAPER if r["status"] == "ok" else PENCIL)
            f.add_option(Option(t, id=f"a:{r['agent']}"))
            f.add_option(None)
        if not needs and not wakes:
            f.add_option(Option(Text("Quiet so far. Start an agent with s in the rail, or press r to wake one now.",
                                     style=GRAPHITE), disabled=True))

    def on_text_selected(self, event):
        """Select text with the mouse and it's copied, like a terminal."""
        text = self.screen.get_selected_text()
        if text and text.strip():
            crew.copy_text(text)
            self.notify("copied", timeout=1.5)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        agent = (event.option.id or "").removeprefix("a:")
        if agent in crew.agent_names():
            tx.show(f"feed:{agent}", tx.crew_cmd("feed", agent), agent)
