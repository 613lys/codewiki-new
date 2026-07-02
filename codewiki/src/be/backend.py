"""Task backend abstraction for CodeWiki generation work.

CodeWiki has two task shapes:

* a synchronous single-shot completion (clustering, parent / repo overviews)
* a per-module documentation task

The maintained implementation is :class:`TaskBackend`, which writes local
task/result files.
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, Any, Dict, List

if TYPE_CHECKING:
    from codewiki.src.be.dependency_analyzer.models.core import Node


class PendingTask(RuntimeError):
    """Raised when one or more tasks need result files."""


class TaskBackendBase(abc.ABC):
    """Abstract task backend used by the documentation generator."""

    @abc.abstractmethod
    def complete(
        self,
        prompt: str,
        *,
        model: str | None = None,
        temperature: float = 0.0,
    ) -> str:
        """Single-shot text completion."""

    @abc.abstractmethod
    async def run_module_agent(
        self,
        module_name: str,
        components: Dict[str, "Node"],
        core_component_ids: List[str],
        module_path: List[str],
        working_dir: str,
    ) -> Dict[str, Any]:
        """Run the per-module agent loop.  Returns the updated module_tree dict."""

    async def plan_submodules(
        self,
        module_name: str,
        components: Dict[str, "Node"],
        core_component_ids: List[str],
        module_path: List[str],
        working_dir: str,
        module_tree: Dict[str, Any],
    ) -> Dict[str, Any] | None:
        """Optionally plan child modules.

        Backends that do not support explicit planning return ``None`` so the
        original agent/tool workflow remains unchanged.
        """
        return None


def get_backend(config) -> "TaskBackendBase":
    """Return the local task backend."""
    from codewiki.src.be.task_backend import TaskBackend
    return TaskBackend(config)
