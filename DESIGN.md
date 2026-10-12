---
name: crew
description: Your Claude Code agents' proof desk — open queries first, every PR a thread you can enter.
colors:
  proof-ink: "#16181d"
  rail-ink: "#1b1e24"
  selection: "#262a33"
  hairline: "#2a2d35"
  code-slab: "#22252c"
  proof-paper: "#e8e6e1"
  graphite: "#8b92a5"
  red-pencil: "#e2674d"
  franked: "#8fa58a"
typography:
  body:
    fontFamily: "the user's terminal font (one size)"
    fontSize: "1 cell"
    fontWeight: 400
    lineHeight: 1
  title:
    fontFamily: "the user's terminal font"
    fontSize: "1 cell"
    fontWeight: 700
    lineHeight: 1
  label:
    fontFamily: "the user's terminal font"
    fontSize: "1 cell"
    fontWeight: 700
    lineHeight: 1
    letterSpacing: "normal (UPPERCASE)"
rounded:
  none: "0"
spacing:
  rail-gutter: "2 cells"
  home-gutter: "3 cells"
  number-column: "6 cells"
  section-gap: "1 blank row"
components:
  rail:
    backgroundColor: "{colors.rail-ink}"
    textColor: "{colors.proof-paper}"
    padding: "1 row, 2 cells"
  rail-row-selected:
    backgroundColor: "{colors.selection}"
    textColor: "{colors.proof-paper}"
  page:
    backgroundColor: "{colors.proof-ink}"
    textColor: "{colors.proof-paper}"
    padding: "1 row, 3 cells"
  query-state:
    textColor: "{colors.red-pencil}"
    typography: "{typography.title}"
  answered-state:
    textColor: "{colors.franked}"
  code-block:
    backgroundColor: "{colors.code-slab}"
    textColor: "{colors.proof-paper}"
    padding: "0 1 cell"
---

# Design System: crew

## Overview

**Creative North Star: "The proof on the desk"**

Reviewing is proofreading. Every PR is a galley, every decision an agent needs from you is a query pencilled in the margin, and home is the stack of open queries. crew is a terminal app the owner keeps open all day beside their editor, so the system is one type size in the user's own terminal font. Rank comes from weight, UPPERCASE labels, dimming and hairline rules, never from boxes. The right-hand pane is Claude Code itself; crew frames it but never restyles it.

Density is a working proof: aligned columns, one row per PR, one blank row between sections. Quiet is the default and emphasis is rationed. Red pencil appears only where something waits on you, so a glance at the margin answers "is anything mine?". It replaced a tree-and-box sidebar and a home of stacked tables that the owner described as "a mainframe".

**Key Characteristics:**
- One type size; hierarchy by bold, CAPS graphite labels, dim and rules.
- No boxes, no tree guides, no emoji; every state is a plain word.
- Red pencil is reserved for queries waiting on you.
- A dark proof-ink page with the rail one step lifted.
- Answered things are struck through before they leave.

## Colors

Dark proof-ink grounds, proof-paper text, graphite for everything secondary, and a single red pencil that only marks what's waiting on you.

### Primary
- **Red Pencil** (#e2674d): the margin bar ▍ and the word "query" beside every item with an open decision, the open-query count, and an agent's "N queries". Lifted from the planned #c8553d so it clears 4.5:1 on both inks.

### Secondary
- **Franked** (#8fa58a): answered, dismissed, done and merged. It always pairs with a struck-through graphite title, and it never competes with the pencil.

### Neutral
- **Proof Ink** (#16181d): the page ground for home, the answer screen and every tmux pane, Claude Code included (tmux window-style).
- **Rail Ink** (#1b1e24): the rail and the status bar, one step lifted from the page so the shell reads as a rail.
- **Selection** (#262a33): the highlighted row in the rail and in home lists.
- **Hairline** (#2a2d35): rules between home blocks, the rule above the rail's key hints, pane borders and scrollbars.
- **Code Slab** (#22252c): fenced code in agent write-ups, with no padding rows.
- **Proof Paper** (#e8e6e1): all primary text, at 13:1 or better on every ground.
- **Graphite** (#8b92a5): metadata, agent labels, quiet state words, "Suggested" answers and key-hint words. At least 4.6:1 on every ground, including selection.

### Named Rules
**The Red Pencil Rule.** Red marks one thing only: a query waiting on you. Severity, errors in prose and decoration never borrow it; a "blocking" finding is bold paper.

**The Contrast Floor Rule.** Every text colour clears 4.5:1 on proof ink, rail ink and selection. The one exception is red pencil on a selected row, at 4.3:1, which is always bold.

## Typography

**Display Font:** none. crew uses the user's terminal font at one size.
**Body Font:** the user's terminal font.

**Character:** a single size, as on a typewritten proof. Bold, case and dimming do the ranking.

### Hierarchy
- **Title** (bold, proof paper): the PR number and title of an unread review, home headings ("Open queries", "Since you last looked") and the question of an open query.
- **Label** (bold, UPPERCASE, graphite): agent sections in the rail (CI-DOCTOR, PR-REVIEW) and section labels on the answer screen (WHAT THIS PR DOES, HOW I REVIEWED IT, FINDINGS).
- **Body** (regular, proof paper): titles you have already read, prose, options.
- **Meta** (regular, graphite): agent names, times, "Suggested", state words such as "queued", "looks good" and "paused".

### Named Rules
**The One Size Rule.** Nothing is ranked by size. If something needs more weight, it gets bold, CAPS or a rule.

**The Plain Word Rule.** Every state is a word ("query", "chat", "queued", "reviewing", "changed", "answered", "paused"); colour only reinforces it. No ○ ● ◐ ? › glyph codes.

## Layout

The tmux frame has a resizable rail on the left (24–160 cells, 42 by default, remembered) and the live session on the right. The status bar sits at the bottom on rail ink.

- **Rail rows** use fixed columns:
  - a 2-cell margin, holding the red ▍ when the row waits on you;
  - a 6-cell PR number;
  - the title, which takes the rest;
  - a right-aligned state word.
- The state column width is computed once per refresh across the whole rail, so titles end in the same column in every section.
- When the title column drops under 24 cells, titles lose their `[AUT-…]` ticket tag and their `type(scope):` prefix.
- Unpicked PRs fold into "N more to pick".
- One blank row separates each agent section.
- Home sits 3 cells in from the edge and is capped at 112 cells wide. Each query block takes:
  - one line for the number, title and agent;
  - one bold line for the question;
  - "Suggested", wrapped to at most two lines with a hanging indent.
- Blocks are separated by hairline rules.
- The key hints at the foot of the rail pack whole "key word" pairs to the rail's width and never split a pair.

## Elevation & Depth

None. Depth is tonal only: page ink, rail ink one step lighter, selection one step lighter again. Nothing has a shadow, a box or a border except the pane borders tmux draws.

### Named Rules
**The No-Box Rule.** No panels, frames or tree guides. Sections are separated by blank rows and hairline rules.

## Shapes

Square terminal cells only. The one recurring mark is the red-pencil margin bar ▍, which runs the full height of a query block. The one recurring stroke is strikethrough on answered items.

## Components

### Navigation (the rail)
- **Style:** an OptionList on rail ink, with no borders. The selected row is filled with selection colour.
- **Order:** "Open queries" with its count first; then each agent's CAPS label with a right-aligned summary ("2 queries", "running", "every 30m", "paused"); then that agent's PR rows; then "N more to pick"; then "New agent".
- **States:** query (bold red pencil), reviewing, changed (bold paper), chat (a live session is open), a verdict word (graphite), queued, not picked, and answered or dismissed (franked, with the title struck through for 60 seconds).

### Open-query block (signature component)
- A red ▍ runs down every line of the block.
- Lines: number, title and agent; the bold question; then "Suggested" with the agent's recommendation.
- One block per PR, with all of its queries beneath it.
- When you answer a query, its line turns into "answered" in franked green with the question struck through, and stays on screen until the view next opens.

### Answer screen
- **Order:** the PR number and title (bold); a graphite meta line; CAPS labels with flush-left markdown under each; findings set as severity word, title and location; a hairline rule; then each query.
- **Query:** "▍ QUERY n OF m"; the question; its context; numbered options; "Suggested"; and the key words.
- Markdown headings are flush left. Code sits on the code slab.

### Status bar
Rail ink. "crew" in bold paper on the left. On the right, plain words: "3 open queries", "pr-review reviewing 12307", and the key hints.

## Do's and Don'ts

### Do:
- **Do** put what's waiting on the owner first, with a red ▍ in the margin.
- **Do** write every state as a word and right-align it in the rail's single state column.
- **Do** strike through answered items in franked green before they leave.
- **Do** keep headings flush left and rank them by bold and CAPS.
- **Do** keep all text at or above 4.5:1 on proof ink, rail ink and selection.

### Don't:
- **Don't** draw boxes, panels or tree guides; use blank rows and hairline rules.
- **Don't** use red for anything except a query waiting on the owner.
- **Don't** use glyph or emoji icons (○ ● ◐ ✓ ✗ 📥) as states.
- **Don't** let Rich's default monokai slab or centred headings through; the galley overrides replace them.
