# Permissions

## The distinction the whole design rests on

    a model *wants* to perform an action        ->  ToolCall
    the user has *authorized* the action        ->  PermissionDecision

`ToolCall` is generated text. `PermissionDecision` is authority. Nothing in the
codebase converts one into the other except a `PermissionBroker`.

Autonomy is not unrestricted execution. The system must always be able to say
which of the two it is holding.

## Levels

| Level | Severity | Default policy | Meaning |
|---|---|---|---|
| `read` | 10 | `auto` | Inspect local data |
| `write` | 20 | `ask` | Create or modify files |
| `external_action` | 30 | `ask` | Anything leaving this machine |
| `send_message` | 40 | `ask` | Email, chat — a human receives it |
| `spend_money` | 50 | `ask` | Purchases, paid APIs |
| `delete` | 60 | `ask` | Destroy data |
| `destructive` | 70 | `deny` | Irreversible system operations |

Configured in `config/default.yaml`:

```yaml
permissions:
  interactive: true
  policy:
    read: auto
    write: ask
    destructive: deny
```

`auto` proceeds and records. `ask` prompts a human. `deny` refuses outright and
never prompts.

## Three rules that close the obvious gaps

**A level nobody configured defaults to `deny`.** A permission level that was
never considered is not one to grant by default. Adding a new level to the enum
therefore fails closed until someone decides what it should do.

**`requires_human_approval` beats an `auto` policy.** Some tools need a human
regardless of their level. The flag can be set on a tool or on an agent
manifest, and it forces a prompt even where policy would have proceeded.

**Non-interactive sessions refuse rather than self-approve.** With
`interactive: false`, every `ask` becomes a refusal — recorded with
`source: "non_interactive"`. An unattended run can neither block forever on
stdin nor quietly grant itself what needed a human. This is the setting for
anything scheduled.

## The single gate

```python
# agents/base.py — the only path to executing a tool
decision = self.broker.request(request)
self.trace.event(Events.PERMISSION_DECISION, ...)
if decision.denied:
    return f"DENIED: ..."          # observation, not exception
output = tool.execute(args, self.context)
```

One choke point, one testable property:

```python
def test_a_denied_tool_is_never_executed(...):
    ...
    assert spy.invocations == []
```

A check that appears in nine places is a check that is missing from the tenth,
and the tenth will be the tool that sends email (ADR-006).

Any future execution path — a Master Agent invoking a sub-agent, a scheduled
run — must route through this same gate. A second call site to `Tool.execute`
is a design error, not a shortcut.

## Ordering

Arguments are validated **before** the broker is consulted. There is no point
asking a human to approve a call that could not have run anyway, and a prompt
that turns out to be moot trains the user to approve without reading.

## Denials are recoverable

A refusal comes back to the model as an observation:

```
DENIED: permission to run 'send_email' was refused (declined by user).
Do not retry this call; either continue without it or explain what you
cannot do.
```

The model can then finish the rest of the task and report honestly on what it
could not do — which is more useful than a crashed run, and far more useful
than a model that quietly pretends the action succeeded.

## Brokers

| Broker | Use |
|---|---|
| `PolicyBroker` | Config-driven; delegates `ask` to a prompter |
| `CLIPermissionBroker` | The interactive default — terminal prompt |
| `AllowAllBroker` | **Tests only.** Never wire into a real run |
| `DenyAllBroker` | Tests: assert refusals are handled gracefully |
| `RecordingBroker` | Wraps another; captures every request and decision |

## The orthogonal control

Permissions decide whether an action may happen. The **filesystem jail**
(`paths.allowed_roots`, `resolve_within_roots()`) decides which paths are
nameable at all. Both must pass; neither substitutes for the other. See
`tools.md`.

## Severity is not sensitivity (FUTURE — NOT IMPLEMENTED)

`PermissionLevel` answers one question: *how much damage can this do?* It cannot
answer a second one: *how private is this?*

Those come apart immediately in any sensitive domain. Reading a wellness journal
is **`read` severity and high sensitivity at the same time** — the current model
has no way to say so, and would wave it through on `read: auto`.

The recorded direction (ADR-019) is a second, orthogonal axis:

```
severity     read < write < external_action < ... < destructive     (existing)
sensitivity  normal | sensitive                                     (future)
```

The broker keeps gating on severity; cross-agent sharing and memory writes gate
on sensitivity.

**Why not just add levels.** Squeezing `store_memory` and `share_with_agent`
into the severity ladder forces an answer to "is sharing a journal more or less
severe than spending money?" — a question with no meaningful answer, which is
the signal that it is the wrong axis.

Nothing here is built. It is recorded so a future implementer extends the model
deliberately rather than discovering the mismatch mid-build. See
[`health-wellness.md`](health-wellness.md).

## Auditing

Every decision is traced:

```json
{"type": "permission.decision", "data": {
  "tool": "read_file", "level": "read", "resource": "config/default.yaml",
  "granted": true, "source": "policy",
  "reason": "policy auto-approves read actions"}}
```

`source` distinguishes `policy`, `user`, `spec` and `non_interactive` — so
"was this approved by a human, or by a rule?" is answerable after the fact.

```
paios trace <run_id> -v
```
