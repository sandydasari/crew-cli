# Product

<!-- impeccable:product-schema 1 -->

## Platform

terminal (macOS). A Python/Textual app running inside its own tmux server; not web, not native mobile.

## Users

One engineer (the owner) who keeps crew open all day in a terminal beside their editor and checks in between coding. They review teammates' PRs and shepherd their own, at a company where PR volume is high (28+ open PRs touching them at once).

## Product Purpose

crew runs the owner's own Claude Code agents on a schedule and turns their work into a short list of calls only the owner can make. Agents review PRs, triage feedback on the owner's PRs and diagnose CI, read-only; nothing is posted, pushed or changed until the owner answers a decision. Success: the owner opens crew, sees what is waiting on them, clears it, and gets back to coding.

## Positioning

Every agent run is a real, resumable Claude Code session per item. The owner can drop into any PR's own thread and keep talking to the agent that reviewed it, with full context, instead of reading a report.

## Operating Context

- Lives in a terminal next to the editor, all day; 16:9 laptop screens and full-screen external monitors.
- Left: sidebar (Textual). Right: live Claude Code sessions swapped in per agent, PR thread or decision (tmux panes).
- GitHub via `gh`; macOS notifications; schedules via launchd.
- Agents today: pr-review (PRs others asked the owner to review), my-prs (the owner's own PRs), ci-doctor (failing CI on the owner's PRs). Owner adds more from the UI.

## Capabilities and Constraints

- Owner picks which PRs an agent works on; new PRs only land in the list.
- Decisions always explain the item and the agent's approach before asking.
- Items close automatically when merged/closed; owner can close a chat (keep the item) or remove an item for good.
- Must render in any truecolor terminal, follow the terminal's own font; width is user-resizable (24–160 cols for the sidebar).
- Claude Code owns the right-hand pane's look; crew cannot restyle it, only frame it.

## Brand Commitments

Name: crew. Agent replies read like normal Claude Code replies and are passed through /humanizer.

## Evidence on Hand

Real data only: live PRs, decisions, runs and usage from ~/.crew/state.db. No invented metrics.

## Product Principles

1. What's waiting on me comes first; everything else is one keystroke away.
2. Nothing outward-facing happens without the owner's answer.
3. Plain words over codes: every symbol must be readable without a legend.
4. Quiet by default; only things that need the owner get emphasis.
5. Conversations, not reports: every item is a thread you can enter.
