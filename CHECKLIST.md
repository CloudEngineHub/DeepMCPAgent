# Promptise Foundry — v1.0.0 Release Checklist

## Pre-Release Checklist

### Code Quality
- [x] 0 TODOs / FIXMEs / HACKs in source
- [x] 0 circular imports
- [x] 0 broken `__all__` exports (346 verified)
- [x] ~3,400+ tests green
- [x] `mkdocs build --strict` — 0 warnings
- [x] Fixed 2 broken Python 3.12 test files
- [x] CHANGELOG updated with all features
- [ ] Run the 2 skipped heavy test suites once (`test_autonomous_swarm.py`, `test_sandbox_integration.py`)

### Documentation
- [x] RAG guide + API reference
- [x] HookManager + ShellHook docs
- [x] RewindEngine docs
- [x] SkillRegistry docs
- [x] AutoApprovalClassifier docs
- [x] Shell interpolation docs
- [x] All new pages in mkdocs nav
- [x] README updated (features, badge, no FastAPI)
- [x] Migration guide updated (v1.0.0, extras, timeline)

### Examples
- [x] `examples/rag/basic_rag.py`
- [x] `examples/hooks/lifecycle_hooks.py`
- [x] `examples/approval/auto_classifier.py`

### Package
- [x] `pip install -e ".[all]"` works
- [x] All new imports resolve
- [x] pyproject.toml URLs ready (`github.com/promptise/foundry`)
- [x] Version via setuptools-scm (tag → `1.0.0`)

---

## Release Day Checklist

### 1. Final Verification
- [ ] `git status` — clean working tree
- [ ] Full test suite green
- [ ] `mkdocs build --strict` — 0 warnings

### 2. Repo Setup
- [ ] Create/transfer repo to `github.com/promptise/foundry`
- [ ] `git push origin main`

### 3. Tag & Version
- [ ] `git tag v1.0.0`
- [ ] `git push origin v1.0.0`
- [ ] Verify version prints `1.0.0`

### 4. Publish to PyPI
- [ ] `python -m build`
- [ ] `twine check dist/*`
- [ ] `twine upload dist/*`
- [ ] `pip install promptise==1.0.0` from clean venv

### 5. Docs Deployment
- [ ] Deploy to GitHub Pages
- [ ] Verify live docs load

### 6. Announce
- [ ] GitHub Release with changelog body
- [ ] Update promptise.com if needed

### 7. Post-Release
- [ ] `pip install promptise` → correct version
- [ ] Run one example against installed package
- [ ] Create v1.0.1 milestone for day-one patches
