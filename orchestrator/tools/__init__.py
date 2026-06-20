"""Tool subsystem.

Importing this package registers the core system tools as a side effect, so
``from orchestrator.tools import registry`` always returns a populated registry.
"""

from orchestrator.tools.registry import ToolRegistry, registry

# Side-effect imports: registering each module's tools against the registry.
from orchestrator.tools import system_tools  # noqa: E402,F401
from orchestrator.tools import media_tools  # noqa: E402,F401
from orchestrator.tools import memory_tools  # noqa: E402,F401
from orchestrator.tools import research_tools  # noqa: E402,F401
from orchestrator.tools import utility_tools  # noqa: E402,F401
from orchestrator.tools import model_tools  # noqa: E402,F401
from orchestrator.tools import ops_tools  # noqa: E402,F401
from orchestrator.tools import graph_tools  # noqa: E402,F401

__all__ = ["ToolRegistry", "registry"]
