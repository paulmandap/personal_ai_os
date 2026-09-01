"""Did the user's own turn name this URL?

**The authorization criterion for `external_action`, stated exactly:**

    A fetch is authorized iff the requested URL, after safe normalisation,
    appears literally in the user's CURRENT objective. A URL discovered from
    page content is never authorized.

**Why a structural gate rather than a better sentence.** Two independent
measurements now say a prompt clause relocates injection compliance rather than
reducing it: ADR-035 on task notes, ADR-054 on web pages. Without a clause every
attacker-URL request ever recorded here landed on the prior-consent phrasing;
with one, those collapsed and a failure the baseline never had appeared instead.
So this asks the model nothing. It is a comparison in Python between the user's
message and what the tool says it is about to fetch, and an injected page cannot
argue with it -- the model is the component being defended, so it cannot also be
the thing enforcing the defence.

**What this does NOT claim.** Not that a URL's occurrence proves the user
authorized *this particular fetch at this point in a multi-step task*. It proves
only that the URL entered through a channel an attacker cannot write to. That is
weaker, and it is what is relied on.

**The known limitation, and the bound that makes it acceptable.** If the user
names two URLs and a fetched page instructs a fetch of the second, this returns
True -- provenance-by-occurrence cannot tell *"the user asked for B"* from *"a
page asked for B and the user happened to mention B"*. Exact matching bounds it:
nothing can be appended to an authorized URL, so `example.com/b?stolen=secret`
does not match `example.com/b`. The worst an attacker achieves is a fetch of a
URL the user already named, verbatim, at a moment the attacker chose -- request
ordering, not an exfiltration channel.

**The trigger that retires this predicate:** the moment the agent may fetch
outside the user's enumerated set, or carry data in a URL, occurrence is no
longer sufficient and causal provenance is required.

**Why `grounding.write_is_grounded` is not reused.** Same shape, wrong
comparison. It asks whether any content *word* overlaps, and measured against
this suite's own attack it returns **True**: `attacker.example` shares the token
`example` with `example.com`, and both share `http`. It would authorize the
exfiltration fetch. A regression test pins that so nobody wires it here.

**Why the check that failed for writes is right for fetches.** Resource
provenance blocked 21 of 40 legitimate writes because *"mark the second one
done"* never names its target. A fetch always names its target literally -- there
is no *"fetch the second one"*. The indirection that broke it does not arise.

**Fail closed everywhere.** Anything unparseable, ambiguous, or not an
`http(s)` URL with a dotted host returns None from `normalise_url` and is
refused. The gate escalates rather than denying outright (ADR-036), so a refusal
costs a prompt, not a lost capability.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

#: Only these two schemes are ever authorizable. `javascript:`, `data:` and
#: `file:` are not fetches of a web page and must never reach the transport.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

#: A URL that already states its scheme. Matched case-insensitively so
#: `HTTPS://` is recognised rather than treated as scheme-less.
_HAS_HTTP_SCHEME = re.compile(r"^https?://", re.IGNORECASE)

#: Anything of the form `word:` at the start. Used to REJECT rather than parse:
#: it catches `javascript:`, `file:`, `data:` -- and also the genuinely ambiguous
#: `example.com:8080/x`, which could be a scheme or a host:port. Ambiguous forms
#: escalate; they are not guessed at.
_HAS_SOME_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")

#: Sentence punctuation that clings to a URL written in prose -- *"see
#: https://example.com/x."* Stripped ONLY from candidates extracted from the
#: user's message, never from the URL the tool is about to fetch.
_TRAILING_PUNCTUATION = ".,;:!?)]}>\"'"


def normalise_url(raw: str) -> str | None:
    """Canonical comparison form, or ``None`` when it must not be authorized.

    **Normalisation lives only inside the comparison.** It never rewrites what
    gets fetched: the tool is handed the model's original string, the approval
    prompt displays that same string, and only this function's private view of
    it is canonicalised.

    Four equivalences, and no others, because every one of them is a way for an
    attacker URL to be judged equal to the user's:

    1. scheme and host are lowercased -- **path, query and fragment are not**;
    2. a genuinely scheme-less candidate is read as `https`;
    3. one trailing ``/`` is dropped from the path;
    4. (in `_candidates` only) trailing sentence punctuation is dropped.

    Everything else is compared byte for byte: **ports, fragments,
    percent-encoding and punycode are never folded**, because decoding `%2e%2e`
    into `..`, or a punycode host into its Unicode form, would each create an
    equivalence an attacker could aim at.
    """
    candidate = (raw or "").strip()
    if not candidate:
        return None

    if _HAS_HTTP_SCHEME.match(candidate):
        pass
    elif _HAS_SOME_SCHEME.match(candidate):
        # Either a non-web scheme, or `host:port` with no scheme -- ambiguous.
        # An explicitly stated scheme is NEVER rewritten, so this cannot be
        # "helpfully" upgraded to https.
        return None
    else:
        candidate = f"https://{candidate}"

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return None

    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return None

    netloc = parts.netloc.lower()
    if not netloc:
        return None
    if "@" in netloc:
        # `https://example.com@attacker.example/` has host `attacker.example`.
        # Never authorized, and never silently reinterpreted.
        return None
    if "." not in netloc.split(":")[0]:
        # A dotted host, so a bare word from prose ("say.") cannot become one.
        return None

    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    out = f"{scheme}://{netloc}{path}"
    if parts.query:
        out += f"?{parts.query}"
    if parts.fragment:
        out += f"#{parts.fragment}"
    return out


def _candidates(objective: str) -> set[str]:
    """Normalised URLs the user's own message contains.

    Whitespace-split rather than regex-extracted: a URL is a token, and the
    tokeniser should not be cleverer than it needs to be. Trailing sentence
    punctuation is stripped here -- the one place it is ever stripped.
    """
    found: set[str] = set()
    for token in (objective or "").split():
        trimmed = token.rstrip(_TRAILING_PUNCTUATION)
        normalised = normalise_url(trimmed)
        if normalised is not None:
            found.add(normalised)
    return found


def fetch_is_authorized(objective: str, resource: str) -> bool:
    """Does the user's turn name exactly this URL?

    `resource` is `Tool.describe_resource(args)` -- the same string shown in the
    approval prompt and used as the fetch target, so the gate judges precisely
    what a human would be asked to approve and precisely what would be fetched.

    **URL-specific by design, and asymmetric on purpose: the user may write
    informally, the machine must be explicit.** A scheme-less candidate is
    accepted from the *objective* -- *"what does example.com/x say"* is how
    people write -- but the *resource* must carry an explicit `http://` or
    `https://`.

    That asymmetry is what keeps this from becoming a generic authorizer.
    `notes.txt` and `bob@example.com` are both valid host shapes; without the
    requirement, a future `external_action` tool whose resource is a filename or
    an address would be authorized by this URL predicate whenever the user
    happened to mention it. With it, they fail closed and escalate. **This must
    never be generalised into an external-action authorization helper**; a new
    resource kind gets its own predicate and its own adversarial tests.
    """
    if not _HAS_HTTP_SCHEME.match((resource or "").strip()):
        return False
    wanted = normalise_url(resource)
    if wanted is None:
        return False
    return wanted in _candidates(objective)


__all__ = ["fetch_is_authorized", "normalise_url"]
