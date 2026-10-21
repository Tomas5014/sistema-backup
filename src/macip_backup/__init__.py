"""Núcleo da prova de conceito de backup da MAC IP."""

from .models import TaskSpec
from .orchestrator import BackupOrchestrator, ExecutionResult

__all__ = ["BackupOrchestrator", "ExecutionResult", "TaskSpec"]
