# Contributing to Promptise Foundry

Thank you for your interest in contributing to Promptise Foundry! This document provides guidelines and instructions for contributing.

## 🚀 Quick Start

1. **Fork and clone the repository**

```bash
git clone https://github.com/promptise-com/foundry.git
cd promptise
```

2. **Create a virtual environment**

```bash
python3 -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
```

3. **Install development dependencies**

```bash
pip install -e ".[dev,docs]"
```

4. **Create a feature branch**

```bash
git checkout -b feature/your-feature-name
```

## 🧪 Testing

### Run all tests

```bash
pytest tests/ -v
```

### Run tests with coverage

```bash
pytest tests/ -v --cov=src/promptise --cov-report=term-missing
```

### Run specific test files

```bash
# Test SuperAgent features
pytest tests/test_env_resolver.py -v
pytest tests/test_superagent_schema.py -v
pytest tests/test_superagent_loader.py -v
```

### Test CLI commands manually

```bash
# Generate a test config
promptise init -o test.superagent -t basic

# Validate it
promptise validate test.superagent --no-check-env

# Clean up
rm test.superagent
```

## 🔍 Code Quality

### Linting

```bash
# Check code style
ruff check .

# Auto-fix issues
ruff check . --fix

# Check formatting
ruff format --check .

# Apply formatting
ruff format .
```

### Type Checking

```bash
# Type-check the source code
mypy src

# Type-check with strict mode (for new modules)
mypy --strict src/promptise/your_new_module.py
```

### Run all quality checks

```bash
# Lint
ruff check .
ruff format --check .

# Type-check
mypy src

# Test
pytest tests/ -v --cov=src/promptise
```

## 📝 Documentation

### Build documentation locally

```bash
# Install docs dependencies
pip install -e ".[docs]"

# Serve docs locally (live reload)
mkdocs serve

# Build static docs
mkdocs build --strict
```

Visit http://127.0.0.1:8000 to view the documentation.

### Documentation structure

- `docs/` - Documentation source files (Markdown)
- `docs/images/` - Images and diagrams
- `mkdocs.yml` - MkDocs configuration

### Writing documentation

- Use clear, concise language
- Include code examples
- Add diagrams where helpful (Mermaid supported)
- Follow existing style and structure

## 🎯 Contribution Guidelines

### Code Style

- Follow PEP 8 style guide
- Use type hints for all functions
- Write docstrings for all public APIs
- Keep functions focused and small
- Use meaningful variable names

### Commit Messages

Use conventional commits format:

```
type(scope): description

[optional body]

[optional footer]
```

**Types:**
- `feat`: New feature
- `fix`: Bug fix
- `docs`: Documentation changes
- `style`: Code style changes (formatting, etc.)
- `refactor`: Code refactoring
- `test`: Adding or updating tests
- `chore`: Build process, dependencies, etc.

**Examples:**

```
feat(superagent): add template generator command

fix(env-resolver): handle empty default values correctly

docs(readme): update installation instructions

test(loader): add tests for circular reference detection
```

### Pull Request Process

1. **Before submitting:**
   - Run all tests: `pytest tests/ -v`
   - Run linting: `ruff check .`
   - Run type checking: `mypy src`
   - Update documentation if needed
   - Add tests for new features

2. **Submit PR:**
   - Use a clear, descriptive title
   - Reference related issues (e.g., "Fixes #123")
   - Describe what changed and why
   - Include screenshots for UI changes
   - Add examples for new features

3. **After submission:**
   - Watch for CI/CD pipeline results
   - Address review feedback promptly
   - Keep PR focused and small when possible

### What to Contribute

**Good first issues:**
- Documentation improvements
- Bug fixes
- Test coverage improvements
- Example configurations
- Error message improvements

**Feature contributions:**
- New CLI commands
- Additional SuperAgent templates
- Server type implementations
- Cross-agent patterns
- Tool integrations

**Before starting large features:**
- Open an issue to discuss the approach
- Get feedback from maintainers
- Break work into smaller PRs when possible

## 🐛 Reporting Bugs

When reporting bugs, please include:

1. **Environment:**
   - OS and version
   - Python version
   - Foundry version
   - Relevant dependencies

2. **Steps to reproduce:**
   - Minimal code example
   - Configuration files (with secrets removed)
   - Command-line invocation

3. **Expected vs actual behavior:**
   - What should happen
   - What actually happens
   - Error messages or stack traces

4. **Additional context:**
   - Screenshots if applicable
   - Related issues or PRs

## 💡 Suggesting Features

When suggesting features, please include:

1. **Problem statement:**
   - What problem does this solve?
   - Who benefits from this feature?

2. **Proposed solution:**
   - How should it work?
   - API design or user interface
   - Example usage

3. **Alternatives considered:**
   - Other approaches
   - Trade-offs

4. **Additional context:**
   - Related features
   - Similar implementations elsewhere

## 📦 Project Structure

```
promptise/
├── .github/
│   └── workflows/         # CI/CD pipelines
├── docs/                  # Documentation
├── examples/
│   ├── agents/           # Example .superagent files
│   └── servers/          # Example MCP servers
├── src/promptise/
│   ├── agent.py          # Agent builder
│   ├── cli.py            # CLI commands
│   ├── clients.py        # MCP client wrapper
│   ├── config.py         # Server specs
│   ├── cross_agent.py    # Cross-agent communication
│   ├── env_resolver.py   # Environment variable resolution
│   ├── exceptions.py     # Custom exceptions
│   ├── prompt.py         # System prompts
│   ├── superagent.py     # SuperAgent file loader
│   ├── superagent_schema.py  # Pydantic schemas
│   └── tools.py          # Tool loader
├── tests/                # Test suite
├── pyproject.toml        # Project configuration
└── README.md
```

## 🔧 Development Tips

### Running from source

```bash
# Install in editable mode
pip install -e ".[dev]"

# Now changes are reflected immediately
promptise --version
```

### Debugging tests

```bash
# Run with verbose output
pytest tests/ -vv

# Run specific test
pytest tests/test_superagent_loader.py::test_load_valid_file -vv

# Drop into debugger on failure
pytest tests/ -vv --pdb
```

### Debugging CLI

```bash
# Add print statements or use debugger
python -m pdb -m promptise.cli agent test.superagent
```

## 🙋 Getting Help

- **Questions:** Open a [GitHub Discussion](https://github.com/promptise-com/foundry/discussions)
- **Bug reports:** Open a [GitHub Issue](https://github.com/promptise-com/foundry/issues)
- **Feature requests:** Open a [GitHub Issue](https://github.com/promptise-com/foundry/issues)
- **Security issues:** See [SECURITY.md](SECURITY.md)

## 📄 License

By contributing, you agree that your contributions will be licensed under the Apache License 2.0.

## 🙏 Thank You!

Every contribution, no matter how small, is appreciated and helps make Promptise Foundry better for everyone!
