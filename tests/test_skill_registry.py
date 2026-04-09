"""Tests for the path-scoped skill registry."""
from __future__ import annotations

from pathlib import Path

import pytest

from promptise.engine.skill_registry import (
    Skill,
    SkillRegistry,
    parse_frontmatter,
)


# ---------------------------------------------------------------------------
# Skill (data class)
# ---------------------------------------------------------------------------


class TestSkill:
    def test_empty_paths_always_matches(self, tmp_path):
        skill = Skill(name="global", paths=[])
        assert skill.matches_cwd(tmp_path) is True

    def test_glob_match_against_cwd(self, tmp_path):
        (tmp_path / "main.py").write_text("# code")
        skill = Skill(name="py-only", paths=["*.py"])
        assert skill.matches_cwd(tmp_path) is True

    def test_glob_no_match(self, tmp_path):
        (tmp_path / "README.md").write_text("# docs")
        skill = Skill(name="ts-only", paths=["*.ts", "*.tsx"])
        assert skill.matches_cwd(tmp_path) is False

    def test_recursive_glob_match(self, tmp_path):
        sub = tmp_path / "src" / "deep"
        sub.mkdir(parents=True)
        (sub / "thing.tsx").write_text("// jsx")
        skill = Skill(name="ts", paths=["**/*.tsx"])
        assert skill.matches_cwd(tmp_path) is True

    def test_factory_returns_value(self):
        skill = Skill(
            name="counter",
            factory=lambda **kw: ("hi", kw),
        )
        result = skill.create(extra=1)
        assert result == ("hi", {"extra": 1})

    def test_create_without_factory_raises(self):
        skill = Skill(name="empty")
        with pytest.raises(ValueError, match="no factory"):
            skill.create()


# ---------------------------------------------------------------------------
# SkillRegistry
# ---------------------------------------------------------------------------


class TestSkillRegistry:
    def test_register_and_get(self):
        reg = SkillRegistry()
        skill = Skill(name="reviewer")
        reg.register(skill)
        assert "reviewer" in reg
        assert len(reg) == 1
        assert reg.get("reviewer") is skill

    def test_register_duplicate_raises(self):
        reg = SkillRegistry()
        reg.register(Skill(name="x"))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(Skill(name="x"))

    def test_unregister_removes(self):
        reg = SkillRegistry()
        reg.register(Skill(name="x"))
        assert reg.unregister("x") is True
        assert reg.unregister("x") is False
        assert "x" not in reg

    def test_activate_for_filters_by_paths(self, tmp_path):
        (tmp_path / "main.py").write_text("# code")
        reg = SkillRegistry()
        reg.register(Skill(name="py", paths=["*.py"]))
        reg.register(Skill(name="ts", paths=["*.ts"]))
        reg.register(Skill(name="any", paths=[]))

        active_names = reg.names_active_for(tmp_path)
        assert active_names == ["any", "py"]


# ---------------------------------------------------------------------------
# Frontmatter parser
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_parses_basic_keys(self):
        src = '''"""
name: my-skill
description: Does something useful.
"""'''
        meta = parse_frontmatter(src)
        assert meta is not None
        assert meta["name"] == "my-skill"
        assert meta["description"] == "Does something useful."

    def test_parses_paths_list(self):
        src = '''"""
name: ts-reviewer
description: TypeScript review.
paths:
  - src/**/*.ts
  - src/**/*.tsx
"""'''
        meta = parse_frontmatter(src)
        assert meta is not None
        assert meta["paths"] == ["src/**/*.ts", "src/**/*.tsx"]

    def test_no_docstring_returns_none(self):
        meta = parse_frontmatter("def foo(): pass\n")
        assert meta is None

    def test_docstring_without_known_keys_returns_none(self):
        src = '"""just a docstring"""\n'
        assert parse_frontmatter(src) is None


# ---------------------------------------------------------------------------
# load_directory (file-based loading)
# ---------------------------------------------------------------------------


class TestLoadDirectory:
    def test_loads_skill_files(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "ts_review.py").write_text(
            '"""\n'
            "name: ts-reviewer\n"
            "description: Reviews TypeScript code.\n"
            "paths:\n"
            "  - src/**/*.tsx\n"
            '"""\n'
            "def create(**kwargs):\n"
            "    return {'kind': 'ts', **kwargs}\n"
        )

        reg = SkillRegistry()
        loaded = reg.load_directory(skills_dir)
        assert loaded == 1

        skill = reg.get("ts-reviewer")
        assert skill is not None
        assert skill.description == "Reviews TypeScript code."
        assert skill.paths == ["src/**/*.tsx"]
        assert skill.create(name="x") == {"kind": "ts", "name": "x"}

    def test_skips_non_skill_files(self, tmp_path):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        (skills_dir / "helper.py").write_text(
            '"""just a helper module"""\ndef noop(): pass\n'
        )

        reg = SkillRegistry()
        loaded = reg.load_directory(skills_dir)
        assert loaded == 0

    def test_directory_not_found(self, tmp_path):
        reg = SkillRegistry()
        with pytest.raises(FileNotFoundError):
            reg.load_directory(tmp_path / "missing")
