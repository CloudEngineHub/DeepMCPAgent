# Runtime API Reference

Agent process lifecycle, triggers, governance (budget, health, mission, secrets), journals, distributed coordination, and the orchestration API.

## Core

### AgentProcess

::: promptise.runtime.process.AgentProcess
    options:
      show_source: false
      heading_level: 4

### AgentRuntime

::: promptise.runtime.runtime.AgentRuntime
    options:
      show_source: false
      heading_level: 4

### AgentContext

::: promptise.runtime.context.AgentContext
    options:
      show_source: false
      heading_level: 4

### StateEntry

::: promptise.runtime.context.StateEntry
    options:
      show_source: false
      heading_level: 4

### ConversationBuffer

::: promptise.runtime.conversation.ConversationBuffer
    options:
      show_source: false
      heading_level: 4

---

## Configuration

### ProcessConfig

::: promptise.runtime.config.ProcessConfig
    options:
      show_source: false
      heading_level: 4

### RuntimeConfig

::: promptise.runtime.config.RuntimeConfig
    options:
      show_source: false
      heading_level: 4

### DistributedConfig

::: promptise.runtime.config.DistributedConfig
    options:
      show_source: false
      heading_level: 4

### ContextConfig

::: promptise.runtime.config.ContextConfig
    options:
      show_source: false
      heading_level: 4

### ExecutionMode

::: promptise.runtime.config.ExecutionMode
    options:
      show_source: false
      heading_level: 4

### OpenModeConfig

::: promptise.runtime.config.OpenModeConfig
    options:
      show_source: false
      heading_level: 4

### EscalationTarget

::: promptise.runtime.config.EscalationTarget
    options:
      show_source: false
      heading_level: 4

### TriggerDeliveryConfig

::: promptise.runtime.config.TriggerDeliveryConfig
    options:
      show_source: false
      heading_level: 4

### HookConfig

::: promptise.runtime.config.HookConfig
    options:
      show_source: false
      heading_level: 4

---

## Lifecycle

### ProcessState

::: promptise.runtime.lifecycle.ProcessState
    options:
      show_source: false
      heading_level: 4

### ProcessLifecycle

::: promptise.runtime.lifecycle.ProcessLifecycle
    options:
      show_source: false
      heading_level: 4

### ProcessTransition

::: promptise.runtime.lifecycle.ProcessTransition
    options:
      show_source: false
      heading_level: 4

### StateError

::: promptise.runtime.lifecycle.StateError
    options:
      show_source: false
      heading_level: 4

---

## Triggers

### TriggerConfig

::: promptise.runtime.config.TriggerConfig
    options:
      show_source: false
      heading_level: 4

### BaseTrigger

::: promptise.runtime.triggers.base.BaseTrigger
    options:
      show_source: false
      heading_level: 4

### TriggerEvent

::: promptise.runtime.triggers.base.TriggerEvent
    options:
      show_source: false
      heading_level: 4

### CronTrigger

::: promptise.runtime.triggers.cron.CronTrigger
    options:
      show_source: false
      heading_level: 4

### WebhookTrigger

::: promptise.runtime.triggers.webhook.WebhookTrigger
    options:
      show_source: false
      heading_level: 4

### FileWatchTrigger

::: promptise.runtime.triggers.file_watch.FileWatchTrigger
    options:
      show_source: false
      heading_level: 4

### EventTrigger

::: promptise.runtime.triggers.event.EventTrigger
    options:
      show_source: false
      heading_level: 4

### MessageTrigger

::: promptise.runtime.triggers.event.MessageTrigger
    options:
      show_source: false
      heading_level: 4

### create_trigger

::: promptise.runtime.triggers.create_trigger
    options:
      show_source: false
      heading_level: 4

### register_trigger_type

::: promptise.runtime.triggers.register_trigger_type
    options:
      show_source: false
      heading_level: 4

### registered_trigger_types

::: promptise.runtime.triggers.registered_trigger_types
    options:
      show_source: false
      heading_level: 4

---

## Governance — Budget

### BudgetConfig

::: promptise.runtime.config.BudgetConfig
    options:
      show_source: false
      heading_level: 4

### ToolCostAnnotation

::: promptise.runtime.config.ToolCostAnnotation
    options:
      show_source: false
      heading_level: 4

### BudgetState

::: promptise.runtime.budget.BudgetState
    options:
      show_source: false
      heading_level: 4

### BudgetEnforcer

::: promptise.runtime.budget.BudgetEnforcer
    options:
      show_source: false
      heading_level: 4

### BudgetViolation

::: promptise.runtime.budget.BudgetViolation
    options:
      show_source: false
      heading_level: 4

### BudgetWarning

::: promptise.runtime.budget.BudgetWarning
    options:
      show_source: false
      heading_level: 4

---

## Governance — Health

### HealthConfig

::: promptise.runtime.config.HealthConfig
    options:
      show_source: false
      heading_level: 4

### HealthMonitor

::: promptise.runtime.health.HealthMonitor
    options:
      show_source: false
      heading_level: 4

### Anomaly

::: promptise.runtime.health.Anomaly
    options:
      show_source: false
      heading_level: 4

### AnomalyType

::: promptise.runtime.health.AnomalyType
    options:
      show_source: false
      heading_level: 4

---

## Governance — Mission

### MissionConfig

::: promptise.runtime.config.MissionConfig
    options:
      show_source: false
      heading_level: 4

### MissionTracker

::: promptise.runtime.mission.MissionTracker
    options:
      show_source: false
      heading_level: 4

### MissionEvidence

::: promptise.runtime.mission.MissionEvidence
    options:
      show_source: false
      heading_level: 4

### MissionEvaluation

::: promptise.runtime.mission.MissionEvaluation
    options:
      show_source: false
      heading_level: 4

### MissionState

::: promptise.runtime.mission.MissionState
    options:
      show_source: false
      heading_level: 4

---

## Governance — Secrets

### SecretScopeConfig

::: promptise.runtime.config.SecretScopeConfig
    options:
      show_source: false
      heading_level: 4

### SecretScope

::: promptise.runtime.secrets.SecretScope
    options:
      show_source: false
      heading_level: 4

### SecretNotFoundError

::: promptise.runtime.secrets.SecretNotFoundError
    options:
      show_source: false
      heading_level: 4

---

## Inbox (Human-in-the-loop)

### InboxConfig

::: promptise.runtime.config.InboxConfig
    options:
      show_source: false
      heading_level: 4

### MessageInbox

::: promptise.runtime.inbox.MessageInbox
    options:
      show_source: false
      heading_level: 4

### InboxMessage

::: promptise.runtime.inbox.InboxMessage
    options:
      show_source: false
      heading_level: 4

### InboxResponse

::: promptise.runtime.inbox.InboxResponse
    options:
      show_source: false
      heading_level: 4

### MessageType

::: promptise.runtime.inbox.MessageType
    options:
      show_source: false
      heading_level: 4

### InboxError

::: promptise.runtime.inbox.InboxError
    options:
      show_source: false
      heading_level: 4

### InvalidInboxMessage

::: promptise.runtime.inbox.InvalidInboxMessage
    options:
      show_source: false
      heading_level: 4

### InboxRateLimitError

::: promptise.runtime.inbox.InboxRateLimitError
    options:
      show_source: false
      heading_level: 4

### InboxFullError

::: promptise.runtime.inbox.InboxFullError
    options:
      show_source: false
      heading_level: 4

### InboxMessageRejected

::: promptise.runtime.inbox.InboxMessageRejected
    options:
      show_source: false
      heading_level: 4

---

## Journal

### JournalConfig

::: promptise.runtime.config.JournalConfig
    options:
      show_source: false
      heading_level: 4

### JournalProvider

::: promptise.runtime.journal.JournalProvider
    options:
      show_source: false
      heading_level: 4

### JournalEntry

::: promptise.runtime.journal.JournalEntry
    options:
      show_source: false
      heading_level: 4

### JournalLevel

::: promptise.runtime.journal.JournalLevel
    options:
      show_source: false
      heading_level: 4

### FileJournal

::: promptise.runtime.journal.FileJournal
    options:
      show_source: false
      heading_level: 4

### InMemoryJournal

::: promptise.runtime.journal.InMemoryJournal
    options:
      show_source: false
      heading_level: 4

### ReplayEngine

::: promptise.runtime.journal.ReplayEngine
    options:
      show_source: false
      heading_level: 4

### RedactingJournal

::: promptise.runtime.journal.RedactingJournal
    options:
      show_source: false
      heading_level: 4

### RewindEngine

::: promptise.runtime.journal.RewindEngine
    options:
      show_source: false
      heading_level: 4

### RewindMode

::: promptise.runtime.journal.RewindMode
    options:
      show_source: false
      heading_level: 4

### RewindPlan

::: promptise.runtime.journal.RewindPlan
    options:
      show_source: false
      heading_level: 4

### RewindResult

::: promptise.runtime.journal.RewindResult
    options:
      show_source: false
      heading_level: 4

---

## Manifests

### AgentManifestSchema

::: promptise.runtime.manifest.AgentManifestSchema
    options:
      show_source: false
      heading_level: 4

### load_manifest

::: promptise.runtime.manifest.load_manifest
    options:
      show_source: false
      heading_level: 4

### save_manifest

::: promptise.runtime.manifest.save_manifest
    options:
      show_source: false
      heading_level: 4

### validate_manifest

::: promptise.runtime.manifest.validate_manifest
    options:
      show_source: false
      heading_level: 4

### manifest_to_process_config

::: promptise.runtime.manifest.manifest_to_process_config
    options:
      show_source: false
      heading_level: 4

---

## Orchestration API

Remote HTTP control of running processes.

### OrchestrationAPI

::: promptise.runtime.api.OrchestrationAPI
    options:
      show_source: false
      heading_level: 4

### OrchestrationClient

::: promptise.runtime.api_client.OrchestrationClient
    options:
      show_source: false
      heading_level: 4

### OrchestrationAPIError

::: promptise.runtime.api_client.OrchestrationAPIError
    options:
      show_source: false
      heading_level: 4

---

## Distributed coordination

### ClusterNode

::: promptise.runtime.distributed.ClusterNode
    options:
      show_source: false
      heading_level: 4

### RuntimeTransport

::: promptise.runtime.distributed.RuntimeTransport
    options:
      show_source: false
      heading_level: 4

### RuntimeCoordinator

::: promptise.runtime.distributed.RuntimeCoordinator
    options:
      show_source: false
      heading_level: 4

### NodeInfo

::: promptise.runtime.distributed.NodeInfo
    options:
      show_source: false
      heading_level: 4

### DiscoveredNode

::: promptise.runtime.distributed.DiscoveredNode
    options:
      show_source: false
      heading_level: 4

### StaticDiscovery

::: promptise.runtime.distributed.StaticDiscovery
    options:
      show_source: false
      heading_level: 4

### RegistryDiscovery

::: promptise.runtime.distributed.RegistryDiscovery
    options:
      show_source: false
      heading_level: 4

---

## Lifecycle hooks

### HookManager

::: promptise.runtime.hooks.HookManager
    options:
      show_source: false
      heading_level: 4

### HookEvent

::: promptise.runtime.hooks.HookEvent
    options:
      show_source: false
      heading_level: 4

### HookContext

::: promptise.runtime.hooks.HookContext
    options:
      show_source: false
      heading_level: 4

### HookBlocked

::: promptise.runtime.hooks.HookBlocked
    options:
      show_source: false
      heading_level: 4

### DispatchResult

::: promptise.runtime.hooks.DispatchResult
    options:
      show_source: false
      heading_level: 4

### ShellHook

::: promptise.runtime.shell_hook.ShellHook
    options:
      show_source: false
      heading_level: 4

### ShellHookResult

::: promptise.runtime.shell_hook.ShellHookResult
    options:
      show_source: false
      heading_level: 4

---

## Dashboard

### RuntimeDashboard

::: promptise.runtime.RuntimeDashboard
    options:
      show_source: false
      heading_level: 4

### RuntimeDashboardState

::: promptise.runtime.RuntimeDashboardState
    options:
      show_source: false
      heading_level: 4

### RuntimeDataCollector

::: promptise.runtime.RuntimeDataCollector
    options:
      show_source: false
      heading_level: 4

---

## Exceptions

### RuntimeBaseError

::: promptise.runtime.exceptions.RuntimeBaseError
    options:
      show_source: false
      heading_level: 4

### ProcessStateError

::: promptise.runtime.exceptions.ProcessStateError
    options:
      show_source: false
      heading_level: 4

### ManifestError

::: promptise.runtime.exceptions.ManifestError
    options:
      show_source: false
      heading_level: 4

### ManifestValidationError

::: promptise.runtime.exceptions.ManifestValidationError
    options:
      show_source: false
      heading_level: 4

### TriggerError

::: promptise.runtime.exceptions.TriggerError
    options:
      show_source: false
      heading_level: 4

### JournalError

::: promptise.runtime.exceptions.JournalError
    options:
      show_source: false
      heading_level: 4
