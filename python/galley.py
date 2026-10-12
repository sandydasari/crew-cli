"""crew's palette and words: a proof on the desk. Red pencil is reserved for what's waiting on you."""
from rich.markdown import CodeBlock, Heading, Markdown
from rich.syntax import Syntax
from rich.text import Text
from rich.theme import Theme

INK = "#16181d"       # page ground (home, sessions)
INK_RAIL = "#1b1e24"  # the rail, a shade lifted from the page
SELECT = "#262a33"    # highlighted row
RULE = "#2a2d35"      # hairline rules
PAPER = "#e8e6e1"     # text
GRAPHITE = "#8b92a5"  # metadata, quiet states
PENCIL = "#e2674d"    # queries waiting on you, and nothing else
DONE = "#8fa58a"      # answered / merged (muted, never competes with pencil)

VERDICT_WORDS = {
    "approve": "looks good", "request_changes": "needs work", "comment": "comments", "nothing_new": "quiet",
    "needs_human": "needs you", "fixes_proposed": "fixes", "needs_reply": "reply", "ci_failing": "ci failing",
    "ready_to_merge": "ready", "my_code": "my code", "flaky_test": "flaky", "infra": "infra", "mixed": "mixed",
}


def verdict_word(verdict):
    return VERDICT_WORDS.get(verdict or "", (verdict or "reviewed").replace("_", " "))


THEME = Theme({
    "markdown.h1": f"bold {PAPER}", "markdown.h2": f"bold {PAPER}", "markdown.h3": f"bold {PAPER}",
    "markdown.h4": f"bold {GRAPHITE}", "markdown.code": f"{PAPER} on #22252c", "markdown.code_block": f"{PAPER} on #22252c",
    "markdown.link": f"underline {GRAPHITE}", "markdown.link_url": GRAPHITE, "markdown.item.bullet": GRAPHITE,
    "markdown.item.number": GRAPHITE, "markdown.block_quote": GRAPHITE, "markdown.hr": RULE,
    "rule.line": RULE, "repr.number": PAPER, "prompt": PAPER, "prompt.default": GRAPHITE, "prompt.choices": GRAPHITE,
})


class _LeftHeading(Heading):
    """Rich centres and boxes h1/h2; a proof sets headings flush left."""

    def __rich_console__(self, console, options):
        text = self.text
        text.justify = "left"
        yield Text("")
        yield text


class _InkCodeBlock(CodeBlock):
    """Code on a slab of the ink itself, not Rich's olive monokai with padding rows."""

    def __rich_console__(self, console, options):
        yield Syntax(str(self.text).rstrip(), self.lexer_name, theme="ansi_dark", background_color="#22252c",
                     word_wrap=True, padding=(0, 1))


Markdown.elements["heading_open"] = _LeftHeading
Markdown.elements["fence"] = _InkCodeBlock
Markdown.elements["code_block"] = _InkCodeBlock


def label(text):
    """A section label: CAPS, graphite, flush left."""
    return Text(text.upper(), style=f"bold {GRAPHITE}")
