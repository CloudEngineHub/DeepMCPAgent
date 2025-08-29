# Installation

## Requirements
- Python **3.10+**
- A virtual environment (recommended)

## Install (editable)
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### Optional extras
- **Deep agent loop**:
  ```bash
  pip install -e ".[deep]"
  ```
- **Docs tooling**:
  ```bash
  pip install -e ".[docs]"
  ```
- **Examples**:
  ```bash
  pip install -e ".[examples]"
  ```

!!! tip "zsh users"
    Quote extras: `pip install -e ".[dev,deep]"` (or escape brackets).

## macOS / Homebrew note (PEP 668)
If you see “externally managed environment” errors, you’re installing into Homebrew’s Python. Use a venv as shown above.
