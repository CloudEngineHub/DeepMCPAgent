# Installation

## Requirements

- Python **3.10+**
- A virtual environment (recommended)

## Install from PyPI (recommended)

The easiest way is to install from [PyPI](https://pypi.org/project/deepmcpagent/):

```bash
pip install "deepmcpagent[deep]"
```

✅ This gives you the **best experience** by including
[`deepagents`](https://pypi.org/project/deepagents/) for the deep agent loop.
If you skip `[deep]`, the agent will fall back to a standard LangGraph ReAct loop.

### Other optional extras

- **Dev tooling** (ruff, mypy, pytest):

  ```bash
  pip install "deepmcpagent[dev]"
  ```

- **Docs tooling** (MkDocs + Material + mkdocstrings):

  ```bash
  pip install "deepmcpagent[docs]"
  ```

- **Examples** (dotenv + extra model integrations):

  ```bash
  pip install "deepmcpagent[examples]"
  ```

!!! tip "zsh users"
Quote extras: `pip install "deepmcpagent[deep,dev]"` (or escape brackets).

---

## Editable install (contributors)

If you’re working on the project itself:

```bash
git clone https://github.com/cryxnet/deepmcpagent.git
cd deepmcpagent
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,deep,docs,examples]"
```

---

## macOS / Homebrew note (PEP 668)

If you see **“externally managed environment”** errors, you’re installing into Homebrew’s Python.
Always use a virtual environment as shown above.
