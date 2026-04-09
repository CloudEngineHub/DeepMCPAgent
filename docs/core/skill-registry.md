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

Modern agents have many skills (web research, code review, data analysis, summarization, etc.). Including all of them in every prompt wastes tokens and confuses the LLM. Path scoping is a cheap, declarative way to keep the active set small and contextual — no code changes needed when the agent moves between projects.

---

## Skill dataclass

```python
@dataclass
class Skill:
    name: str                          # unique identifier
    description: str = ""              # shown to the agent
    paths: list[str] = []             # glob patterns — empty = always active
    factory: Callable[..., Any] = None # returns a node, tool, or any effect
    metadata: dict[str, Any] = {}     # extra frontmatter fields
```

`matches_cwd(cwd)` returns `True` if any file under `cwd` matches any of the globs. Empty `paths` = always active (global skill).

---

## SkillRegistry

```python
reg = SkillRegistry()
reg.register(skill)                    # add a skill
reg.unregister("name")                 # remove by name
reg.get("name")                        # look up
reg.all()                              # all registered
reg.activate_for(cwd=".")             # path-filtered subset
reg.names_active_for(cwd=".")         # just the names, sorted
```

---

## File-based loading with frontmatter

Write a `.py` file with a YAML-ish docstring header and a `create()` function:

```python
# skills/ts_review.py
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
reg = SkillRegistry()
loaded = reg.load_directory("./.promptise/skills")
print(f"Loaded {loaded} skills")
```

The frontmatter parser is built-in — no YAML library required. It recognizes `name`, `description`, `paths` (as a list), and passes any extra keys through as `metadata`.

---

## Using with PromptGraph

```python
from promptise.engine import PromptGraph

reg = SkillRegistry()
reg.load_directory("./skills")

graph = PromptGraph("assistant")
for skill in reg.activate_for(cwd="."):
    graph.add_node(skill.create())
```

---

## Related

- [Skills Library](engine-skills.md) — built-in skill factories (web_researcher, code_reviewer, etc.)
- [Tool Optimization](tool-optimization.md) — semantic tool selection for agents with large tool sets
