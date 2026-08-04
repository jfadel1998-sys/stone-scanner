"""Console output that cannot kill the work it is describing.

On 2026-08-03 a three-hour crawl committed every row, then died on a cosmetic `print()` with
`OSError: [Errno 22]` — thirteen lines before `reconcile_rejections`. The run was recorded
FAILED despite having succeeded, and the 249 auto-rejections it had earned went unstamped.

The pipe was broken from the other end: `refresh.ps1` pipes the child's stdout through
`Add-Content` into a log on the removable project drive under `$ErrorActionPreference =
"Stop"`, so one failed write terminates the parent. On Windows the child's next write to the
dead pipe surfaces as EINVAL, not EPIPE — which is why it read as a nonsense argument error
rather than anything to do with a pipe.

`say()` is `print()` that swallows whatever the write raises. Not just OSError: a supplier
name containing a character the console's cp1252 codepage can't encode raises
UnicodeEncodeError, which is the same bug wearing a different coat. Nothing a *write* raises
is ever the crawl's problem.

This module deliberately imports nothing from the package, so any module can use it without
risking an import cycle.
"""

from __future__ import annotations

from typing import Callable

# Set once per run by the orchestrator. Called with a short description the first time output
# fails, and never again — the caller routes it somewhere durable (refresh-history.log), and
# a crawl printing per supplier would otherwise fill that file with the same line.
_on_first_failure: Callable[[str], None] | None = None
_failed = False


def reset(on_first_failure: Callable[[str], None] | None = None) -> None:
    """Start a fresh run. Clears the failed flag and installs the one-shot notifier.

    Called at the top of every run, because the web Refresh button can start a second crawl
    in the same process: without this, a first run that lost its console would leave the flag
    set and the second run would go quietly mute and never report why.
    """
    global _on_first_failure, _failed
    _on_first_failure = on_first_failure
    _failed = False


def failed() -> bool:
    """Whether output has failed at any point in this run."""
    return _failed


def say(*args, **kwargs) -> None:
    """print(), except that a failed write is not allowed to end the crawl."""
    global _failed
    try:
        print(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - a write failure is never the crawl's problem
        if not _failed:
            _failed = True
            notify = _on_first_failure
            if notify is not None:
                try:
                    notify(f"{type(e).__name__}: {e}")
                except Exception:  # noqa: BLE001 - the notifier must not raise either
                    pass
