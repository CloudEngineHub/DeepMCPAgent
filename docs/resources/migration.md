# Migration Guide

Migrate from `deepmcpagent` to `promptise` -- the v1.0.0 release.

---

## Overview

DeepMCPAgent has been rebranded as **Promptise Foundry** with v1.0.0. The framework grew beyond its original MCP-focused scope to include prompt engineering, a reasoning graph engine, an agent runtime, a full MCP server and client, RAG foundation, and more. The new name reflects this evolution.

All functionality is preserved and expanded. The migration is primarily a rename -- same API signatures, same configuration format, same CLI structure -- plus significant new capabilities.

---

## What Changed

### Package Name

| | Old | New |
|---|-----|-----|
| **PyPI package** | `deepmcpagent` | `promptise` |
| **Python import** | `from deepmcpagent import ...` | `from promptise import ...` |
| **CLI command** | `deepmcpagent` | `promptise` |
| **GitHub repository** | `github.com/cryxnet/DeepMCPAgent` | `github.com/promptise/foundry` |
| **Documentation** | Various URLs | `promptise.github.io/foundry` |

---

## Step-by-Step Migration

### Step 1: Uninstall the Old Package

```bash
pip uninstall deepmcpagent
```

### Step 2: Install the New Package

```bash
# With DeepAgents support (recommended)
pip install "promptise[all]"

# Or with all features
pip install "promptise[deep,sandbox]"
```

### Step 3: Update Python Imports

Find and replace `deepmcpagent` with `promptise` across your codebase:

**Before:**

```python
from deepmcpagent import build_agent
from deepmcpagent.config import HTTPServerSpec, StdioServerSpec
from deepmcpagent.sandbox import SandboxConfig
from deepmcpagent.cross_agent import CrossAgent
```

**After:**

```python
from promptise import build_agent
from promptise.config import HTTPServerSpec, StdioServerSpec
from promptise.sandbox import SandboxConfig
from promptise.cross_agent import CrossAgent
```

### Step 4: Update CLI Commands

**Before:**

```bash
deepmcpagent agent assistant.superagent
deepmcpagent validate config.superagent
deepmcpagent init -o agent.superagent -t basic
deepmcpagent list-tools --http name=math url=http://localhost:8000/mcp
```

**After:**

```bash
promptise agent assistant.superagent
promptise validate config.superagent
promptise init -o agent.superagent -t basic
promptise list-tools --http name=math url=http://localhost:8000/mcp
```

### Step 5: Update Dependency Files

**requirements.txt:**

```text
# Before
deepmcpagent>=0.6.0

# After
promptise[all]>=1.0.0
```

**pyproject.toml:**

```toml
# Before
dependencies = ["deepmcpagent>=0.6.0"]

# After
dependencies = ["promptise[all]>=1.0.0"]
```

**Dockerfile:**

```dockerfile
# Before
RUN pip install "deepmcpagent"

# After
RUN pip install "promptise[all]"
```

### Step 6: SuperAgent Files -- No Changes Needed

`.superagent` configuration files work as-is with the new package. The YAML format is unchanged. Just use the new CLI command:

```bash
promptise agent your-config.superagent
```

---

---

## Compatibility Period

For approximately 6 months after release (until ~July 2026):

- `deepmcpagent==0.7.0` will be available on PyPI as a compatibility shim
- Importing from `deepmcpagent` will emit a `DeprecationWarning` and re-export from `promptise`
- Old GitHub URLs automatically redirect to the new repository
- Stars, forks, and issues are preserved

After the compatibility period, `deepmcpagent` will be removed from PyPI.

---

## Verify Your Migration

### Check Installation

```bash
pip show promptise
promptise --version
promptise --help
```

### Check Imports

```python
from promptise import build_agent
from promptise.config import HTTPServerSpec, StdioServerSpec
print("All imports successful")
```

### Run Your Tests

```bash
pytest tests/ -v
```

---

## Troubleshooting

### `ModuleNotFoundError: No module named 'deepmcpagent'`

You have not installed the new package yet:

```bash
pip uninstall deepmcpagent
pip install "promptise[all]"
```

### `Command 'deepmcpagent' not found`

Use the new CLI command:

```bash
promptise agent config.superagent
```

Or install the compatibility package temporarily:

```bash
pip install deepmcpagent==0.7.0
```

### Import Errors with Naming

Remember the naming:

- PyPI package: `promptise`
- Python imports: `from promptise import ...`
- CLI command: `promptise`

---

## Migration Checklist

- [ ] Uninstall old package: `pip uninstall deepmcpagent`
- [ ] Install new package: `pip install "promptise[all]"`
- [ ] Update all Python imports: `from deepmcpagent` to `from promptise`
- [ ] Update CLI commands: `deepmcpagent` to `promptise`
- [ ] Update `requirements.txt` / `pyproject.toml`
- [ ] Update Dockerfile (if applicable)
- [ ] Update CI/CD pipelines
- [ ] Update documentation links
- [ ] Run tests to verify everything works

---

## Timeline

| Date | Event |
|------|-------|
| v1.0.0 (April 2026) | Initial release of `promptise` on PyPI |
| ~October 2026 | `deepmcpagent` compatibility shim removed from PyPI |
