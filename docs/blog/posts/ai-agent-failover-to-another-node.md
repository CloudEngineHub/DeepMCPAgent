---
title: "Fail an AI Agent Over to Another Node After a Crash"
description: "When the box running your agent dies, you don't just want a new agent to spin up — you want the specific crashed process rebuilt where it left off, on a…"
keywords: "ai agent failover to another node, multi-node ai agent, distributed ai agent runtime, agent node failover, resume agent on different machine, high availability ai agent"
date: 2026-07-16
slug: ai-agent-failover-to-another-node
categories:
  - Runtime
---

# Fail an AI Agent Over to Another Node After a Crash

AI agent failover to another node is what you actually need when the machine running a long-lived agent dies mid-run — not a fresh agent that boots with an empty head, but the *specific* crashed process rebuilt where it left off, on a surviving box. Spinning up a replacement is easy. Reconstructing the process that had already scored 128 fraud alerts, advanced a queue cursor, and set its own `pipeline_status = "degraded"` — on a different machine, from durable evidence — is the hard part. This post shows how Promptise Foundry does that with a `RuntimeCoordinator`, static or registry discovery, and a shared journal, with no etcd or Consul in the picture. It also draws an honest line against the distributed runtimes other frameworks ship.

## Why node failover is different from a new agent

A stateless request/response agent needs no failover: if the node hosting it dies, a load balancer routes the next request to another replica and nothing is lost. The problem lives entirely in **long-running, stateful processes** — an [agent process](../../runtime/processes.md) with triggers, a heartbeat, and accumulated context state that has been running for hours.

When the box under that process disappears, three things are true at once:

- **The state is not in the LLM.** The model is stateless. Everything the process learned lives in its context store — counters, cursors, flags — not in any weights.
- **A new agent starts from zero.** Boot a replacement and it re-does completed work, double-fires side effects, and forgets every decision it made.
- **The state has to move machines.** This is the part naive "restart it" designs miss. "Resume agent on different machine" means the survivor must read the dead node's evidence and rebuild *that* process, not begin its own.

So a real multi-node AI agent deployment needs three capabilities together: a way for nodes to know about each other, a way to notice a node has died, and a way for a survivor to reconstruct the orphaned process exactly. Promptise gives you all three as first-class runtime pieces.

## The three pieces of a distributed AI agent runtime

High-availability AI agent failover in Promptise is built from parts you can point at directly, not a black box:

1. **Discovery** — nodes find each other. `StaticDiscovery` for a fixed topology, `RegistryDiscovery` for a dynamic one with TTL-based pruning. See [Service Discovery and Transport](../../runtime/distributed/discovery-transport.md).
2. **Coordination** — the `RuntimeCoordinator` tracks nodes, health-checks them over plain HTTP, aggregates cluster status, and exposes remote start/stop/inject operations. See the [Distributed Coordinator](../../runtime/distributed/coordinator.md) reference.
3. **Reconstruction** — the [journal system](../../runtime/journal/index.md) is the durable record that lets *any* node rebuild a process from its last checkpoint plus the entries after it. This is the same append-only journal behind single-node [durable execution for AI agents](durable-execution-for-ai-agents.md); the distributed case just puts it on shared storage.

The load-bearing idea: the journal is the source of truth, and it is portable. If node B can read node A's journal, node B can become node A for that process. The coordinator's job is to notice A is gone and to drive B; the journal's job is to make the rebuild deterministic.

## Rebuild a crashed process on a surviving node

Here is the core mechanic as a complete, runnable script — no API key, no LLM call. `on_node_a()` does some work, checkpoints, does a little more, then "crashes." `on_node_b()` — pointed at the **same journal path**, standing in for a shared or replicated volume — reconstructs the process from that journal alone:

```python
import asyncio

from promptise.runtime.journal import FileJournal, JournalEntry, ReplayEngine

# In production this path is a SHARED, durable volume both nodes can read:
# an NFS/EFS mount, a multi-attach block device, or an object store synced to disk.
SHARED_JOURNAL = ".promptise/shared-journal"


async def on_node_a() -> None:
    """Node A runs the process, then the box dies mid-cycle."""
    journal = FileJournal(base_path=SHARED_JOURNAL)

    await journal.append(JournalEntry(
        process_id="fraud-scorer",
        entry_type="state_transition",
        data={"from_state": "created", "to_state": "running"},
    ))

    # Last-known-good snapshot after a completed trigger-invoke-result cycle.
    await journal.checkpoint("fraud-scorer", {
        "context_state": {"alerts_scored": 128, "queue_cursor": "2026-07-16T09:00Z"},
        "lifecycle_state": "running",
    })

    # More work happens *after* the checkpoint...
    await journal.append(JournalEntry(
        process_id="fraud-scorer",
        entry_type="context_update",
        data={"key": "alerts_scored", "value": 129},
    ))
    await journal.close()
    # --- CRASH: node A is gone right here ---


async def on_node_b() -> dict:
    """Node B reads the shared journal and rebuilds the crashed process."""
    journal = FileJournal(base_path=SHARED_JOURNAL)
    engine = ReplayEngine(journal)
    recovered = await engine.recover("fraud-scorer")
    await journal.close()
    return recovered


async def main() -> None:
    await on_node_a()
    recovered = await on_node_b()

    print(recovered["lifecycle_state"])   # running
    print(recovered["context_state"])     # {'alerts_scored': 129, 'queue_cursor': '2026-07-16T09:00Z'}
    print(recovered["entries_replayed"])  # 1


asyncio.run(main())
```

Node B recovers `alerts_scored = 129`, not the `128` from the checkpoint — the post-checkpoint `context_update` was replayed on top of the snapshot. That is deterministic reconstruction: given the same journal, every node rebuilds the same state. To finish the failover, node B feeds `recovered["context_state"]` into a fresh `AgentContext(initial_state=...)` and re-hosts the process under its own `AgentRuntime`, as covered on the [Replay Engine](../../runtime/journal/replay.md) page. The process resumes at alert 129 on a new machine — it never re-scores the first 128.

## Wiring it into a live cluster

On a real cluster you do not run the two halves by hand — a coordinator drives them. Each node exposes its runtime over HTTP with a `RuntimeTransport`, the coordinator health-checks every node, and when one goes silent you reconstruct its orphaned process on a healthy survivor:

```python
from promptise.runtime.distributed.discovery import StaticDiscovery
from promptise.runtime.distributed.coordinator import RuntimeCoordinator
from promptise.runtime.journal import FileJournal, ReplayEngine


async def failover(dead_node: str, process: str) -> None:
    # Nodes find each other from a fixed topology (or RegistryDiscovery for a
    # dynamic one with TTL pruning) — no etcd, no Consul, no ZooKeeper.
    discovery = StaticDiscovery(nodes={
        "node-a": "http://10.0.0.1:9100",
        "node-b": "http://10.0.0.2:9100",
    })

    async with RuntimeCoordinator(
        health_check_interval=15.0,
        node_timeout=45.0,          # unhealthy after 3 missed checks
    ) as coordinator:
        for node in await discovery.discover():
            coordinator.register_node(node.node_id, node.url)

        # The background monitor already flagged node-a as unhealthy.
        health = await coordinator.check_health()
        if health[dead_node]["status"] != "healthy":
            survivor = coordinator.healthy_nodes[0]

            # Reconstruct the orphaned process from the shared journal...
            engine = ReplayEngine(FileJournal(base_path=".promptise/shared-journal"))
            recovered = await engine.recover(process)

            # ...then re-host it on the survivor via the transport HTTP API.
            await coordinator.start_process_on_node(survivor.node_id, process)
            print(f"{process} failed over to {survivor.node_id}: {recovered['context_state']}")
```

Two honest points about what the coordinator is. First, it is a set of building blocks, not an auto-healing controller: it detects dead nodes (health checks), tells you which nodes are alive (`healthy_nodes`), and executes remote operations (`start_process_on_node`, `stop_process_on_node`, `inject_event_on_node`). *You* own the failover policy — which survivor adopts the orphan — and the journal owns the deterministic rebuild. Second, all of this rides on `aiohttp`, which ships with the base `pip install promptise`; there is no external coordination service to run. The coordinator itself is a single process, so for true HA you put it behind a load balancer or run coordinator election — that trade-off is documented on the [coordinator](../../runtime/distributed/coordinator.md) page.

## What other frameworks do today

It is worth being precise about the landscape, because "distributed" means different things across these tools.

**Microsoft AutoGen (0.4+)** genuinely ships a distributed agent runtime: a gRPC host acting as a message gateway, with `GrpcWorkerAgentRuntime` workers that route typed messages between agents across processes and machines. That is real, and it is more than LangGraph or CrewAI offer out of the box. But it coordinates **message passing** between agents — it is a transport, not a supervisor. If a worker dies, the in-flight state of the agents it hosted is gone unless you persisted it yourself; there is no built-in notion of reconstructing a *specific named process* from a decision journal on a surviving worker. The delta is exact: cross-node messaging ✓, journal-based failover of a supervised process ✗.

**LangGraph** gives you durable state — its checkpointers persist graph state to SQLite, Postgres, or Redis, and LangGraph Platform adds a managed server with horizontal scaling. So the durable-state half of failover is partially covered: put the checkpointer on shared storage and another worker *can* resume a thread. What the open-source runtime leaves to you is the coordination half — detecting that a node died and rebuilding a specific process on a survivor is your orchestrator's job (Kubernetes, a queue), not a runtime primitive. For a deeper look at where those two persistence models diverge, see [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md). **CrewAI** leaves multi-node coordination and failover to you entirely.

Promptise's edge is not "we have failover and they don't" — it is that agent node failover is **structural** here. Discovery, health-based coordination, and deterministic journal reconstruction of a named process are first-class runtime pieces that compose, with no external coordination infrastructure required.

## Frequently asked questions

### How do I resume an AI agent on a different machine?

Point both machines at the same durable journal (a shared or replicated `FileJournal` volume). When the first node dies, run `ReplayEngine.recover(process_id)` on the survivor to rebuild the process's `context_state` and `lifecycle_state` from the last checkpoint plus later entries, then re-host it under that node's `AgentRuntime`. Because recovery only applies recorded state mutations in order, the rebuild is deterministic across machines.

### Does Promptise need etcd, Consul, or ZooKeeper for multi-node coordination?

No. `StaticDiscovery` and `RegistryDiscovery` handle node discovery in-process, and the `RuntimeCoordinator` health-checks nodes and drives remote operations over plain HTTP with `aiohttp` — which is included in the base install. There is no external coordination service to deploy.

### Does the coordinator fail agents over automatically?

The coordinator detects dead nodes and exposes the primitives — `healthy_nodes`, `start_process_on_node`, and journal reconstruction — but you define the failover policy that decides which survivor adopts an orphaned process. It is a supervisor toolkit, not a leaderless auto-healer, and it is itself a single process, so run it behind a load balancer or with election for true high availability.

## Next steps

Stand up a `RuntimeCoordinator` across two nodes, kill the one running your agent, and watch the survivor rebuild that exact process from its journal — resuming its counters and cursor instead of starting over. Begin with the [Distributed Coordinator](../../runtime/distributed/coordinator.md) and [Service Discovery and Transport](../../runtime/distributed/discovery-transport.md) references to wire the cluster, then read the [journal system](../../runtime/journal/index.md) docs to make each process reconstructable in the first place.
