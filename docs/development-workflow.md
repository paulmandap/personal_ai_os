# Development workflow

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
paios doctor
```

`paios doctor` is the first thing to run on any machine, and the first thing to
run when something behaves oddly. It answers "does this environment work?" in
one command.

## Starting a session

1. Read `PROJECT_STATE.md` — the source of truth for where development stopped.
2. Read `CLAUDE.md` — the rules.
3. `paios doctor` — confirm the machine still works.
4. `git status` and `git log --oneline -10` — see what changed.
5. `pytest -q` — confirm a green baseline *before* changing anything.

Continue from persisted state, not from a remembered conversation. The
repository is the handoff; there may be no previous session to ask.

## Making a change

```
Inspect  ->  Plan  ->  Implement  ->  Test  ->  Fix  ->  Document  ->  Summarise
```

Prefer small coherent changes. Do not build speculative abstractions — if
nothing in the repository uses it and no test exercises it, it does not go in.

## Testing

```powershell
pytest -q                    # unit — must pass with Ollama STOPPED
pytest -m integration        # live — needs Ollama and pulled models
pytest -q --cov=personal_ai_os --cov-report=term-missing
```

### The rule that matters

**The unit suite must pass with the network unplugged.** This is not
convenience — it is the evidence that the architecture, and not the model, owns
the workflow. `tests/unit/conftest.py` blocks `socket.connect`, so a test that
quietly reaches a live server fails loudly instead of eroding the claim.

Use `ScriptedModel` for agent behaviour and `httpx.MockTransport` for provider
translation. Live models belong in `tests/integration/`.

### Integration tests and VRAM

Tests that switch between the 3B and 7B evict the previous model first
(`fresh_vram`). On an 8 GB card, letting Ollama swap under load crashed its
runner mid-request. If you add a test that uses a different model size, use the
same fixture.

## Failure reporting

Do not hide failures. Report what failed, why it likely failed, what was tried,
what is unresolved, and the recommended next step.

Never weaken a test to make it pass, and never delete a failing test because
the implementation is hard. A test that is inconvenient is usually a test that
found something.

Do not mark a feature complete when it partially works. Say which part works.

## Git

**You are the only person who commits.** An assistant may run read-only
commands (`status`, `diff`, `log`, `show`, `branch`) and must never run
`commit`, `push`, `tag`, `reset`, `rebase`, `merge` or `checkout`.

Before ending a meaningful unit of work, produce:

```
Files changed
What changed
Tests performed
Known issues
Suggested commit message
Next recommended task
```

Then stop. The commit is yours.

## Ending a session

1. `pytest -q` and `pytest -m integration`
2. Update `PROJECT_STATE.md` — phase, completed, in progress, blockers, next
3. Update any docs the change invalidated
4. Add an ADR to `docs/decisions.md` if an architectural decision was made
5. `git diff` and review
6. Summarise, including what is unfinished
7. Suggest a commit message
8. **Do not commit**

The test: could someone with no memory of this session pick up the repository
tomorrow and continue? If not, `PROJECT_STATE.md` is not finished.

## Adding things

| Adding | Do |
|---|---|
| A tool | Subclass `Tool`, add to `default_registry()`, test `run()` and any path handling |
| An agent | Write `agents/<name>.yaml`. Add a class only if it needs custom prompt/output logic |
| A model | Change `config/local.yaml`. No code change |
| A provider | Implement `AgentModel`, extend `default_factory()` in `models/registry.py` |
| A permission level | Add to the enum *and* to `config/default.yaml` — unconfigured levels deny |

## Conventions

- Type hints everywhere; `from __future__ import annotations` at the top
- Pydantic models for anything crossing a boundary
- Typed errors from `core/errors.py`, never bare `Exception`
- Components receive collaborators as constructor arguments; no global state
- Comments explain *why*, not *what*
- Line length 90
