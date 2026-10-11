"""Summarize an experiment file into the guide's results table, the misses and the cost.

    python summarize.py runs.jsonl

Columns:
  runs       runs recorded (a run that stopped at an iteration limit counts)
  limit      runs that raised GraphIterationLimitError: no answer, the model was still calling tools
  errors     runs that failed before an answer for another reason (left out of every other column)
  offered1   runs whose first model call offered the expected tool
  offered    runs where any model call offered it
  called     runs where the model called it at any point (the task counts as solved)
  tools      median number of tools offered to the first model call
  in1        median input tokens of the first model call, as the API reported them
  cached     share of all input tokens the API served from its prompt cache
  s/task     median seconds per run;  s/call1  median seconds of the first model call
  rmt        runs in which the model called request_more_tools
"""

import json
import statistics
import sys
from collections import Counter, defaultdict

# gpt-5-mini, US dollars per million tokens (OpenAI pricing page, standard tier, October 2026)
PRICE = {"input": 0.25, "cached": 0.025, "output": 2.00}

with open(sys.argv[1] if len(sys.argv) > 1 else "runs.jsonl", encoding="utf-8") as f:
    rows = [json.loads(line) for line in f]
by_setting = defaultdict(list)
for row in rows:
    by_setting[row["setting"]].append(row)

header = (
    "setting",
    "runs",
    "limit",
    "errors",
    "offered1",
    "offered",
    "called",
    "tools",
    "in1",
    "cached",
    "s/task",
    "s/call1",
    "rmt",
)
print(f"{header[0]:<30}" + "".join(f"{h:>9}" for h in header[1:]))
totals = Counter()
for setting, rs in by_setting.items():
    ok = [r for r in rs if "error" not in r]
    limit = sum("iteration_limit" in r for r in ok)
    offered1 = sum(r["expected"] in r["calls"][0]["tools"] for r in ok)
    offered = sum(r["right_tool_offered"] for r in ok)
    called = sum(r["right_tool_called"] for r in ok)
    tools = statistics.median(len(r["calls"][0]["tools"]) for r in ok)
    in1 = statistics.median(r["calls"][0]["input_tokens"] for r in ok)
    calls = [c for r in ok for c in r["calls"]]
    inp = sum(c["input_tokens"] for c in calls)
    cached = sum(c["cached_tokens"] for c in calls)
    out = sum(c.get("output_tokens", 0) for c in calls)
    totals.update(input=inp, cached=cached, output=out, calls=len(calls), runs=len(rs))
    sec = statistics.median(r["seconds"] for r in ok)
    sec1 = statistics.median(r["calls"][0]["seconds"] for r in ok)
    rmt = sum("request_more_tools" in r["called"] for r in ok)
    cells = (
        len(rs),
        limit,
        len(rs) - len(ok),
        offered1,
        offered,
        called,
        f"{tools:.0f}",
        f"{in1:.0f}",
        f"{cached / inp * 100:.0f}%",
        f"{sec:.1f}",
        f"{sec1:.1f}",
        rmt,
    )
    print(f"{setting:<30}" + "".join(f"{c:>9}" for c in cells))

print()
for setting, rs in by_setting.items():
    ok = [r for r in rs if "error" not in r]
    misses = Counter((r["task"], r["expected"]) for r in ok if not r["right_tool_called"])
    not_offered = Counter(
        (r["task"], r["expected"]) for r in ok if r["expected"] not in r["calls"][0]["tools"]
    )
    limits = Counter((r["task"], r["expected"]) for r in ok if "iteration_limit" in r)
    print(f"{setting}:")
    print(f"  not called:                {dict(sorted(misses.items()))}")
    print(f"  not offered on first call: {dict(sorted(not_offered.items()))}")
    print(f"  stopped at the limit:      {dict(sorted(limits.items()))}")
    for r in rs:
        if "error" in r:
            print(f"  error: task {r['task']}: {r['error'][:200]}")

fresh = totals["input"] - totals["cached"]
cost = (
    fresh * PRICE["input"] + totals["cached"] * PRICE["cached"] + totals["output"] * PRICE["output"]
) / 1e6
print()
print(
    f"{totals['runs']} runs, {totals['calls']} model calls; input {totals['input']:,} tokens "
    f"({totals['cached']:,} cached), output {totals['output']:,} tokens; about ${cost:.2f} at gpt-5-mini prices"
)
