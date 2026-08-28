# Finance

The Finance Agent answers questions about the user's own money: balances,
spending, recurring bills, savings goals, and whether a purchase is affordable.

Two rules shape the whole domain, and both exist because being confidently
wrong about money is worse than being slow or unhelpful.

---

## 1. Money is integer minor units

Every stored amount is an `INTEGER` count of centavos. No `REAL` column, no
`float` in any signature (ADR-023).

```python
to_minor("1,234.56")  ->  123456
format_minor(123456)  ->  "PHP 1,234.56"
```

`to_minor` **refuses a float** — that is the one place the representation error
could quietly return.

Binary floating point cannot represent `0.10`. In minor units the arithmetic is
exact:

```python
to_minor("0.10") + to_minor("0.20") == to_minor("0.30")   # True
sum(to_minor("0.01") for _ in range(1000)) == to_minor("10.00")   # True
```

### Strict core, tolerant edge

Models emit bare JSON numbers for amounts constantly, and JSON `1234.56`
arrives in Python as a float. Rejecting it would be pedantic; carrying it
inward would defeat the point. So the **tool boundary** stringifies —
`str(1234.56)` is `'1234.56'`, Python's shortest round-trip form — and
`to_minor` parses it with `Decimal`.

The core stays strict. Only the edge is forgiving.

---

## 2. The model explains; the tool computes

`affordability_check` does the arithmetic and returns its working:

```
requested            PHP 5,000.00
total balance        PHP 20,000.00
upcoming bills      -PHP 8,000.00
reserved for goals  -PHP 0.00
discretionary        PHP 12,000.00
after this purchase  PHP 7,000.00
verdict              affordable
```

The agent's job is to explain that. It is told, explicitly, never to add or
subtract anything itself (ADR-024).

**Why this is not over-caution.** Arithmetic has exactly one correct answer and
needs no model — the numbers are all in a database. Letting the model derive
them adds a failure mode with no upside, in the domain where a wrong answer
costs the most.

`no_unsupported_amounts` in the evaluation suite enforces it: every figure in
the answer must trace back to a tool result or to the user's own words.

---

## 3. If two writes must both happen, they are one tool

`transfer` moves money between accounts in a single database transaction —
both legs or neither (ADR-029).

This exists because of a measured failure. Asked to move ₱5,000 between
accounts, the agent issued two separate `add_transaction` calls. The debit
failed (it guessed the account name "BPI savings" where the record said
"savings"); the credit succeeded. **The ledger gained ₱5,000 that never
existed.**

A model cannot roll back. Any operation that must be all-or-nothing has to be a
single tool call, because inside the tool is the only place a partial failure
can be undone. No prompt makes the second call succeed.

A failed transfer changes nothing: insufficient funds, an unknown account, a
same-account move, or mismatched currencies all refuse before anything is
written.

### Account names resolve the way people say them

`account("BPI savings")` finds the account recorded as `savings` — exact match
first, then token coverage, with **ambiguity refused rather than guessed**.

Same lesson as ADR-022: requiring an exact identifier the user never supplied
is what makes a model invent one, and here inventing one corrupted a ledger.
With money, silently picking the wrong account is the worst available outcome,
so two plausible matches produce an error naming both.

## Currencies: refuse rather than guess

`total_balance_minor()` raises if accounts hold different currencies, naming
them. There is no exchange rate here and no way to fetch one offline, so adding
PHP to USD would be silently, confidently wrong — the exact failure this module
exists to prevent.

`list_accounts` still reports every balance and marks only the total
unavailable. A partial answer beats a wrong number.

## Schema

```sql
accounts(id, name, currency, balance_minor, updated_at)
transactions(id, account_id, occurred_on, amount_minor, category, description, created_at)
commitments(id, name, amount_minor, day_of_month, category, active)
goals(id, name, target_minor, saved_minor, target_date)
```

Added as migration 2 in `memory/store.py`. Migration 1 is untouched — it has
already run on the real database.

**Transactions move the balance in the same SQL transaction.** A ledger where
the entry landed but the balance did not is worse than no ledger.

---

## How affordability is calculated

```
discretionary = total balance
              − commitments still due this month
              − this month's share of dated savings goals
```

Two judgement calls worth knowing:

**Only bills not yet due count.** On the 10th, rent due on the 5th has already
gone out; internet due on the 20th has not. Counting both would understate
what is available.

**A dated goal is spread over the months remaining; an undated goal reserves
nothing.** Saving ₱60,000 by next August reserves ₱5,000 this month, not
₱60,000 — otherwise a distant target would make everything look unaffordable
today. An undated goal is an aspiration rather than a commitment, and treating
it as one would quietly block every purchase.

**Verdicts are three, not two.** `affordable`, `tight` (under ₱500 left), and
`not_affordable`. Calling ₱200 of remaining headroom "affordable" is
technically true and practically misleading.

---

## Permissions: `write`, not `spend_money`

These tools record facts *about* money. **Nothing here moves any.** A
`spend_money` tool would be an actual payment, which this system does not have.

Labelling them `spend_money` would put a frightening prompt in front of a
harmless action, and the cost of that is not caution — it is teaching the user
to click through the prompt that would one day really matter (ADR-014).

`requires_human_approval` stays `false` for the same reason: `write: ask`
already prompts on every mutation, and prompting on reads as well would make
those prompts routine.

| Tool | Level |
|---|---|
| `list_accounts`, `list_transactions`, `list_commitments`, `list_goals`, `affordability_check` | `read` |
| `set_balance`, `add_transaction`, `transfer`, `add_commitment`, `add_goal` | `write` |

`transfer` moves money between the user's *own* accounts. It is still `write`,
not `spend_money`: nothing leaves the user's control, and the total is
unchanged.

---

## Using it

```powershell
paios run finance "My BPI account has 20000 pesos. Rent is 8000 due on the 28th."
paios run finance "I want to buy a monitor for 5000. Can I afford it?"

paios finance          # the ledger, no model involved
paios finance 5000     # affordability, computed directly
```

`paios finance` exists partly for its own sake and partly as verification: it
is how you check that a figure an agent quoted is what the database actually
holds.

---

## Measured behaviour

`paios eval run finance` — both `qwen2.5:3b` and `qwen2.5:7b` score **25/25
(100%)**, covering: using the tool rather than doing arithmetic, reporting an
unaffordable purchase plainly, reading balances before stating them, recording
a stated balance, and getting the sign right on spending.

### What building the suite found

`records_spending_with_the_right_sign` failed **every run** at first. The cause
was not the model: it passed `description: null` — an explicit JSON null for an
optional field — and pydantic rejected it. The call was well-formed in every
way that mattered.

Worse, the agent then told the user *"I've updated your cash account by
subtracting 450 pesos"* when the tool had failed and the balance was unchanged.

The fix was a `ToolInput` base class treating an explicit null on an optional
field as "not supplied". It applies to **every tool in the system**, not just
finance — and it also lifted the task agent's `tool_calling` score from 90% to
100%, because the same bug was quietly costing iterations there too.

---

## Not in scope

- **No payments.** Nothing initiates a transfer or a purchase.
- **No advice.** The agent reports figures; it does not recommend investments,
  and it is not a licensed adviser.
- **No bank import.** Manual entry only. Bank CSV formats vary wildly, and
  column-guessing is its own source of silently wrong balances.
- **No currency conversion.** Accounts carry a currency code, and a mixed
  total is *refused* rather than computed — see above. Conversion needs a rate
  this system has no offline way to obtain.
