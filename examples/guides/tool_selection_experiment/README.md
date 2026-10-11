# Tool selection experiment: 122 MCP tools, 20 tasks, 5 settings

The code behind the guide
[Too Many MCP Tools? LLM Tool Selection, Measured](https://www.promptise.com/guides/view/too-many-mcp-tools-llm-tool-selection).
It gives an agent a 122-tool admin MCP server and 20 requests with one right tool each, and
compares sending all tools against each of Promptise Foundry's tool optimization levels:
whether the right tool was offered and called, input tokens and time, per model call.

## Files

| File | What it is |
|---|---|
| `acme_tools.py` | `make_tool` / `add_tools`: generate echo tools on an `MCPServer` from tables |
| `platform_server.py` | The measured server: 14 resources x 5 standard tools + 52 actions = 122 tools |
| `customer_server.py` | 26 billing and support tools, only used through `all_in_one_server.py` |
| `all_in_one_server.py` | Both servers in one, 148 tools, to show OpenAI's 128-tool limit |
| `tasks.py` | The 20 requests and the tool each one needs |
| `experiment.py` | Runs every task against every setting and appends one JSON line per run |
| `summarize.py` | Turns the JSON lines into the guide's results table, the misses and the cost |
| `definitions.py` | Offline: tool definition sizes per optimization level (tiktoken) |
| `retrieval_check.py` | Offline: would semantic selection offer the right tool? (`ToolIndex`) |
| `followup.py` | Tools offered for a direct request and for a "Yes, go ahead." follow-up |
| `selection_probes.py` | Single runs for `semantic_top_k`, `preserve_tools` and `request_more_tools()` |

Every tool only echoes its arguments, so nothing is changed anywhere: the experiment records
which tool the model picked, not what the tool did.

## Setup

Python 3.10 or newer.

```bash
pip install "promptise[tool-optimization]>=1.3.2"
export OPENAI_API_KEY="sk-..."
cd examples/guides/tool_selection_experiment   # run everything from this directory
```

The `tool-optimization` extra brings `sentence-transformers` (and PyTorch, a large download).
The embedding models, `all-MiniLM-L6-v2` and, for `retrieval_check.py`, `BAAI/bge-small-en-v1.5`,
are downloaded from Hugging Face on first use and cached in `~/.cache/huggingface/`. Every
script builds agents with `openai:gpt-5-mini`, so set `OPENAI_API_KEY` even for the offline
ones, which make no model call. The scripts start the MCP servers themselves, over
stdio.

## Run

```bash
python definitions.py          # offline, under a minute
python retrieval_check.py      # offline
python experiment.py           # 300 runs: 20 tasks x 3 repeats x 5 settings -> runs.jsonl
python summarize.py runs.jsonl
python experiment.py --server all_in_one_server.py --settings "all tools" minimal standard \
    --reps 1 --tasks 1 --out runs148.jsonl
python followup.py
python selection_probes.py topk preserve noargs
```

`experiment.py` appends to its output file, so delete `runs.jsonl` before a fresh run. For a
quick look, run a subset: `python experiment.py --reps 1 --tasks 1 12 --settings "all tools" semantic`.

## How a run is counted

- **Right tool called**: the model called the expected tool at any point in the run. The
  task counts as solved then, even if the model did more afterwards.
- **Right tool offered**: the first model call of the run included the expected tool.
  `summarize.py` also reports whether any later call offered it.
- **Runs that loop.** Some tasks can't be finished against an echo server (a `list_*` tool
  never returns the ID the next call needs, a usage tool returns no numbers), and the model
  may keep calling tools until the ReAct `reason` node reaches its iteration limit (15). Since Promptise 1.3.2 such a run
  raises `GraphIterationLimitError` instead of returning the last tool result as the answer.
  `experiment.py` catches it and records the run with `"iteration_limit"`, the tools the model
  called up to then and no answer. It counts as a run like any other: solved only if the
  expected tool was called before the limit. The `limit` column of `summarize.py` shows how
  many runs ended this way.
- **Errors**: a run that fails for any other reason (for example the provider rejecting the
  request) is recorded with `"error"` and left out of every column except `errors`.

## What to expect

The guide's run (Promptise 1.3.2, `gpt-5-mini`, October 2026) summarized like this. Model
behaviour varies from run to run, so expect your counts to differ by a few runs per setting.

```
setting                            runs    limit   errors offered1  offered   called    tools      in1   cached   s/task  s/call1      rmt
all tools                            60        3        0       60       60       57      122     9686      96%      4.7      2.0        0
minimal                              60        6        0       60       60       52      122     8108      95%      6.1      2.1        0
standard                             60        5        0       60       60       49      122     8000      96%      8.1      2.2        0
semantic                             60        9        0       48       56       50        9      798      13%      4.0      1.7        6
semantic, full descriptions          60        4        0       51       58       50        9      808      10%      3.7      1.7        6
```

`offered1` and `tools` should not vary: the first model call's tools depend only on the request,
the tool descriptions and the embedding model. With all tools sent, `all_in_one_server.py` fails on every call with
OpenAI's `array_above_max_length` error (148 tools, the limit is 128).

## Cost

The full experiment made 1,166 model calls: 6.8 million input tokens, of which OpenAI served
6.1 million from its prompt cache, and 0.19 million output tokens. That is about **$0.71** at
gpt-5-mini prices ($0.25 per million input tokens, $0.025 cached, $2.00 output), and it took
about 50 minutes. `summarize.py` prints the cost of your own run on its last line.
`followup.py` and `selection_probes.py` cost a few cents together; `definitions.py` and
`retrieval_check.py` make no model calls.
