"""Rich-progress UI helpers for the scan pipeline.

Centralises the column layout so all stages (CDX scan, well-known probing,
fetch+extract) share one consistent look. Also exposes a small ``ProgressUI``
wrapper that the pipeline can use whether progress is on or off — when
``enabled=False`` it becomes a no-op so library callers (and tests) get a
silent run by default.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)


def _make_progress(console: Console, enabled: bool) -> Progress:
    return Progress(
        SpinnerColumn(style="cyan"),
        TextColumn("{task.description}"),
        BarColumn(bar_width=30),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        TextColumn("[yellow]{task.fields[timeout_left]}"),
        TextColumn("[dim]{task.fields[postfix]}"),
        console=console,
        transient=False,
        disable=not enabled,
        refresh_per_second=8,
    )


class ProgressUI:
    """Thin context-managed wrapper around ``rich.progress.Progress``.

    Each call site adds and updates tasks; ``ProgressUI.status(...)`` prints
    a one-shot stderr line above the live progress (used for stage markers
    like ``[*] Querying CDX index…``).

    When ``enabled=False`` everything is a no-op except status lines, which
    still go to stderr so library users see ``[*]`` markers in CI logs.
    """

    def __init__(self, enabled: bool, verbose: bool = False):
        self.enabled = enabled
        self.verbose = verbose
        self.console = Console(stderr=True, force_terminal=enabled if enabled else None)
        self._progress = _make_progress(self.console, enabled)

    # Lifecycle ----------------------------------------------------------

    def __enter__(self) -> "ProgressUI":
        if self.enabled:
            self._progress.__enter__()
        return self

    def __exit__(self, *exc) -> None:
        if self.enabled:
            self._progress.__exit__(*exc)

    # Tasks --------------------------------------------------------------

    def add_task(self, description: str, total: int | None = None) -> TaskID | None:
        if not self.enabled:
            return None
        return self._progress.add_task(
            description, total=total, postfix="", timeout_left=""
        )

    def set_timeout_left(
        self,
        task: TaskID | None,
        *,
        deadline: float | None,
        timeout_seconds: float,
    ) -> None:
        """Update the ``timeout_left`` column for ``task``.

        Pass ``deadline=None`` (or ``timeout_seconds <= 0``) to clear the
        countdown — used for unlimited (``--exhaustive``-style) runs.
        """
        if task is None or not self.enabled:
            return
        import math
        import time as _time

        if deadline is None or timeout_seconds <= 0 or math.isinf(deadline):
            text = ""
        else:
            remaining = max(0.0, deadline - _time.monotonic())
            text = f"{remaining:.0f}s left / {timeout_seconds:.0f}s"
        self._progress.update(task, timeout_left=text)

    def advance(self, task: TaskID | None, n: int = 1) -> None:
        if task is None:
            return
        self._progress.advance(task, n)

    def update(self, task: TaskID | None, **fields: Any) -> None:
        if task is None:
            return
        # rich's update accepts arbitrary task fields; we store postfix this way.
        if "postfix" in fields:
            self._progress.update(task, postfix=fields.pop("postfix"))
        if fields:
            self._progress.update(task, **fields)

    def stop_task(self, task: TaskID | None) -> None:
        if task is None:
            return
        self._progress.stop_task(task)

    # Messaging ----------------------------------------------------------

    def status(self, line: str) -> None:
        """Print a static status line above the live progress display.

        Used for stage markers like ``[*] Querying CDX index…`` — visible
        whether progress bars are enabled or not.
        """
        if self.enabled:
            self.console.print(line)
        else:
            # When disabled we still want stage markers visible in logs.
            import sys

            print(line, file=sys.stderr, flush=True)

    def announce_contact(self, kind: str, value: str, date: str, url: str) -> None:
        """Live per-contact feed — interleaves cleanly with the bar.

        Default: one line — ``+ kind  value``. With ``verbose=True`` an extra
        dim line with ``date`` and the full snapshot URL is printed beneath.
        """
        if not self.enabled:
            return
        self.console.print(f"  [bold green]+[/] {kind:5}  {value}")
        if self.verbose:
            self.console.print(f"      [dim]{date}  {url}[/]")
