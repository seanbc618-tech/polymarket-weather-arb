# Agent Handoff MCP (Codex ↔ Grok)

Cross-agent handoffs for this repo go through files under `docs/`. Codex is
configured with a local MCP server named **`agent-handoff`** that can only
access this directory tree (not the whole repo).

## What is configured

On this machine, Codex global config (`~/.codex/config.toml`) includes:

```toml
[mcp_servers.agent-handoff]
command = "npx"
args = [
  "-y",
  "@modelcontextprotocol/server-filesystem",
  "/path/to/polymarket-weather-arb/docs",
]
```

Manage it with:

```bash
codex mcp list
codex mcp get agent-handoff
codex mcp remove agent-handoff   # only if intentionally removing
```

First launch of `npx @modelcontextprotocol/server-filesystem` needs network to
download the package; later starts use the local cache.

## Who writes, who reads

| Role | Action |
|------|--------|
| **Grok** (and other workers) | Write durable outputs under `docs/` with normal file tools |
| **Codex** | Prefer MCP tools on `agent-handoff` to list/read those outputs |

Codex may also edit/write under `docs/` via the same MCP when updating a review
or task note. Source code and runtime data stay outside this root on purpose.

## Canonical paths

| Path | Contents |
|------|----------|
| `docs/reviews/` | Runtime monitors, audits, soak reports, retro writeups |
| `docs/worker-tasks/` | Task assignments and acceptance criteria for a worker |
| `docs/agent-worker-standards.md` | Authoritative ownership / handoff contract |
| `docs/claude-code-handoff.md` | Longer product/safety handoff for Claude Code |
| `docs/runbooks/` | Operator procedures |
| `docs/strategy/` | Strategy notes |

## Naming

Prefer dated, searchable names:

```text
docs/reviews/runtime-monitor-YYYY-MM-DD-<worker-or-topic>.md
docs/worker-tasks/YYYY-MM-DD-<worker>-<short-topic>.md
```

Examples already in tree:

- `docs/reviews/runtime-monitor-2026-07-23-grok-evening.md`
- `docs/worker-tasks/2026-07-23-grok-evening-production-monitor.md`

## Suggested report header

Put a short YAML frontmatter (or an equivalent first heading block) so Codex
can scan quickly:

```markdown
---
worker: grok
kind: runtime-monitor   # runtime-monitor | audit | soak | task | handoff
window_start: 2026-07-23T11:00:00Z
window_end: 2026-07-23T19:00:00Z
verdict: CONTINUE       # CONTINUE | STOP | ESCALATE | COMPLETE
primary_findings: 0p0 1p2
real_trading_mutation: not_executed
---

# Summary

One short paragraph: what was observed and what the next agent should do.
```

Body content should still satisfy **Worker Handoff Format** in
`docs/agent-worker-standards.md` (objective, reuse, mutations, tests, commit,
real-trading mutation statement).

## Codex usage checklist

When continuing after Grok (or any peer):

1. `list_directory` / `list_directory_with_sizes` on `docs/reviews` and `docs/worker-tasks`.
2. Open the newest matching report for the topic (prefer today's date prefix).
3. Read `docs/agent-worker-standards.md` if ownership or gates are unclear.
4. Do **not** treat chat transcripts as source of truth; only these files (plus
   git and live runtime checks) are durable handoff.

MCP tool names (filesystem server) include:

- `list_allowed_directories`
- `list_directory` / `directory_tree`
- `read_text_file` / `read_file`
- `search_files`
- `write_file` / `edit_file` (docs only)

Exact names are namespaced by the Codex client (often `agent-handoff__…`).

## Grok usage checklist

When finishing a monitor or slice that another agent may continue:

1. Write or update a file under `docs/reviews/` or `docs/worker-tasks/`.
2. Include the worker handoff fields from the standards doc.
3. Prefer one clear `verdict` and explicit “real trading mutation: not executed”
   (or executed, with detail).
4. Do not rely on Codex reading your session transcript; put conclusions in
   `docs/`.

## Scope and limits

- Root is **`docs/` only** — not `src/`, not SQLite, not `.env`, not `~/.grok`.
- This is a **file bus**, not live agent-to-agent chat.
- Moving the repo path requires updating the `args` path in
  `~/.codex/config.toml` (or re-running `codex mcp add`).
- Optional later upgrade: a small custom MCP with `list_handoffs` /
  `get_latest` if bare filesystem search becomes noisy.
