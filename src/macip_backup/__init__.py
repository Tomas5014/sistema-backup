"""Núcleo da prova de conceito de backup da MAC IP."""

from .models import TaskSpec
from .orchestrator import BackupOrchestrator, ExecutionResult

__version__ = "0.2.1"

__all__ = ["BackupOrchestrator", "ExecutionResult", "TaskSpec"]
