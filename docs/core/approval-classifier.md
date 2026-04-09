# AutoApprovalClassifier

An explicit decision hierarchy that sits in front of your approval handler. Instead of sending every tool call to a human, the classifier evaluates five ordered layers — allow rules, deny rules, read-only detection, an optional LLM classifier, and finally the fallback handler — and returns the first definitive answer.

```python
from promptise import (
    ApprovalPolicy,
    AutoApprovalClassifier,
    ApprovalRule,
    WebhookApprovalHandler,
)

classifier = AutoApprovalClassifier(
    allow_rules=[ApprovalRule(tool="get_*", reason="read-only")],
    deny_rules=[ApprovalRule(tool="exec_shell", reason="too risky")],
    read_only_auto_allow=True,
    fallback=WebhookApprovalHandler(url="https://approvals.internal/api"),
)

policy = ApprovalPolicy(
    tools=["*"],
    handler=classifier,  # drop-in replacement
)
```

---

## The 5-layer hierarchy

Evaluated in order. First definitive answer wins.

| Layer | What it does | Result |
|---|---|---|
| **1. Allow rules** | Pattern/predicate matching. First match → approve. | `approved=True` |
| **2. Deny rules** | Pattern/predicate matching. First match → deny. | `approved=False` |
| **3. Read-only auto-allow** | Tool name starts with `get_`, `list_`, `read_`, `search_`, etc. | `approved=True` |
| **4. LLM classifier** | Optional async function returns `"allow"`, `"deny"`, or `"escalate"`. | allow/deny or fall through |
| **5. Fallback handler** | Your existing `ApprovalHandler` (webhook, queue, callback). | whatever the human says |

---

## ApprovalRule

Rules match by tool glob, user ID, argument substring, or async predicate. All non-empty filters must match (AND logic).

```python
# Simple glob match
ApprovalRule(tool="delete_*", reason="destructive")

# User-scoped
ApprovalRule(tool="*", user="admin@acme.com", reason="admin bypass")

# Argument inspection
ApprovalRule(tool="shell", argument_contains="rm -rf", reason="dangerous command")

# Custom async predicate
async def is_low_risk(req):
    return len(str(req.arguments)) < 100

ApprovalRule(predicate=is_low_risk, reason="small payload")
```

---

## Read-only auto-allow

Enabled by default. Tool names starting with any of these prefixes are auto-approved:

`get_`, `list_`, `read_`, `search_`, `find_`, `fetch_`, `describe_`, `show_`, `view_`, `lookup_`, `query_`, `head_`, `stat_`, `exists_`, `count_`

Override with your own list:

```python
classifier = AutoApprovalClassifier(
    read_only_prefixes=("get_", "list_", "count_"),
    fallback=my_handler,
)
```

Or disable entirely:

```python
classifier = AutoApprovalClassifier(
    read_only_auto_allow=False,
    fallback=my_handler,
)
```

---

## LLM classifier (optional)

For fuzzy decisions that rules can't capture:

```python
async def safety_check(request):
    # Call your LLM / safety model
    response = await llm.classify(
        f"Is this tool call safe? {request.tool_name}({request.arguments})"
    )
    if response.safe:
        return "allow", "LLM classified as safe"
    if response.dangerous:
        return "deny", "LLM classified as dangerous"
    return "escalate", "LLM unsure — send to human"

classifier = AutoApprovalClassifier(
    llm_classifier=safety_check,
    fallback=my_handler,
)
```

Returning `"escalate"` defers to the fallback handler (layer 5).

---

## Stats and audit

Every decision increments a counter in `classifier.stats`:

```python
print(classifier.stats.allow_rule_hits)    # 42
print(classifier.stats.deny_rule_hits)     # 3
print(classifier.stats.read_only_allows)   # 189
print(classifier.stats.llm_allows)         # 7
print(classifier.stats.fallback_denies)    # 1
```

The last decision's diagnostic trace is available via `classifier.last_trace`:

```python
trace = classifier.last_trace
print(trace.layer)        # "allow_rule"
print(trace.rule_reason)  # "read-only"
```

---

## Drop-in replacement

`AutoApprovalClassifier` implements the `ApprovalHandler` protocol. Swap it into any existing `ApprovalPolicy` without changing anything else:

```python
# Before
policy = ApprovalPolicy(tools=["*"], handler=webhook_handler)

# After
policy = ApprovalPolicy(tools=["*"], handler=AutoApprovalClassifier(
    allow_rules=[...],
    deny_rules=[...],
    fallback=webhook_handler,
))
```

---

## Related

- [Approval (HITL)](approval.md) — the underlying approval system
- [Runtime Hooks](../runtime/hooks.md) — react to PERMISSION_REQUEST / PERMISSION_DENIED events
- [Guardrails](guardrails.md) — input/output security scanning
