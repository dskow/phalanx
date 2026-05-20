"""Castrum agents.

Each agent is a LangGraph node with a typed input and output declared
in ``castrum.state``. Agents never call shell, filesystem, or network
APIs directly — every side effect routes through ``castrum.guardrails``.
"""
