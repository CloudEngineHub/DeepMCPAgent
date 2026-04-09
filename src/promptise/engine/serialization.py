"""Serialization for PromptGraph nodes and graphs.

Supports converting nodes and graphs to/from configuration dicts
and YAML files.  Enables declarative agent definition via
``.agent`` manifest files.

Example — load graph from YAML::

    graph = load_graph("research-agent.yaml")
    engine = PromptGraphEngine(graph=graph, model=my_model)

Example — save graph to dict::

    config = graph_to_config(graph)
    yaml.dump(config, open("agent.yaml", "w"))
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

from .base import BaseNode
from .graph import PromptGraph

logger = logging.getLogger("promptise.engine")

# Registry of node types for deserialization
_NODE_TYPES: dict[str, type] = {}


def register_node_type(name: str, cls: type) -> None:
    """Register a node type for YAML/dict deserialization."""
    _NODE_TYPES[name] = cls


def _ensure_registry() -> None:
    """Lazy-populate the registry on first use."""
    if _NODE_TYPES:
        return
    from .nodes import (
        GuardNode,
        HumanNode,
        LoopNode,
        ParallelNode,
        PromptNode,
        RouterNode,
        SubgraphNode,
        ToolNode,
        TransformNode,
    )
    from .reasoning_nodes import (
        CritiqueNode,
        FanOutNode,
        JustifyNode,
        ObserveNode,
        PlanNode,
        ReflectNode,
        RetryNode,
        SynthesizeNode,
        ThinkNode,
        ValidateNode,
    )

    _NODE_TYPES.update(
        {
            "prompt": PromptNode,
            "tool": ToolNode,
            "router": RouterNode,
            "guard": GuardNode,
            "parallel": ParallelNode,
            "loop": LoopNode,
            "human": HumanNode,
            "transform": TransformNode,
            # Reasoning nodes
            "think": ThinkNode,
            "reflect": ReflectNode,
            "observe": ObserveNode,
            "justify": JustifyNode,
            "critique": CritiqueNode,
            "plan": PlanNode,
            "synthesize": SynthesizeNode,
            "validate": ValidateNode,
            "retry": RetryNode,
            "fan_out": FanOutNode,
            "subgraph": SubgraphNode,
        }
    )


def node_from_config(config: dict[str, Any]) -> BaseNode:
    """Create a node from a configuration dict.

    The ``type`` field determines which node class is instantiated.
    All other fields are passed as keyword arguments.

    Args:
        config: Node configuration with at minimum ``name`` and ``type``.

    Returns:
        A ``BaseNode`` instance.

    Raises:
        ValueError: If the node type is unknown.
    """
    _ensure_registry()
    node_type = config.get("type", "prompt")
    cls = _NODE_TYPES.get(node_type)

    if cls is None:
        raise ValueError(f"Unknown node type {node_type!r}. Available: {list(_NODE_TYPES.keys())}")

    # Filter config to only include valid constructor params
    name = config.get("name", "unnamed")
    kwargs: dict[str, Any] = {}
    for key, val in config.items():
        if key in ("name", "type"):
            continue
        kwargs[key] = val

    try:
        if hasattr(cls, "from_config"):
            return cls.from_config(config)
        return cls(name, **kwargs)
    except TypeError as exc:
        logger.warning("Failed to create %s node %r: %s", node_type, name, exc)
        # Fallback: create with just name and instructions
        return cls(name, instructions=config.get("instructions", ""))


def node_to_config(node: BaseNode) -> dict[str, Any]:
    """Convert a node to a configuration dict.

    Args:
        node: The node to serialize.

    Returns:
        A dict suitable for YAML serialization.
    """
    config: dict[str, Any] = {
        "name": node.name,
        "type": type(node).__name__.lower().replace("node", ""),
    }

    if node.instructions:
        config["instructions"] = node.instructions
    if node.description:
        config["description"] = node.description
    if node.transitions:
        config["transitions"] = dict(node.transitions)
    if node.default_next:
        config["default_next"] = node.default_next
    if node.max_iterations != 10:
        config["max_iterations"] = node.max_iterations
    if node.metadata:
        config["metadata"] = dict(node.metadata)

    # Type-specific fields
    if hasattr(node, "tools") and node.tools:
        config["tools"] = [t.name for t in node.tools]
    if hasattr(node, "strategy") and node.strategy:
        config["strategy"] = type(node.strategy).__name__
    if hasattr(node, "temperature") and node.temperature != 0.0:
        config["temperature"] = node.temperature
    if hasattr(node, "max_tokens") and node.max_tokens != 4096:
        config["max_tokens"] = node.max_tokens

    return config


def graph_to_config(graph: PromptGraph) -> dict[str, Any]:
    """Convert a PromptGraph to a configuration dict.

    Args:
        graph: The graph to serialize.

    Returns:
        A dict suitable for YAML serialization.
    """
    nodes_config: dict[str, Any] = {}
    for name, node in graph.nodes.items():
        nodes_config[name] = node_to_config(node)

    edges_config: list[dict[str, Any]] = []
    for edge in graph.edges:
        edge_dict: dict[str, Any] = {
            "from": edge.from_node,
            "to": edge.to_node,
        }
        if edge.label:
            edge_dict["label"] = edge.label
        if edge.priority:
            edge_dict["priority"] = edge.priority
        edges_config.append(edge_dict)

    return {
        "name": graph.name,
        "entry": graph.entry,
        "nodes": nodes_config,
        "edges": edges_config,
    }


def graph_from_config(config: dict[str, Any]) -> PromptGraph:
    """Create a PromptGraph from a configuration dict.

    Args:
        config: Graph configuration with ``name``, ``entry``,
            ``nodes``, and optional ``edges``.

    Returns:
        A ``PromptGraph`` instance.
    """
    graph = PromptGraph(name=config.get("name", "graph"))

    # Build nodes
    for name, node_config in config.get("nodes", {}).items():
        if "name" not in node_config:
            node_config["name"] = name
        node = node_from_config(node_config)
        graph.add_node(node)

    # Build edges
    for edge_config in config.get("edges", []):
        graph.add_edge(
            edge_config["from"],
            edge_config["to"],
            label=edge_config.get("label", ""),
            priority=edge_config.get("priority", 0),
        )

    # Set entry
    entry = config.get("entry")
    if entry and graph.has_node(entry):
        graph.set_entry(entry)

    return graph


def load_graph(path: str) -> PromptGraph:
    """Load a PromptGraph from a YAML file.

    Args:
        path: Path to the YAML file.

    Returns:
        A ``PromptGraph`` instance.
    """
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)

    if not isinstance(config, dict):
        raise ValueError(f"Expected dict in YAML file, got {type(config).__name__}")

    # Support both top-level graph config and nested under "graph" key
    if "graph" in config:
        config = config["graph"]

    return graph_from_config(config)


def save_graph(graph: PromptGraph, path: str) -> None:
    """Save a PromptGraph to a YAML file.

    Args:
        graph: The graph to save.
        path: Output file path.
    """
    config = graph_to_config(graph)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)
