"""core.statemachine — State-machine engine for the migration pipeline.

Drives ``TaskContext`` through its state transitions.  Each state has a
registered handler function with signature ``(TaskContext) -> TaskContext``.
The handler is responsible for:
  1. Executing the stage logic (LLM calls, tool invocations, user prompts).
  2. Persisting its artifact via ``task.save_artifact()``.
  3. Setting ``task.current_state`` to the next state.

The engine calls ``task.save()`` after every handler returns, so the task
manifest is always up-to-date on disk and can be resumed after a crash.
"""
from __future__ import annotations

from typing import Callable

from .task import TaskContext, TaskState, TaskStatus
from .llm import record_trajectory_action

HandlerFn = Callable[[TaskContext], TaskContext]

# Maximum iterations to guard against infinite loops (e.g. DEBUG ↔ PATCH).
_MAX_ITERATIONS = 100


class StateMachine:
    """Drives a TaskContext through its state handlers until DONE."""

    def __init__(
        self,
        task: TaskContext,
        handlers: dict[TaskState, HandlerFn],
    ) -> None:
        self.task = task
        self.handlers = handlers

    def run(self) -> TaskContext:
        """Execute handlers until the task reaches DONE (or iteration cap)."""
        iterations = 0
        while self.task.current_state != TaskState.DONE:
            if iterations >= _MAX_ITERATIONS:
                print(f"[statemachine] iteration cap ({_MAX_ITERATIONS}) reached "
                      f"at state {self.task.current_state.value}, forcing DONE.")
                self.task.current_state = TaskState.DONE
                break

            state = self.task.current_state
            handler = self.handlers.get(state)
            if handler is None:
                raise RuntimeError(
                    f"No handler registered for state {state.value}"
                )

            prev_state = state
            self.task = handler(self.task)
            iterations += 1

            # Persist after every transition
            self.task.save()

            # Safety: if handler forgot to advance state, force DONE.
            # Allow a controlled PATCH self-transition when rollback_hint is set.
            if self.task.current_state == prev_state:
                hint = getattr(self.task, "rollback_hint", "")
                if state == TaskState.PATCH and hint == "generate":
                    print(f"[statemachine] controlled PATCH self-transition with rollback_hint={hint}.")
                    continue
                msg = f"handler_stall:{state.value}"
                print(f"[statemachine][ERROR] handler for {state.value} did not advance state.")
                record_trajectory_action("statemachine_error", msg)
                # Fail-safe: surface as task-level failure instead of silent DONE.
                try:
                    self.task.task.status = TaskStatus.FAILED
                except Exception:
                    pass
                # Prefer TASK_UPDATE so the pipeline can emit a report/summary.
                self.task.current_state = TaskState.TASK_UPDATE

        # Final persist
        self.task.save()
        return self.task
