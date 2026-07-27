"""Process-agnostic Hermes runtime services.

This package is the low-level authority shared by the agent, tools, gateway,
plugins, TUI, and CLI entry points.  Importing it must not start a service,
parse command-line arguments, or mutate process-wide streams.
"""
