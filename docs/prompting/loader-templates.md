---
seo_title: "Prompt YAML Loader and Template Engine"
description: "Load prompts from .prompt YAML files, from URLs or directories, save them back, and use the template engine for variables, conditionals, loops and includes."
---

# Prompt Loader & Templates

Load prompts from `.prompt` YAML files, save prompts back to YAML, and use the built-in template engine for variable interpolation, conditionals, loops, and includes.

## Quick Example

```yaml
# prompts/analyze.prompt
name: analyze
version: "1.0.0"
description: Expert data analysis prompt
author: team-data
tags: [analysis, reporting]

template: |
  Analyze the following data: {text}
  Focus area: {focus}

arguments:
  text:
    description: The data to analyze
    required: true
  focus:
    description: Specific area to focus on
    required: false
    default: general

strategy: chain_of_thought
perspective: analyst
constraints:
  - Must include confidence scores
```

```python
from promptise.prompts.loader import load_prompt

prompt = load_prompt("prompts/analyze.prompt")
result = await prompt(text="quarterly figures...", focus="revenue trends")
```

## YAML Loader

### File Extensions

The loader accepts three extensions:

- `.prompt`
- `.prompt.yaml`
- `.prompt.yml`

### Loading a Single Prompt

```python
from promptise.prompts.loader import load_prompt

prompt = load_prompt("prompts/analyze.prompt")
result = await prompt(text="quarterly figures...")
```

The `register` parameter auto-registers the loaded prompt into the global `PromptRegistry`:

```python
prompt = load_prompt("prompts/analyze.prompt", register=True)
# Now available via registry.get("analyze", "1.0.0")
```

### Loading from a URL

Fetch a `.prompt` file from any HTTP(S) URL:

```python
from promptise.prompts.loader import load_url

prompt = await load_url(
    "https://raw.githubusercontent.com/org/prompts/main/analyze.prompt",
    register=True,
)
result = await prompt(text="quarterly figures...")
```

`load_url()` is async. It refuses URLs whose host resolves to a private, loopback or link-local address (`PromptFileError`), uses a 30-second timeout, and wraps HTTP errors in `PromptFileError`. It needs `httpx`, which Promptise installs.

### Loading a Directory

Recursively load all `.prompt` files from a directory into a local `PromptRegistry`:

```python
from promptise.prompts.loader import load_directory

local_registry = load_directory("prompts/")

# Retrieve prompts from the local registry
analyze = local_registry.get("analyze")
summarize = local_registry.get("summarize", "2.0.0")
```

Pass `register=True` to also register each prompt in the global registry. `load_directory()` scans `**/*.prompt`, `**/*.prompt.yaml` and `**/*.prompt.yml` recursively. Suite files are loaded (so they are validated) but not added to the returned registry, and a second file with the same name and version is skipped. A missing directory raises `PromptFileError`.

### Saving a Prompt to YAML

Export any `Prompt` instance back to a `.prompt` file:

```python
from promptise.prompts.loader import save_prompt

save_prompt(
    my_prompt,
    "prompts/exported.prompt",
    version="2.0.0",
    author="data-team",
    description="Improved analysis prompt",
    tags=["analysis", "v2"],
)
```

The `version`, `author`, `description`, and `tags` parameters override the prompt's stored metadata. `save_prompt()` writes the template, arguments (from the function signature), built-in strategies and perspectives, constraints, the built-in guards, world contexts and a non-default `model`. Blocks, context providers, hooks, custom strategies, custom guards and the return type are not saved.

## Complete YAML Schema

All supported fields for a `.prompt` file:

```yaml
# --- Required ---
name: "my_prompt"              # Unique prompt name; must be a Python identifier
template: "..."                # Prompt template with {variable} placeholders

# --- Model ---
model: "openai:gpt-5-mini"    # LLM model (can be overridden at runtime)

# --- Metadata ---
version: "1.0.0"              # Semantic version
description: "..."            # Human-readable description
author: "team-name"           # Author or team
tags: [tag1, tag2]            # Searchable tags

# --- Arguments (replace the signature taken from the template) ---
arguments:
  variable_name:
    description: "What this argument is"
    required: true             # default true
    default: "fallback"        # string; used when required is false (else "")

# --- Return type (default: str) ---
return_type: "str"             # str, int, float, bool, list, dict

# --- Reasoning ---
strategy: "chain_of_thought"   # Built-in strategy name
perspective: "analyst"         # Built-in perspective name

# --- Constraints ---
constraints:
  - "Be concise"
  - "Under 500 words"

# --- Guards ---
guards:
  - type: "length"
    max_length: 500
  - type: "content_filter"
    blocked: ["secret", "password"]
  - type: "schema_strict"      # no options

# --- Observability ---
observe: false                 # stored on the prompt; no events emitted in 1.6.0

# --- World context ---
world:
  user:
    type: UserContext          # UserContext, EnvironmentContext, ConversationContext,
    name: "Alice"              # TeamContext, ErrorContext, OutputContext or BaseContext
    expertise_level: "expert"
  project:                     # no type: BaseContext
    sprint: "2026-Q1"
    budget: 50000
```

Unknown top-level keys are rejected (`PromptValidationError`, with the pydantic errors in `.errors` and the path in `.file_path`). A single prompt needs `template`; a suite needs `prompts`. Defaults: `version: "0.0.0"`, `model: openai:gpt-5-mini`. The file has no keys for blocks or context providers: put that text in the template, or add them in code with `with_blocks()` / `with_context()` after loading. World contexts are only read by a [context provider](context.md), which you add in code.

### Available Strategy Names

`chain_of_thought`, `structured_reasoning`, `self_critique`, `plan_and_execute`, `decompose`

Composite strategies use a list:

```yaml
strategy: ["chain_of_thought", "self_critique"]
```

### Available Perspective Names

`analyst`, `critic`, `advisor`, `creative`

Custom perspectives use a dict:

```yaml
perspective:
  role: "security auditor"
  instructions: "Focus on OWASP Top 10 vulnerabilities."
```

### Available Guard Types

`content_filter` (`blocked`, `required`), `length` (`min_length`, `max_length`), `schema_strict` (no options). Other keys on a guard entry are ignored; an unknown `type` raises `PromptFileError`.

### Suite Format

When `suite: true`, the file defines multiple prompts sharing defaults:

```yaml
name: data_suite
suite: true
description: Data analysis prompt suite
author: team-data

defaults:
  strategy: "chain_of_thought"
  perspective: "analyst"
  constraints:
    - "Be concise"

prompts:
  summarize:
    template: "Summarize: {text}"
  analyze:
    template: "Analyze: {data}"
    strategy: "structured_reasoning"  # Per-prompt override
```

In a suite file, `defaults` are merged into every entry of `prompts` (the entry wins), and each entry may use any single-prompt key. `load_prompt()` returns a `PromptSuite` instance whose prompts are attributes; `register=True` is ignored for suites.

### Environment Variable Resolution

Every string value in the file (not only the template) supports `${VAR}` and `${VAR:-default}`, resolved from the environment when the file is loaded:

```yaml
template: |
  API endpoint: ${API_BASE_URL:-https://api.example.com}
  Analyze: {text}
```

## Template Engine

The built-in `TemplateEngine` provides variable interpolation, conditionals, loops, and template includes. It is intentionally minimal, not a Jinja2 replacement.

### Variable Interpolation

```python
from promptise.prompts.template import render_template

result = render_template(
    "Summarize in {max_words} words: {text}",
    {"text": "long article...", "max_words": 100},
)
```

### Conditionals

```python
result = render_template(
    "{% if formal %}Use formal language.{% endif %}\nAnalyze: {text}",
    {"formal": True, "text": "data..."},
)
```

With `else` branches:

```python
result = render_template(
    "{% if detailed %}Provide a detailed analysis.{% else %}Be brief.{% endif %}\n{text}",
    {"detailed": False, "text": "data..."},
)
```

### Loops

```python
result = render_template(
    "{% for topic in topics %}Discuss: {topic}\n{% endfor %}",
    {"topics": ["AI safety", "Alignment", "Governance"]},
)
```

### Includes

Register reusable template fragments, then reference them with `{% include "name" %}`:

```python
from promptise.prompts.template import TemplateEngine

engine = TemplateEngine(includes={
    "header": "You are a {role} assistant.",
    "rules": "Rules:\n- Be concise\n- Cite sources",
})

result = engine.render(
    '{% include "header" %}\n{% include "rules" %}\n\nAnalyze: {text}',
    {"role": "research", "text": "quarterly data..."},
)
```

### Literal Braces

Use `{{` and `}}` to output literal curly braces:

```python
result = render_template(
    "Return JSON like: {{\"key\": \"value\"}}\nInput: {text}",
    {"text": "data..."},
)
# Output: Return JSON like: {"key": "value"}
# Input: data...
```

### Processing Order

The engine processes templates in this order:

1. `{% include "name" %}`: resolved from the includes registry (unknown name: `ValueError`)
2. `` !`cmd` ``: shell interpolation, only when a `shell_executor` is set (see [Shell Context Injection](shell-interpolation.md))
3. `{% if %}...{% else %}...{% endif %}`: truthiness of the variable (missing counts as false)
4. `{% for item in items %}...{% endfor %}`: iteration (missing collection: `KeyError`)
5. `{{` / `}}`: literal brace escapes
6. `{variable}`: interpolation (missing variable: `KeyError`; `{a.b}` or `{a[0]}`: `ValueError`)

Conditions and loops take a bare variable name (no expressions, no nesting of the same tag). Inside a loop, only `{item}` is replaced; filters and loop indexes are not supported.

## API Summary

### Loader Functions

| Function | Returns | Description |
|----------|---------|-------------|
| `load_prompt(path, *, register=False)` | `Prompt \| PromptSuite` | Load from a `.prompt` YAML file |
| `await load_url(url, *, register=False)` | `Prompt \| PromptSuite` | Load from a public HTTP(S) URL |
| `save_prompt(prompt, path, *, version=None, author=None, description=None, tags=None)` | `None` | Save a `Prompt` to a YAML file |
| `load_directory(path, *, register=False)` | `PromptRegistry` | Load all `.prompt` files from a directory |

### Template Functions

| Function / Class | Description |
|------------------|-------------|
| `render_template(template, variables, includes=None, *, shell_executor=None)` | Convenience function to render a template |
| `TemplateEngine(includes=None, *, shell_executor=None)` | Template engine with an include registry |
| `TemplateEngine.render(template, variables)` | Render a template with variables |

### Exceptions

| Exception | Description |
|-----------|-------------|
| `PromptFileError` | Error loading or saving a `.prompt` file (missing file, wrong extension, YAML error, unknown strategy, perspective, guard or return type) |
| `PromptValidationError` | Schema validation failed; a `PromptFileError` subclass with `.errors` (list of dicts) and `.file_path` |

!!! tip "Portable prompts"
    YAML prompt files are model-agnostic. Define the prompt structure in YAML, choose the model at runtime, and share prompts across teams and projects.

!!! warning "Missing variables raise KeyError"
    If a template references `{variable}` and it is not in the variables dict, a `KeyError` is raised. A prompt loaded from YAML without an `arguments` section gives every template variable a default of `""`, so a missing argument renders as an empty string; with `arguments`, a `required: true` argument must be passed. Use `required: false` and a `default` for optional variables.

## What's Next

- [Suite & Registry](suite-registry.md): Group and version prompts
- [PromptBuilder](builder.md): Fluent API for runtime prompt construction
- [Inspector](inspector.md): Trace how prompts are assembled
