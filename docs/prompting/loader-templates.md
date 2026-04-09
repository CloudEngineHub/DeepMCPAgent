# Loader & Templates

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

!!! tip "Requires httpx"
    `load_url()` uses the `httpx` library. Install it with `pip install httpx`.

### Loading a Directory

Recursively load all `.prompt` files from a directory into a local `PromptRegistry`:

```python
from promptise.prompts.loader import load_directory

local_registry = load_directory("prompts/")

# Retrieve prompts from the local registry
analyze = local_registry.get("analyze")
summarize = local_registry.get("summarize", "2.0.0")
```

Pass `register=True` to also register each prompt in the global registry.

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

The `version`, `author`, `description`, and `tags` parameters override the prompt's stored metadata.

## Complete YAML Schema

All supported fields for a `.prompt` file:

```yaml
# --- Required ---
name: "my-prompt"              # Unique prompt identifier
template: "..."                # Prompt template with {variable} placeholders

# --- Model ---
model: "openai:gpt-5-mini"    # LLM model (can be overridden at runtime)

# --- Metadata ---
version: "1.0.0"              # Semantic version
description: "..."            # Human-readable description
author: "team-name"           # Author or team
tags: [tag1, tag2]            # Searchable tags

# --- Arguments ---
arguments:
  variable_name:
    description: "What this argument is"
    required: true             # true or false
    default: "fallback"        # Default value if not required

# --- Return type ---
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
  - type: "schema_strict"
    max_retries: 3

# --- Observability ---
observe: true                  # Enable prompt-level observability

# --- World context ---
world:
  user:
    type: UserContext
    name: "Alice"
    expertise_level: "expert"
  project:
    sprint: "2026-Q1"
    budget: 50000
```

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

`content_filter`, `length`, `schema_strict`

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

### Environment Variable Resolution

Templates support `${VAR}` and `${VAR:-default}` syntax:

```yaml
template: |
  API endpoint: ${API_BASE_URL:-https://api.example.com}
  Analyze: {text}
```

## Template Engine

The built-in `TemplateEngine` provides variable interpolation, conditionals, loops, and template includes. It is intentionally minimal -- not a Jinja2 replacement.

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

1. `{% include "name" %}` -- resolved from the includes registry
2. `{% if %}...{% else %}...{% endif %}` -- truthiness evaluation
3. `{% for item in items %}...{% endfor %}` -- iteration
4. `{{` / `}}` -- literal brace escapes
5. `{variable}` -- interpolation via `str.format_map`

## API Summary

### Loader Functions

| Function | Returns | Description |
|----------|---------|-------------|
| `load_prompt(path, register=False)` | `Prompt \| PromptSuite` | Load from a `.prompt` YAML file |
| `load_url(url, register=False)` | `Prompt \| PromptSuite` | Load from an HTTP(S) URL (async) |
| `save_prompt(prompt, path, ...)` | `None` | Save a `Prompt` to a YAML file |
| `load_directory(path, register=False)` | `PromptRegistry` | Load all `.prompt` files from a directory |

### Template Functions

| Function / Class | Description |
|------------------|-------------|
| `render_template(template, variables, includes=None)` | Convenience function to render a template |
| `TemplateEngine(includes=None)` | Template engine with an include registry |
| `TemplateEngine.render(template, variables)` | Render a template with variables |

### Exceptions

| Exception | Description |
|-----------|-------------|
| `PromptFileError` | Error loading or saving a `.prompt` file |
| `PromptValidationError` | Schema validation failed for a `.prompt` file |

!!! tip "Portable prompts"
    YAML prompt files are model-agnostic. Define the prompt structure in YAML, choose the model at runtime, and share prompts across teams and projects.

!!! warning "Missing variables raise KeyError"
    The template engine uses `str.format_map` internally. If a template references `{variable}` and it is not in the variables dict, a `KeyError` is raised. Use the `arguments` section in YAML files with `required: false` and a `default` to handle optional variables.

## What's Next

- [Suite & Registry](suite-registry.md) -- Group and version prompts
- [PromptBuilder](builder.md) -- Fluent API for runtime prompt construction
- [Inspector](inspector.md) -- Trace how prompts are assembled
