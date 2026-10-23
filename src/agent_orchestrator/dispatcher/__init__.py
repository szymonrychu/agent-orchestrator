"""The dispatcher process: queued messages to agents, round-robin (D4)."""

from agent_orchestrator.dispatcher.loop import MAX_IN_FLIGHT, Dispatcher

__all__ = ["MAX_IN_FLIGHT", "Dispatcher"]
