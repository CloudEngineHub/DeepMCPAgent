"""SkillRegistry — path-scoped activation of agent skills.

This module is an additive layer on top of :mod:`promptise.engine.skills`.
It lets you register skills with a ``paths`` glob list — a set of
directory patterns — and then activate only the subset that match the
current working directory. The agent only sees skills relevant to the
codebase or context it is operating in.

Why? Modern agentic frameworks have to make a tradeoff between the
power of "many tools / many skills" and the cost (and confusion) of
including them all in every prompt. Path scoping is a cheap, declarative
way to keep the active set small and contextual: a skill that only
makes sense in a TypeScript repo is hidden when the agent is working in
a Python project.

Two ways to declare a skill:

1. **Programmatic** — instantiate :class:`Skill` directly::

       from promptise.engine.skill_registry import Skill, SkillRegistry
       from promptise.engine.skills import code_reviewer

       reg = SkillRegistry()
       reg.register(
           Skill(
               name="ts-reviewer",
               description="Reviews TypeScript / React code.",
               paths=["src/**/*.tsx", "src/**/*.ts"],
               factory=lambda **kw: code_reviewer("ts_review", **kw),
           )
       )

2. **From files** — write Python files with a YAML-style frontmatter
   header in a docstring and load them from a directory::

       \"\"\"
       name: ts-reviewer
       description: Reviews TypeScript / React code.
       paths:
         - src/**/*.tsx
         - src/**/*.ts
       \"\"\"

       def create(**kwargs):
           from promptise.engine.skills import code_reviewer
           return code_reviewer("ts_review", **kwargs)

   Then::

       reg = SkillRegistry()
       reg.load_directory("./.promptise/skills")

Activation::

    active = reg.activate_for(cwd=".")
    # active is a list[Skill] whose paths matched something under cwd
    nodes = [skill.create() for skill in active]
"""

from __future__ import annotations

import importlib.util
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """A registered skill with optional path-scoped activation.

    Attributes:
        name: Unique skill identifier.
        description: Short human-readable summary, surfaced in the
            agent's tool / skill list.
        paths: Glob patterns. The skill activates if the current
            working directory contains *any* file matching any of
            these globs (relative to ``cwd``). Empty list means the
            skill is *always active*.
        factory: A callable that produces the skill's effect. The
            return value is opaque — it might be a node, a tool, a
            prompt block, or anything your framework consumes. The
            registry just hands it to the caller of :meth:`create`.
        metadata: Free-form extra fields parsed from frontmatter.
    """

    name: str
    description: str = ""
    paths: list[str] = field(default_factory=list)
    factory: Callable[..., Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches_cwd(self, cwd: str | Path) -> bool:
        """True if any file under ``cwd`` matches one of ``self.paths``.

        An empty paths list always matches (skill is global).
        """
        if not self.paths:
            return True

        root = Path(cwd).resolve()
        if not root.is_dir():
            return False

        for pattern in self.paths:
            # ``Path.glob`` understands ``**``; for simple file names we
            # also try fnmatch on relative paths to be permissive.
            try:
                hit = next(root.glob(pattern), None)
            except (NotImplementedError, ValueError):
                hit = None
            if hit is not None:
                return True
            # Fallback: walk + fnmatch (slower, but copes with patterns
            # that Path.glob doesn't accept like leading "**/").
            for p in root.rglob("*"):
                rel = p.relative_to(root).as_posix()
                if fnmatch(rel, pattern):
                    return True
                break  # don't iterate the entire tree, only confirm
        return False

    def create(self, **kwargs: Any) -> Any:
        """Invoke the factory and return its result.

        Raises :class:`ValueError` if no factory was registered.
        """
        if self.factory is None:
            raise ValueError(f"Skill {self.name!r} has no factory")
        return self.factory(**kwargs)


class SkillRegistry:
    """In-process registry of :class:`Skill` objects.

    Supports programmatic registration, file-based loading, and
    path-scoped activation. Cheap to construct and freely shareable
    across agents — it holds no execution state.
    """

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    # -- Registration --

    def register(self, skill: Skill) -> None:
        """Add a skill to the registry.

        Raises:
            ValueError: If a skill with the same name is already
                registered.
        """
        if skill.name in self._skills:
            raise ValueError(f"skill {skill.name!r} is already registered")
        self._skills[skill.name] = skill

    def unregister(self, name: str) -> bool:
        """Remove a skill by name. Returns True if found, False otherwise."""
        return self._skills.pop(name, None) is not None

    def get(self, name: str) -> Skill | None:
        """Look up a skill by name; returns None if missing."""
        return self._skills.get(name)

    def all(self) -> list[Skill]:
        """All registered skills (any scope)."""
        return list(self._skills.values())

    def __len__(self) -> int:
        return len(self._skills)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._skills

    # -- Activation --

    def activate_for(self, cwd: str | Path = ".") -> list[Skill]:
        """Return the subset of skills that activate for ``cwd``.

        A skill is "active" if its ``paths`` are empty (global) OR
        any one of its globs matches a file under ``cwd``.
        """
        return [s for s in self._skills.values() if s.matches_cwd(cwd)]

    def names_active_for(self, cwd: str | Path = ".") -> list[str]:
        """Return only the names of active skills, sorted alphabetically."""
        return sorted(s.name for s in self.activate_for(cwd))

    # -- File-based loading --

    def load_directory(self, directory: str | Path) -> int:
        """Load every ``*.py`` file in ``directory`` as a skill.

        Each file must contain a top-level docstring with frontmatter
        in the form::

            \"\"\"
            name: my-skill
            description: ...
            paths:
              - "src/**/*.ts"
            \"\"\"

        The file must also expose a ``create(**kwargs)`` function that
        returns the skill's effect (a node, tool, etc.).

        Args:
            directory: Path to scan. Subdirectories are not recursed.

        Returns:
            Number of skills loaded.

        Raises:
            FileNotFoundError: If ``directory`` does not exist.
        """
        d = Path(directory)
        if not d.is_dir():
            raise FileNotFoundError(f"skill directory {d} does not exist")

        loaded = 0
        for py_file in sorted(d.glob("*.py")):
            try:
                skill = self._load_file(py_file)
            except Exception:  # noqa: BLE001
                logger.exception("failed to load skill file %s", py_file)
                continue
            if skill is None:
                continue
            try:
                self.register(skill)
                loaded += 1
            except ValueError as exc:
                logger.warning("skipping skill %s: %s", py_file.name, exc)
        return loaded

    @staticmethod
    def _load_file(path: Path) -> Skill | None:
        """Import a Python file and turn it into a Skill, or return None.

        Returns None if the file has no parseable frontmatter — that's
        treated as "this isn't a skill file" rather than an error.
        """
        text = path.read_text(encoding="utf-8")
        frontmatter = parse_frontmatter(text)
        if frontmatter is None:
            return None

        name = frontmatter.get("name") or path.stem
        description = str(frontmatter.get("description", ""))
        paths = frontmatter.get("paths") or []
        if isinstance(paths, str):
            paths = [paths]
        if not isinstance(paths, list):
            raise ValueError(f"{path.name}: 'paths' must be a list of strings")

        # Import the file as an isolated module
        spec = importlib.util.spec_from_file_location(f"_promptise_skill_{path.stem}", path)
        if spec is None or spec.loader is None:
            raise ImportError(f"could not import skill file {path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        factory = getattr(module, "create", None)
        if factory is None or not callable(factory):
            raise AttributeError(
                f"{path.name}: skill module must define a callable `create(**kwargs)`"
            )

        return Skill(
            name=name,
            description=description,
            paths=[str(p) for p in paths],
            factory=factory,
            metadata={
                k: v for k, v in frontmatter.items() if k not in {"name", "description", "paths"}
            },
        )


# ---------------------------------------------------------------------------
# Frontmatter parsing — minimal YAML-ish, no external deps
# ---------------------------------------------------------------------------


_DOCSTRING_RE = re.compile(r'^\s*(?:"""|\'\'\')(.+?)(?:"""|\'\'\')', re.DOTALL)


def parse_frontmatter(source: str) -> dict[str, Any] | None:
    """Extract a YAML-ish frontmatter dict from a Python source file.

    Looks at the *first* triple-quoted docstring in ``source`` and
    parses ``key: value`` lines plus simple ``- list`` items beneath
    a ``key:`` heading. Returns None if the file has no docstring or
    if no recognizable frontmatter keys are found.

    This is intentionally tiny — for full YAML, register the skill
    programmatically instead.
    """
    m = _DOCSTRING_RE.match(source)
    if not m:
        return None
    body = m.group(1).strip()

    result: dict[str, Any] = {}
    current_list_key: str | None = None

    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            current_list_key = None
            continue
        if line.lstrip().startswith("#"):
            continue

        # List item under a list key
        stripped = line.lstrip()
        if stripped.startswith("- ") and current_list_key is not None:
            value = stripped[2:].strip().strip("\"'")
            result[current_list_key].append(value)
            continue

        # Key: value
        if ":" in line and not line.startswith(" "):
            key, _, value = line.partition(":")
            key = key.strip()
            value = value.strip()
            if not value:
                # Start of a list
                result[key] = []
                current_list_key = key
            else:
                result[key] = _coerce(value)
                current_list_key = None
        # Anything else is ignored

    if not any(k in result for k in ("name", "description", "paths")):
        return None
    return result


def _coerce(raw: str) -> Any:
    """Coerce a frontmatter scalar to a Python value."""
    s = raw.strip().strip("\"'")
    if s.lower() in {"true", "yes", "on"}:
        return True
    if s.lower() in {"false", "no", "off"}:
        return False
    if s.lower() == "null" or s == "":
        return None
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        pass
    return s


__all__ = ["Skill", "SkillRegistry", "parse_frontmatter"]
