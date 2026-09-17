"""CMP: durable, explicit task memory for Python agents."""
from .memory import (TaskMemory, Task, Evidence, Resolution, ContextPackage,
                     BudgetError, ConflictError, estimated_message_units)
from .harness import (Harness, NativeHarness, NativeBackend, HarnessError,
                      RunSession, RoutedTurn, RoutedHarness, HybridHarness)
from .router import (
    MemoryRoute, Route, RouterConfig, PinnedTask, RouteDecision,
    TaskResolutionError, RoutedContext, MemoryRouter, HybridMemoryRouter,
    QUOTED_EVIDENCE_GUARD,
)

__version__ = "0.4.0a5"
__all__ = ["TaskMemory", "Task", "Evidence", "Resolution", "ContextPackage",
           "BudgetError", "ConflictError", "estimated_message_units", "Harness",
           "NativeHarness", "NativeBackend", "HarnessError", "RunSession",
           "RoutedTurn", "RoutedHarness", "HybridHarness",
           "MemoryRoute", "Route", "RouterConfig", "PinnedTask", "RouteDecision",
           "TaskResolutionError", "RoutedContext", "MemoryRouter",
           "HybridMemoryRouter", "QUOTED_EVIDENCE_GUARD"]
