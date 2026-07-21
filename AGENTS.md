# AGENTS.md — guidance for AI coding agents in this repo

This file gives ZCode (and other AI coding agents) the rules of engagement.

## When to request approval via hermes-approve

The `hermes-approve` MCP server exposes a `request_approval` tool. Calling it
sends a message to a dedicated Telegram bot that the human carries in their
pocket, with [✅ Allow] / [❌ Deny] buttons. The call blocks until the human
responds or 15 minutes elapse (auto-deny on timeout).

**Always call `request_approval()` BEFORE doing any of the following:**

- `git push --force` / `git rebase` that rewrites history already pushed to a remote
- `rm -rf` or recursive delete outside `C:/Users/Bew/ZCodeProject`
- Editing `.env` files, secret files, or credentials
- `docker compose restart | kill | down` against services currently in use
  (e.g. n8n, augment)
- Sending data to external services when the data was not previously public
  (publishing APIs, webhooks, posting to chat)

**You MAY request approval (use your judgment):**

- Deleting non-temp files (anything outside `tmp_test/`)
- Installing new top-level packages (npm/pip at repo root)
- Editing Hermes or ZCode config files

**Do NOT request approval for:**

- Normal code edits inside the repo
- Running tests, linters, type checkers
- Creating/editing files under `tmp_test/`
- Reading files (always safe)

When in doubt, request approval. The human would rather click "Deny" than
discover afterwards that you did something irreversible.

## How to call the tool

```python
# Python-ish pseudocode for any MCP-aware agent
result = request_approval(
    action="git push --force origin phase-1d-2/session-memory-hook",  # ≤ 200 chars
    risk="destructive",                                                # low|moderate|destructive
    summary="Overwrite remote because the previous push contained a secret.",  # ≥ 20 chars
    timeout_seconds=900,                                               # 60–1800, default 900
)
# result is JSON. Check result.decision: "allow" | "deny"
# If "deny" (including timeout auto-deny), DO NOT proceed — propose an alternative.
```

## Other repo conventions

- Branch naming: `<phase>-<n>/<short-slug>` (e.g. `phase-2/telegram-approve-mcp`)
- Commit style: `type(scope): summary` — see `git log --oneline` for examples
- Don't commit secrets. The `.secrets/` directory is gitignored.
- Don't commit runtime state. The `.approve/` directory is gitignored.
- Match surrounding code style: `ruff` and `mypy` configs are at repo root.
