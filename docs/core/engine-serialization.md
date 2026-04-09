# Serialization & YAML

Save and load graphs as YAML files or Python dicts. Register custom node types for full round-trip support.

## Save a Graph

```python
from promptise.engine.serialization import save_graph

save_graph(graph, "my-agent.yaml")
```

## Load a Graph

```python
from promptise.engine.serialization import load_graph

graph = load_graph("my-agent.yaml")
```

Supports top-level graph config or nested under a `graph` key.

## YAML Format

```yaml
name: research-agent
entry: plan
nodes:
  plan:
    type: plan
    max_subgoals: 4
    quality_threshold: 3
    transitions:
      proceed: search
      replan: plan

  search:
    type: prompt
    instructions: "Search for information using available tools."
    inject_tools: true
    default_next: observe

  observe:
    type: observe
    default_next: verify

  verify:
    type: guard
    on_pass: synthesize
    on_fail: search

  synthesize:
    type: synthesize
    default_next: __end__

edges:
  - from: plan
    to: search
  - from: search
    to: observe
  - from: observe
    to: verify
    label: check_quality
    priority: 5
```

## Python Dict Format

```python
from promptise.engine.serialization import graph_from_config, graph_to_config

# Convert graph to dict
config = graph_to_config(graph)

# Convert dict to graph
graph = graph_from_config(config)
```

### graph_to_config

Returns a dict with:

```python
{
    "name": "research-agent",
    "entry": "plan",
    "nodes": {
        "plan": {"type": "plan", "instructions": "...", ...},
        "search": {"type": "prompt", "inject_tools": True, ...},
    },
    "edges": [
        {"from": "plan", "to": "search"},
        {"from": "search", "to": "observe", "label": "always", "priority": 0},
    ],
}
```

### graph_from_config

Rebuilds the graph from a config dict. Creates nodes using the type registry, adds edges, and sets the entry node.

### node_to_config / node_from_config

Convert individual nodes:

```python
from promptise.engine.serialization import node_to_config, node_from_config

# Serialize a node
config = node_to_config(my_node)
# {"name": "search", "type": "prompt", "instructions": "...", "inject_tools": true, ...}

# Deserialize a node
node = node_from_config(config)
```

## Node Type Registry

All 19 built-in node types are registered for YAML/dict deserialization:

| Type | Node Class | Category |
|------|-----------|----------|
| `prompt` | PromptNode | Standard |
| `tool` | ToolNode | Standard |
| `router` | RouterNode | Standard |
| `guard` | GuardNode | Standard |
| `parallel` | ParallelNode | Standard |
| `loop` | LoopNode | Standard |
| `human` | HumanNode | Standard |
| `transform` | TransformNode | Standard |
| `subgraph` | SubgraphNode | Standard |
| `think` | ThinkNode | Reasoning |
| `reflect` | ReflectNode | Reasoning |
| `observe` | ObserveNode | Reasoning |
| `justify` | JustifyNode | Reasoning |
| `critique` | CritiqueNode | Reasoning |
| `plan` | PlanNode | Reasoning |
| `synthesize` | SynthesizeNode | Reasoning |
| `validate` | ValidateNode | Reasoning |
| `retry` | RetryNode | Reasoning |
| `fan_out` | FanOutNode | Reasoning |

## Custom Node Types

Register custom node types so they can be serialized/deserialized:

```python
from promptise.engine.serialization import register_node_type

class DatabaseNode(BaseNode):
    ...

register_node_type("database", DatabaseNode)

# Now you can use type: database in YAML
```

After registration, YAML files can reference the custom type:

```yaml
nodes:
  fetch_data:
    type: database
    instructions: "Fetch user data"
    default_next: analyze
```

## Edge Config Format

Edges in YAML/dict config support:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `from` | `str` | Yes | Source node name |
| `to` | `str` | Yes | Target node name |
| `label` | `str` | No | Label for visualization |
| `priority` | `int` | No | Priority for condition checking (higher = checked first) |

Conditions are not serializable — conditional edges must be added programmatically after loading:

```python
graph = load_graph("my-agent.yaml")
graph.when("analyze", "retry",
    condition=lambda r: r.output.get("confidence", 0) < 0.5,
    label="low_confidence",
)
```
