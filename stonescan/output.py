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
# A failure that happened while NO notifier was installed, and so was never reported anywhere.
# Tracked separately from _failed because the two answer different questions: _failed is "has
# output broken during this run", _unreported is "is there a failure still owed to somebody".
# Conflating them makes every run after a bad one re-report the same old failure.
_unreported = False


def reset(on_first_failure: Callable[[str], None] | None = None) -> None:
    """Start a fresh run. Installs the one-shot notifier and clears the failed flag.

    Called at the top of every run, because the web Refresh button can start a second crawl
    in the same process: without this, a first run that lost its console would leave the flag
    set and the second run would go quietly mute and never report why.

    A failure that happened BEFORE this call is carried across rather than cleared, and this
    is not a detail. `desktop.run_refresh` prints two lines before it reaches `run_all`, so on
    the packaged nightly the console can be gone by the time the notifier exists. Clearing the
    flag there would erase the only evidence the night ever had — the run would proceed mute
    and record nothing about why. So: report it late rather than not at all.
    """
    global _on_first_failure, _failed, _unreported
    _on_first_failure = on_first_failure
    owed = _unreported
    _failed = False
    _unreported = False
    if owed and on_first_failure is not None:
        _notify(RuntimeError("console output was already failing before this run started"))


def _notify(e: BaseException) -> None:
    """Record the first write failure of a run, once. Never raises."""
    global _failed, _unreported
    if _failed:
        return
    _failed = True
    notify = _on_first_failure
    if notify is None:
        # Nobody to tell yet. Hold it for the next reset() rather than dropping it — this is
        # the desktop.run_refresh case, where two lines print before run_all exists.
        _unreported = True
        return
    try:
        notify(f"{type(e).__name__}: {e}")
    except Exception:  # noqa: BLE001 - the notifier must not raise either
        pass


def flush() -> None:
    """Drain stdout now, swallowing whatever that raises.

    Buffering makes a write failure arrive LATE. A pipe is block-buffered, so `say()` can
    return cleanly and the EINVAL surface minutes later when the buffer fills — or, worse, at
    interpreter shutdown, where CPython flushes `sys.stdout` outside anyone's try block, prints
    "Exception ignored" and exits **120**. Measured: `python -c "import os;os.close(1);print('x')"`
    exits 120. A crawl already recorded `done` must not end up reported as a failure by
    `refresh.ps1` because a buffer could not drain, so the drain happens here, in a place that
    can catch it.
    """
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.flush()
        except Exception as e:  # noqa: BLE001 - the whole point
            _notify(e)


def failed() -> bool:
    """Whether output has failed at any point in this run."""
    return _failed


def say(*args, **kwargs) -> None:
    """print(), except that a failed write is not allowed to end the crawl."""
    try:
        print(*args, **kwargs)
    except Exception as e:  # noqa: BLE001 - a write failure is never the crawl's problem
        _notify(e)
