# Phase 2 — Telegram Approve MCP Server (Design Spec)

**Date**: 2026-07-21
**Author**: Bew + ZCode (brainstorming)
**Status**: Approved design — ready for implementation plan
**Branch target**: `phase-2/telegram-approve-mcp` (stacked on `phase-1d-2/session-memory-hook` or `main` after 1D-2 merges)

---

## 1. Goal

Build a custom **MCP server** that gives the ZCode agent an opt-in tool to request human approval via Telegram before performing risky actions.

**Flow**: ZCode agent calls MCP tool → bot sends message + inline buttons to Telegram → human taps Allow / Deny → decision returns to agent → agent continues or aborts.

**Non-goals** (explicitly out of scope for Phase 2):
- Auto-block guardrail via ZCode PreToolUse hook (deferred to Phase 3)
- Multi-user support (single allowlist UID only)
- "Remember this decision" / session-scoped allow (deferred)
- Hermes approval pipeline integration (Hermes keeps its own system; this is a parallel path for ZCode)
- Slack/Discord/email routing (Telegram only)

---

## 2. Requirements (locked via brainstorming)

| # | Requirement | Decision |
|---|---|---|
| 1 | Flow direction | **ZCode requests → Telegram approves** (not Hermes-driven) |
| 2 | Trigger | **Opt-in** (agent calls MCP tool explicitly; no auto-block) |
| 3 | Telegram bot | **New bot** dedicated to approvals (not the Hermes bot) |
| 4 | Sync model | **Sync block** — tool blocks until decision or timeout; auto-deny on timeout |
| 5 | Payload scope | **Metadata only** — action label, risk level, agent-written summary. No command args, file paths beyond action string, file content, or diffs |

---

## 3. Architecture (Approach A — stdio + in-process poller)

```
┌─────────┐   stdin/stdout (MCP)   ┌────────────────────┐
│  ZCode  │ ◀────────────────────▶ │   MCP server       │
│  agent  │                        │   (Python, FastMCP)│
└─────────┘                        └─────────┬──────────┘
                                             │ writes
                                             ▼
                                   ┌──────────────────────┐
                                   │  in-process store    │
                                   │  + audit log         │
                                   └──────────┬───────────┘
                                              ▲
                                   ┌──────────┴───────────┐
                                   │ Poller thread        │
                                   │ (getUpdates long-poll│
                                   │  every ~30s)         │
                                   └──────────┬───────────┘
                                              │ HTTPS
                                              ▼
                                   ┌──────────────────────┐
                                   │  Telegram Bot API    │
                                   │  (@bew_approve_bot)  │
                                   └──────────────────────┘
```

### 3.1 File layout

```
C:\Users\Bew\ZCodeProject\
├── scripts\
│   └── hermes-approve-mcp.py        ← MCP server (FastMCP, stdio)
├── .secrets\
│   └── approve-bot.env              ← bot token + allowed UID (gitignored)
├── .approve\                        ← runtime state (gitignored)
│   ├── pending.jsonl                ← append-only audit log
│   ├── state.json                   ← last_update_id cursor
│   └── approve.log                  ← troubleshooting log (rotated)
├── tests\
│   ├── unit\                        ← pure-function tests
│   └── integration\                 ← mock Telegram API
├── AGENTS.md                        ← tells agent when to call tool
└── .gitignore                       ← new: ignore .secrets/, .approve/
```

### 3.2 ZCode MCP registration (`~/.zcode/cli/config.json`)

```json
{
  "mcp": {
    "servers": {
      "hermes-approve": {
        "type": "stdio",
        "command": "C:\\HermesHooks\\python.exe",
        "args": ["C:\\Users\\Bew\\ZCodeProject\\scripts\\hermes-approve-mcp.py"],
        "env": {
          "APPROVE_BOT_ENV": "C:/Users/Bew/ZCodeProject/.secrets/approve-bot.env",
          "APPROVE_STATE_DIR": "C:/Users/Bew/ZCodeProject/.approve"
        },
        "enabled": true,
        "timeoutMs": 600000
      }
    }
  }
}
```

- Uses `C:\HermesHooks\python.exe` (space-free path, same trick as Phase 1 hook)
- `timeoutMs: 600000` (10 min) — upper bound for sync block
- User-scope config (works across all ZCode sessions for this machine)

### 3.3 Components

| Component | Responsibility | Thread |
|---|---|---|
| `FastMCP` registry | Registers 2 tools: `request_approval`, `ping` | main |
| `TelegramPoller` | Long-polls `getUpdates` every ~30s; dispatches callback_query | background daemon |
| `PendingStore` | In-mem dict of pending requests + threading.Event per id; guarded by lock | shared |
| `ApprovalRequest` | dataclass: `id, action, risk, summary, status, created_at, expires_at, decision` | — |
| `TelegramClient` | Thin wrapper over `httpx` for `sendMessage`, `editMessageText`, `answerCallbackQuery`, `getUpdates` | called from both threads |

### 3.4 Threading model

```
[main thread]                       [poller daemon]
  │                                   │
  │ FastMCP init                      │ spawn at server start
  │ spawn poller ────────────────────▶│ loop:
  │                                   │   getUpdates(offset=last+1, timeout=30)
  │ request_approval() tool called:   │   for each update:
  │  ├─ validate input                │     if message: reply "use buttons"
  │  ├─ req_id = gen_id()             │     if callback_query: dispatch
  │  ├─ expires_at = mono + timeout   │       parse "ap:<id>:<decision>"
  │  ├─ append pending.jsonl          │       verify from.id in allowlist
  │  ├─ sendMessage + buttons         │       store.set_resolution(id, decision)
  │  ├─ event = Event(); store[id]    │       answerCallbackQuery (toast)
  │  ├─ event.wait(timeout)           │       editMessageText (remove buttons)
  │  ├─ if timeout: auto-deny         │       commit state.json
  │  └─ return decision               │   on exception: log + sleep 5s + retry
```

---

## 4. Data Flow & Sequences

### 4.1 Happy path (user taps Allow)

```
agent          MCP main              Poller            Telegram API
  │                │                    │                   │
  │ request_approval│                   │                   │
  ├───────────────▶│ validate           │                   │
  │                │ gen req_id         │                   │
  │                │ append audit log   │                   │
  │                │ sendMessage ───────────────────────────▶
  │                │ ◀────────────── msg {message_id: 42}   │
  │                │ event.wait(900) ◀━━ BLOCK ━━━━━━━━━━━━ │
  │                │                    │ getUpdates(long)──▶
  │                │                    │ ◀── callback_query │
  │                │                    │   {data:"ap:8f3a2:allow",
  │                │                    │    from.id:UID}    │
  │                │                    │ verify allowlist ✓ │
  │                │                    │ answerCallbackQuery("✅ Approved")
  │                │                    │ editMessageText("✅ ALLOWED")
  │                │                    │ store.set_resolution → event.set()
  │                │ ◀━━━━━━━━━━━━━━━━━━┤                   │
  │                │ return JSON allow  │                   │
  │ ◀──────────────│                    │                   │
  │ agent performs action               │                   │
```

### 4.2 Timeout auto-deny

- `event.wait(timeout)` returns `False` (not set) → tool writes `{"event":"resolved","reason":"timeout","auto":true}` to audit log → calls `editMessageText` to update Telegram message to "⌛ AUTO-DENIED" → returns `{"decision":"deny","reason":"timeout","auto":true}`.
- Uses `time.monotonic()` for deadline calculation (immune to NTP clock jumps). `datetime.now()` is used only for display + audit log.

### 4.3 State machine

```
                  ┌──────────┐
                  │ created  │ (in-memory only)
                  └────┬─────┘
                       │ sendMessage OK
                       ▼
                  ┌──────────┐
                  │ pending  │ ◀── re-render on edit
                  └────┬─────┘
                       │ user click / timeout
                       ▼
                  ┌─────────────┐
                  │  resolved   │ (final; allow | deny | auto-deny)
                  └─────────────┘
```

### 4.4 Telegram getUpdates cursor (`state.json`)

```json
{
  "last_update_id": 4827,
  "last_heartbeat_at": "2026-07-21T19:42:03Z"
}
```

- Persisted after every processed batch
- Next call: `getUpdates(offset=last_update_id + 1, timeout=30)` — Telegram deletes older updates automatically

### 4.5 Stale callback handling at boot

1. On boot, read `last_update_id` from `state.json`
2. First `getUpdates` call uses `offset = last + 1` → Telegram drops everything older
3. If `state.json` is missing (fresh install): first batch may include stale callback_query from previous session → **drop silently** if id not in store, answer toast "⌛ Expired", commit offset

### 4.6 Tool input/output contract

**Tool signature**:
```python
@mcp.tool()
def request_approval(
    action: str,           # required, ≤ 200 chars
    risk: str,             # "low" | "moderate" | "destructive"
    summary: str,          # required, ≥ 20 chars (forced detail), ≤ 1000 chars
    timeout_seconds: int = 900  # default 15 min, range [60, 1800]
) -> str:
    """Request human approval via Telegram. Blocks until decision or timeout.
    Returns JSON with 'decision' field. Always returns JSON — never raises.
    """
```

**Return values**:

```json
// User allowed
{"decision":"allow","id":"ap_8f3a2","responded_at":"...","responded_by":5967541638,"elapsed_seconds":132}

// User denied explicitly
{"decision":"deny","reason":"user_denied","id":"ap_8f3a2","responded_at":"...","responded_by":5967541638}

// Timeout (auto-deny)
{"decision":"deny","reason":"timeout","auto":true,"id":"ap_8f3a2","timeout_seconds":900}

// Input validation failed (no Telegram send)
{"error":"validation_failed","field":"summary","message":"summary must be ≥ 20 chars"}

// Server misconfigured
{"error":"server_misconfigured","message":"APPROVE_BOT_TOKEN env not set"}

// Telegram send failed (after retry)
{"error":"telegram_send_failed","message":"HTTP 429 after retry"}
```

### 4.7 Audit log format (`pending.jsonl`)

Append-only, 1 JSON per line, every lifecycle event:

```jsonl
{"event":"created","id":"ap_8f3a2","action":"git push --force ...","risk":"destructive","summary":"...","created_at":"...","expires_at":"..."}
{"event":"sent","id":"ap_8f3a2","telegram_message_id":42,"sent_at":"..."}
{"event":"resolved","id":"ap_8f3a2","decision":"allow","reason":"user_allowed","responded_by":5967541638,"resolved_at":"..."}
```

Timeout variant: `{"event":"resolved","id":"...","decision":"deny","reason":"timeout","auto":true,...}`

**`reason` vocabulary (consistent across return JSON and audit log)**: `user_allowed`, `user_denied`, `timeout` (auto). Decisions: `allow`, `deny`.

---

## 5. Telegram Message Format & Button Routing

### 5.1 `callback_data` (≤ 64 bytes — Telegram limit)

Format: `ap:<id>:<decision>` where `id` is a 6–12 char hex short ID (not UUID v4 — saves bytes for future expansion).

Examples: `ap:8f3a2:allow` (16 bytes), `ap:abc123def456:deny` (20 bytes).

### 5.2 Inline keyboard

Two buttons side by side (1 row × 2 cols):

```
[✅ Allow]   [❌ Deny]
```

Rationale: one button forces text reply (slow); three buttons unnecessary for Phase 2 (no "session allow" scope yet).

### 5.3 Risk visual mapping

| risk | emoji | label |
|---|---|---|
| `low` | 🟢 | LOW |
| `moderate` | 🟡 | MODERATE |
| `destructive` | 🟠 | DESTRUCTIVE |

### 5.4 Message format (HTML parse_mode)

**Pending state** (sendMessage):
```
🟠 Approval request

Action: git push --force origin phase-1d-2/...
Risk:   DESTRUCTIVE
Time:   2026-07-21 19:42 (Bangkok UTC+7)

Force-push branch ที่มี 5 commits ใหม่ไปที่ origin
เพื่อเขียนทับ history ที่ push ไปเมื่อวาน
(commit 8f3a2c1 จะหายไปจาก remote)

⏱ Auto-deny ใน 15:00
```

All user-controlled fields (`action`, `summary`) are HTML-escaped via `html.escape()`.

**Resolved: allow**:
```
...
✅ ALLOWED by Bew at 19:44 (after 2m 12s)
```
(buttons removed via `reply_markup=None`)

**Resolved: deny (user)**:
```
...
❌ DENIED by Bew at 19:44
```

**Resolved: timeout (auto-deny)**:
```
...
⌛ AUTO-DENIED (timeout 15:00)
```

### 5.5 `answerCallbackQuery` toast

| Situation | Toast |
|---|---|
| Allow success | `✅ Approved` |
| Deny success | `❌ Denied` |
| Already resolved | `⚠️ Already resolved` |
| Unauthorized user | `⛔ Unauthorized` |
| Request expired | `⌛ Expired` |

### 5.6 Routing & authorization

```
callback_query arrives:
  │
  ├── from.id NOT in ALLOWED_UIDS?
  │     └── answerCallbackQuery("⛔ Unauthorized") + log warning
  │
  ├── parse "ap:<id>:<decision>"
  │     ├── format invalid → drop silently + log
  │     ├── <id> not in store → answerCallbackQuery("⌛ Expired")
  │     ├── <id> already resolved → answerCallbackQuery("⚠️ Already resolved")
  │     └── <id> pending:
  │           ├── store.set_resolution(id, decision)  ← lock-guarded
  │           ├── answerCallbackQuery(toast)
  │           └── editMessageText(final state)
  │
  └── commit last_update_id → state.json
```

### 5.7 Race conditions

**Double-click Allow → Deny**: poller processes sequentially within a batch; first wins via lock, second sees "resolved" → "Already resolved" toast.

**Timeout vs click**: `set_resolution()` acquires lock; first caller wins, second returns `False`.

**Concurrency**: store supports multiple simultaneous pending requests (each tool call blocks on its own `threading.Event`). Each request = separate Telegram message (no shared-message edit complexity).

---

## 6. Error Handling

**Principle**: tool never raises; always returns JSON. Mirrors Phase 1D-2 hook's "must not block session" rule.

### 6.1 Error categories

| # | Category | Example | Tool behavior | Return |
|---|---|---|---|---|
| 1 | Input validation | summary < 20 chars | no Telegram send | `{"error":"validation_failed",...}` |
| 2 | Server misconfig | bot token missing | no Telegram send | `{"error":"server_misconfigured",...}` |
| 3 | Telegram API error | network down, 401, 429 | 1 retry after 2s, then fail | `{"error":"telegram_send_failed",...}` |
| 4 | Poller dies mid-wait | process crash, ZCode cancels | timeout auto-deny | `{"decision":"deny","reason":"timeout",...}` |
| 5 | `editMessageText` fails | user deleted message | log warning, don't abort | decision returned normally |
| 6 | `answerCallbackQuery` fails | network glitch | log warning, don't abort | resolution still happens |

### 6.2 Validation rules

```python
def validate(action, risk, summary, timeout_seconds) -> Optional[Error]:
    if not action or not action.strip():
        return Error("action_required", "action must not be empty")
    if len(action) > 200:
        return Error("action_too_long", "action must be ≤ 200 chars")
    if risk not in {"low", "moderate", "destructive"}:
        return Error("risk_invalid", "risk must be one of: low, moderate, destructive")
    if len(summary.strip()) < 20:
        return Error("summary_too_short",
                     "summary must be ≥ 20 chars (explain WHY this needs approval)")
    if len(summary) > 1000:
        return Error("summary_too_long", "summary must be ≤ 1000 chars")
    if timeout_seconds < 60:
        return Error("timeout_too_short", "min 60 seconds")
    if timeout_seconds > 1800:
        return Error("timeout_too_long", "max 1800 seconds (30 min)")
    return None
```

### 6.3 Telegram API retry policy

```python
def telegram_call(method, payload, retries=1):
    for attempt in range(retries + 1):
        try:
            r = httpx.post(f"{BASE}/{method}", json=payload, timeout=10)
            if r.status_code == 200:
                return r.json()["result"]
            if r.status_code in (400, 401, 403):
                raise TelegramFatal(f"HTTP {r.status_code}: {r.text}")
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise TelegramFatal(f"HTTP {r.status_code} after retry: {r.text}")
        except httpx.RequestError as e:
            if attempt < retries:
                time.sleep(2 ** attempt)
                continue
            raise TelegramFatal(f"network: {e}")
```

HTTP 429 honors `retry_after` field when present.

### 6.4 Edge case matrix

| # | Scenario | Resolution |
|---|---|---|
| E1 | Process crash mid-wait | Acceptable failure. Telegram message stays pending; next boot's poller drops stale callbacks with "Expired" toast. Audit log shows no `resolved` event. |
| E2 | ZCode cancels tool mid-wait | Tool call dies; pending request may still be resolved by user click but no consumer. Logged as "resolved without consumer". |
| E3 | Telegram 429 rate limit | Honor `retry_after`; if still failing, return error. |
| E4 | User sends text (not button) | Reply "⚠️ Use inline buttons below approval requests."; do not resolve anything. |
| E5 | Stale callback at boot | Drop silently + commit offset (see 4.5). |
| E6 | Extended Telegram outage | sendMessage fails → return error. Poller retries with exponential backoff capped at 60s. Local timeout auto-deny still fires. |
| E7 | System clock drift (NTP) | Use `time.monotonic()` for timeout deadlines; `datetime.now()` for display only. |
| E8 | `last_update_id` in JSON but not in store | Telegram's offset mechanism already drops older updates; not an issue. |
| E9 | Bot blocked by user | `sendMessage` returns 403 → return `{"error":"bot_blocked",...}`. |
| E10 | Poller thread crash | Each loop iteration is try/except-wrapped; on exception → log + sleep 5s + retry. Tool pre-flight checks `poller.is_alive()` and respawns if dead. |

### 6.5 Logging strategy (3 channels)

| Channel | Content | Routing |
|---|---|---|
| MCP logging API (stdout) | Lifecycle INFO events only | ZCode MCP runtime captures |
| stderr | DEBUG-level traces | ZCode MCP log file |
| `.approve/approve.log` | Audit + troubleshooting; rotated at 1MB, keep last 5 | File |

Levels: `INFO` (lifecycle), `WARNING` (recoverable), `ERROR` (fatal), `DEBUG` (off by default).

### 6.6 Health-check tool

```python
@mcp.tool()
def ping() -> str:
    """Health check: server status, bot info, poller state."""
    return json.dumps({
        "status": "ok",
        "bot_username": "bew_approve_bot",
        "poller_alive": poller.is_alive(),
        "pending_count": len(store._pending),
        "last_getupdate_at": poller.last_call_at,
        "last_error": poller.last_error,
        "uptime_seconds": uptime()
    }, indent=2)
```

---

## 7. Agent Guidance (`AGENTS.md`)

A new section will be added to `C:\Users\Bew\ZCodeProject\AGENTS.md` (or `CLAUDE.md`) telling the ZCode agent when to call `request_approval`:

```markdown
## When to request approval via hermes-approve

**Always call `request_approval()` BEFORE**:
- `git push --force` / `git rebase` that rewrites already-pushed history
- `rm -rf` outside `C:/Users/Bew/ZCodeProject`
- Editing `.env`, secret files, credentials
- `docker compose restart/kill/down` of services in use
- Sending data to external services (publishing APIs, webhooks)

**You MAY request approval (use judgment)**:
- Deleting non-temp files
- Installing new packages (npm/pip)
- Editing Hermes or ZCode config

**Do NOT request approval for**:
- Normal code edits inside the repo
- Running tests / linters
- Creating/editing files in `tmp_test/`
```

---

## 8. Testing Strategy

### 8.1 Test pyramid

```
                  ┌──────────────┐
                  │  E2E manual  │  3 scenarios (real bot)
                  └──────────────┘
                ┌──────────────────┐
                │ Integration test │  ~8 tests (mock Telegram API)
                └──────────────────┘
              ┌──────────────────────┐
              │    Unit test         │  ~15 tests (pure functions)
              └──────────────────────┘
            ┌──────────────────────────┐
            │   Static / lint          │  ruff + mypy
            └──────────────────────────┘
```

### 8.2 Unit tests (pure functions, no mocks)

| Test | Function | Input → expected |
|---|---|---|
| `test_validate_action_empty` | `validate()` | `action=""` → error "action_required" |
| `test_validate_action_too_long` | `validate()` | `action="x"*201` → error "action_too_long" |
| `test_validate_risk_invalid` | `validate()` | `risk="critical"` → error "risk_invalid" |
| `test_validate_summary_short` | `validate()` | `summary="abc"` → error "summary_too_short" |
| `test_validate_summary_long` | `validate()` | `summary="x"*1001` → error "summary_too_long" |
| `test_validate_timeout_bounds` | `validate()` | `timeout_seconds=30` or `2000` → error |
| `test_validate_happy` | `validate()` | all fields valid → `None` |
| `test_format_request_destructive` | `format_request()` | risk=destructive → "🟠" + "DESTRUCTIVE" |
| `test_format_request_html_escape` | `format_request()` | action with `<script>` → escaped |
| `test_format_resolved_allow` | `format_resolved()` | allow → "✅ ALLOWED" |
| `test_format_resolved_timeout` | `format_resolved()` | timeout → "⌛ AUTO-DENIED" |
| `test_parse_callback_valid` | `parse_callback()` | `"ap:8f3a2:allow"` → `("8f3a2","allow")` |
| `test_parse_callback_invalid` | `parse_callback()` | `"foo:bar"` → `None` |
| `test_short_id_uniqueness` | `gen_id()` | 10,000 generated IDs all unique |
| `test_short_id_length` | `gen_id()` | 6 ≤ len ≤ 12 |

### 8.3 Integration tests (mock Telegram via `httpx.MockTransport`)

| Test | Scenario | Asserts |
|---|---|---|
| `test_request_approval_happy` | user taps Allow in mock | tool blocks → event.set → returns allow JSON |
| `test_request_approval_deny` | user taps Deny in mock | returns deny JSON |
| `test_request_approval_timeout` | no callback ever | returns auto-deny JSON after `timeout_seconds` (use 1s timeout) |
| `test_already_resolved_click` | Allow then Deny in one batch | second toast = "Already resolved" |
| `test_unauthorized_user` | from.id != allowlist | toast "Unauthorized", no resolution |
| `test_validation_short_summary` | summary 3 chars | returns validation error, no Telegram call |
| `test_stale_callback_at_boot` | store empty, callback arrives | drop silently, offset committed |
| `test_state_json_persists` | run + restart mock | `last_update_id` survives |

### 8.4 Concurrency tests

| Test | Scenario |
|---|---|
| `test_double_click_allow_deny` | 2 callback_query in same batch → first wins |
| `test_timeout_vs_click_race` | timeout + click concurrent → lock decides |
| `test_concurrent_pending_requests` | 5 simultaneous requests → all resolve independently |

### 8.5 E2E manual scenarios (real bot)

| # | Scenario | Steps | Expected |
|---|---|---|---|
| M1 | Happy path | Tell agent "force-push this branch and request approval" | Telegram message arrives, tap Allow, agent pushes |
| M2 | Deny path | Same command, tap Deny | Agent says "I won't do it" |
| M3 | Timeout path | Same command, don't tap for 15 min | Auto-deny, agent offers alternative |

### 8.6 Test infrastructure

```python
# tests/conftest.py
@pytest.fixture
def mock_telegram():
    """Mock httpx transport simulating Telegram API."""
    def handler(request: httpx.Request) -> httpx.Response:
        url = request.url.path
        if "sendMessage" in url:
            return httpx.Response(200, json={
                "ok": True,
                "result": {"message_id": 42, "date": 0, "chat": {"id": 1}}
            })
        if "getUpdates" in url:
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))

@pytest.fixture
def fast_store():
    return PendingStore(timeout_seconds=1)
```

### 8.7 Coverage targets

- Core logic (validate, format, parse, store): **95%+**
- Telegram client (mocked): **80%+**
- MCP wiring: **50%** (manual E2E is primary verification)

---

## 9. Rollout Plan

### 9.1 Three sub-phases

**Phase 2.1 — Scaffold + unit tests (~3h)**
- Create `scripts/hermes-approve-mcp.py` skeleton
- Create `.secrets/approve-bot.env` (gitignored, with placeholder)
- Create `.approve/` directory + `.gitignore` entries
- Write all unit tests
- Run `pytest tests/unit/` → green

**Phase 2.2 — Integration + implementation (~3h)**
- Write all integration tests (mock Telegram)
- Implement `PendingStore`, `TelegramPoller`, `format_*`, `validate`
- Run `pytest tests/` → green
- ruff + mypy clean

**Phase 2.3 — Real bot + E2E (~2h)**
- Create @bew_approve_bot via @BotFather
- Add entry to `~/.zcode/cli/config.json`
- Restart ZCode → verify `ping()` tool works
- Run E2E scenarios M1–M3 by hand
- Commit + push branch `phase-2/telegram-approve-mcp`

### 9.2 Rollback plan

1. **Disable tool**: set `enabled: false` in ZCode config → restart → safe immediately
2. **Revoke bot**: @BotFather `/revoke` or `/deletebot`
3. **No Hermes impact**: separate bot, separate dependencies, separate state dir
4. **No replay needed**: `.approve/` contents are disposable

### 9.3 Acceptance criteria (definition of done)

- [ ] Unit tests pass 100%
- [ ] Integration tests pass 100%
- [ ] E2E scenarios M1, M2, M3 pass
- [ ] Code committed on branch `phase-2/telegram-approve-mcp`
- [ ] `.gitignore` covers `.secrets/` and `.approve/`
- [ ] `MEMORY.md` updated: Phase 2 marked done
- [ ] `AGENTS.md` includes "when to call request_approval" section
- [ ] ruff + mypy clean
- [ ] Bot token exists only in `.secrets/approve-bot.env`, never in code or git history

---

## 10. Open Items / Follow-ups

- **Phase 1D-2 not yet merged**: Phase 2 branch should branch off `main` after the 1D-2 PR merges, OR explicitly stack on top of `phase-1d-2/session-memory-hook`.
- **Outstanding credential revoke (8 items from MEMORY.md)**: independent of Phase 2 but should not be forgotten.
- **Future Phase 3 candidates**: auto-block guardrail hook; multi-user; "remember this decision" scopes; Slack/Discord routing; history dashboard.

---

## 11. Key Decisions Recap

| Decision | Chosen | Rejected alternatives | Why |
|---|---|---|---|
| Flow direction | ZCode → Telegram | Hermes-driven; bidirectional bridge | Matches actual workflow (ZCode is the actor) |
| Trigger | Opt-in MCP tool | Auto-block hook; hybrid | ZCode hook sync-wait is fragile; agent judgment > brittle pattern matching |
| Bot | New dedicated bot | Reuse Hermes bot; gateway HTTP API | Avoids callback routing conflicts; chat hygiene; isolated blast radius |
| Sync model | Sync block + timeout | Async poll; hybrid | Async burns tokens polling; sync is simpler for agent reasoning |
| Payload | Metadata only | Full payload; command + path | Minimizes leak surface (8 leaked creds still outstanding) |
| Architecture | In-process poller (A) | HTTP webhook (B); SQLite daemon (C) | Single-user single-session; YAGNI; matches existing `n8n/server.py` pattern |
