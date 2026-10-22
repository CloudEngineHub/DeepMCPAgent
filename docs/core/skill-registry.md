---
seo_title: "Path-Scoped Skills for Reasoning Graphs"
description: "Register reasoning graph skills with glob patterns so each one only activates when the agent works in a matching directory, and load skills from files."
---

# Path-Scoped Skill Activation

Register reasoning-graph skills with glob patterns so they only activate when the agent is working in a matching directory. A TypeScript review skill stays hidden when the agent operates in a Python repo.

```python
from promptise.engine import Skill, SkillRegistry
from promptise.engine.skills import code_reviewer

reg = SkillRegistry()
reg.register(
    Skill(
        name="ts-reviewer",
        description="Reviews TypeScript / React code.",
        paths=["**/*.tsx", "**/*.ts"],
        factory=lambda **kw: code_reviewer("ts_review", **kw),
    )
)

# Only activates when cwd contains .tsx or .ts files
active = reg.activate_for(cwd=".")
```

---

## Why path scoping?

Modern agents have many skills (web research, code review, data analysis, summarization, etc.). Including all of them in every prompt wastes tokens and confuses the LLM. Path scoping is a cheap, declarative way to keep the active set small and contextual, no code changes needed when the agent moves between projects.

---

## Skill dataclass

```python
@dataclass
class Skill:
    name: str                          # unique identifier
    description: str = ""              # shown to the agent
    paths: list[str] = []             # glob patterns, empty = always active
    factory: Callable[..., Any] = None # returns a node, tool, or any effect
    metadata: dict[str, Any] = {}     # extra frontmatter fields
```

`matches_cwd(cwd)` returns `True` if any file under `cwd` matches any of the globs: first `Path(cwd).glob(pattern)` (so `**/*.ts` searches subdirectories), then `fnmatch` on every path relative to `cwd`, where `*` also matches `/`, so `*.ts` finds `src/app.ts` too. The walk stops at the first match, but a tree with no match is walked in full. Before 1.7.0 the `fnmatch` pass stopped after the first entry, so `*.ts` in practice matched the top level only. Empty `paths` = always active (global skill). A `cwd` that is not a directory matches nothing.

`create(**kwargs)` calls `factory(**kwargs)` and returns what it returns; a skill without a factory raises `ValueError`.

---

## SkillRegistry

| Method | Returns | Description |
|--------|---------|-------------|
| `register(skill)` | `None` | Add a skill; a name that is already registered raises `ValueError` |
| `unregister(name)` | `bool` | Remove by name; `False` when it was not registered |
| `get(name)` | `Skill \| None` | Look up by name |
| `all()` | `list[Skill]` | Every registered skill, in registration order |
| `activate_for(cwd=".")` | `list[Skill]` | The skills whose `paths` match under `cwd` (or are empty) |
| `names_active_for(cwd=".")` | `list[str]` | Their names, sorted |
| `load_directory(directory)` | `int` | Load skill files (below); returns how many were registered |
| `len(reg)`, `"name" in reg` | | Count and membership |

The registry holds no execution state, so one instance can be shared by several agents.

---

## File-based loading with frontmatter

Write a `.py` file that **starts** with a YAML-ish docstring header and defines a `create()` function. The header must be the first thing in the file: a comment, an import or `from __future__` above it means the file is not read as a skill.

```python title="skills/ts_review.py"
"""
name: ts-reviewer
description: Reviews TypeScript / React code.
paths:
  - src/**/*.tsx
  - src/**/*.ts
"""

def create(**kwargs):
    from promptise.engine.skills import code_reviewer
    return code_reviewer("ts_review", **kwargs)
```

Load an entire directory:

```python
from pathlib import Path

from promptise.engine import SkillRegistry

skills_dir = Path("skills")
skills_dir.mkdir(exist_ok=True)
(skills_dir / "ts_review.py").write_text('''"""
name: ts-reviewer
description: Reviews TypeScript / React code.
paths:
  - src/**/*.tsx
  - src/**/*.ts
"""


def create(**kwargs):
    from promptise.engine.skills import code_reviewer
    return code_reviewer("ts_review", **kwargs)
''')

reg = SkillRegistry()
loaded = reg.load_directory(skills_dir)
print(f"Loaded {loaded} skills")                 # Loaded 1 skills
print(reg.names_active_for(cwd="."))             # [] until the cwd has src/**/*.ts files
```

How `load_directory()` treats files:

- Only `*.py` files directly in the directory (no subdirectories), in name order. A missing directory raises `FileNotFoundError`.
- A file without a recognizable header is skipped silently (it is not a skill file).
- The file is imported to get `create`, so **its code runs**: load only directories you trust. A file that fails to import or has no callable `create` is logged and skipped; so is a name that is already registered.
- `name` defaults to the file name without `.py`; a single `paths` string becomes a one-item list.

The frontmatter parser is built in, so no YAML library is needed. It recognizes `name`, `description`, `paths` (as a list), and passes any extra keys through as `metadata`. Scalars are converted: `true`/`yes`/`on`, `false`/`no`/`off`, `null`, integers and floats; quotes around a value are removed. Lines starting with `#` are ignored, and an empty line ends a list.

The same parser is public as `parse_frontmatter(source)`. It reads the first
triple-quoted docstring of a Python source string and returns its `key: value` lines
(and `- item` lists under a key) as a dict, or `None` when there is no docstring or
no recognizable key:

```python
from promptise.engine import parse_frontmatter

source = '''"""
name: ts-reviewer
description: Reviews TypeScript / React code.
paths:
  - src/**/*.tsx
  - src/**/*.ts
"""
'''
print(parse_frontmatter(source))
# {'name': 'ts-reviewer', 'description': 'Reviews TypeScript / React code.',
#  'paths': ['src/**/*.tsx', 'src/**/*.ts']}
print(parse_frontmatter("# a comment first\n" + source))   # None
```

---

## Using with PromptGraph

```python
from promptise.engine import PromptGraph, Skill, SkillRegistry
from promptise.engine.skills import summarizer

reg = SkillRegistry()
reg.register(Skill(name="summary", description="Summarize findings.",
                   factory=lambda **kw: summarizer("summarize", **kw)))   # no paths: always active

graph = PromptGraph("assistant")
for skill in reg.activate_for(cwd="."):
    graph.add_node(skill.create())
print(list(graph.nodes))   # ['summarize']
```

`activate_for()` checks the file system on every call, so call it when the
working directory changes (each run, or each project the agent switches to),
not once at startup.

---

## Related

- [Skills Library](engine-skills.md): built-in skill factories (web_researcher, code_reviewer, etc.)
- [Tool Optimization](tool-optimization.md): semantic tool selection for agents with large tool sets
