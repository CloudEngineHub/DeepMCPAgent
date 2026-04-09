# Sandbox API Reference

Secure sandbox for agent code execution. Provides a zero-configuration sandbox environment for agents to safely execute arbitrary commands and code using container technology with multiple security layers.

## SandboxConfig

::: promptise.sandbox.config.SandboxConfig
    options:
      show_source: false
      heading_level: 3

## NetworkMode

::: promptise.sandbox.config.NetworkMode
    options:
      show_source: false
      heading_level: 3

## SandboxManager

::: promptise.sandbox.manager.SandboxManager
    options:
      show_source: false
      heading_level: 3

## SandboxSession

::: promptise.sandbox.session.SandboxSession
    options:
      show_source: false
      heading_level: 3

## CommandResult

::: promptise.sandbox.session.CommandResult
    options:
      show_source: false
      heading_level: 3

## SandboxBackend

::: promptise.sandbox.backends.SandboxBackend
    options:
      show_source: false
      heading_level: 3

## DockerBackend

::: promptise.sandbox.backends.DockerBackend
    options:
      show_source: false
      heading_level: 3

## Sandbox Tools

LangChain-compatible tools that agents use to interact with the sandbox environment.

### SandboxExecTool

::: promptise.sandbox.tools.SandboxExecTool
    options:
      show_source: false
      heading_level: 4

### SandboxReadFileTool

::: promptise.sandbox.tools.SandboxReadFileTool
    options:
      show_source: false
      heading_level: 4

### SandboxWriteFileTool

::: promptise.sandbox.tools.SandboxWriteFileTool
    options:
      show_source: false
      heading_level: 4

### SandboxListFilesTool

::: promptise.sandbox.tools.SandboxListFilesTool
    options:
      show_source: false
      heading_level: 4

### SandboxInstallPackageTool

::: promptise.sandbox.tools.SandboxInstallPackageTool
    options:
      show_source: false
      heading_level: 4

### create_sandbox_tools

::: promptise.sandbox.tools.create_sandbox_tools
    options:
      show_source: false
      heading_level: 4

## Utilities

### SandboxContainerManager

::: promptise.sandbox.utils.SandboxContainerManager
    options:
      show_source: false
      heading_level: 4

### cleanup_on_exit

::: promptise.sandbox.utils.cleanup_on_exit
    options:
      show_source: false
      heading_level: 4
