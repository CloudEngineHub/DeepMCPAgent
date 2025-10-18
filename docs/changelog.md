# Changelog

## 0.5.0 — 2025-10-18

### Added

- Cross-Agent Communication (in-process) with `cross_agent.py`.
- `CrossAgent`, `make_cross_agent_tools`, `ask_agent_<name>`, and `broadcast_to_agents`.
- `build_deep_agent(..., cross_agents=...)` to attach peers as tools.
- Example `examples/use_cross_agent.py`.
- Cross-Agent documentation section.

### Changed

- Pydantic v2 config via `model_config = ConfigDict(extra="forbid")`.

### Fixed

- Clearer trace output for cross-agent tool calls.
- Better errors for unknown peers in `broadcast_to_agents`.
- Implemented sync `_run` on tools to satisfy `BaseTool`.

---

## 0.4.1 — 2025-10-17

### Added

- No new features.

### Changed

- Updated runtime compatibility for environments without `deepagents` installed (graceful fallback to ReAct agent).
- Minor code cleanup and improved defensive checks in the agent builder.

### Fixed

- Fixed `TypeError` when falling back to `create_react_agent()` with `langgraph>=0.6`.
- Agent builder now dynamically detects supported parameters and omits deprecated ones for smooth operation across LangGraph versions.
- Improved `_after()` trace hook to skip `None` values when `trace_tools=True`, correctly displaying tool results.

---

## 0.4.0

### Added

- CLI now supports pretty console output, `--trace/--no-trace`, and `--raw` modes.
- HTTP server specs fully supported with block string syntax (`--http "name=... url=..."`).
- Tool tracing hooks (`on_before`, `on_after`, `on_error`) integrated into the agent layer.
- Richer agent streaming output: shows invoked tools, arguments, and results.
- Added `__version__` export via package metadata.
- Basic PyTests

### Changed

- Updated runtime dependencies:
  - `langgraph` and `langgraph-prebuilt` pinned to `>=0.6,<0.7`.
  - `langchain` bumped to `>=0.3.27`.
  - `fastmcp` bumped to `>=2.12.2`.
- CLI and agent examples polished for consistency and usability.
- Development extras modernized (latest `ruff`, `mypy`, `pytest`, etc.).

### Fixed

- Multiple Ruff issues (imports, `Optional` → `X | None`, try/except cleanups).
- Validation errors in CLI argument parsing.
- Tool discovery now handles `None` or empty results gracefully.
- Safer error handling in `_FastMCPTool` when tool callbacks raise.
- CI workflow stabilized for PyPI publishing with setuptools-scm dynamic versioning.

---

## 0.3.0

### Added

- Improved JSON Schema → Pydantic mapping:
  - Carries through defaults and descriptions via `Field`.
  - Generates per-tool arg models (`Args_<tool>`).
  - Sanitizes model names for Pydantic compatibility.
- CLI improvements:
  - Added `--version` flag.
  - Simplified option parsing.
  - Updated documentation.
- PyPI Trusted Publishing workflow (publish on tag).
- CI improvements: Ruff formatting, mypy fixes, skip deep extra on Python 3.10.

### Fixed

- Type errors in CLI, agent, tools, and clients.
- CLI annotation options adjusted to satisfy Ruff rules.

### Changed

- Project license clarified to Apache-2.0.
- Project metadata aligned with license notice.

---

## 0.1.0

- Initial FastMCP client edition.
