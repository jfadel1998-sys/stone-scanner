"""What the Windows scheduled task is actually doing — read, never written.

The nightly task can exist, be misconfigured, and have failed, with nothing in the app any
the wiser. On 2026-07-31 the live task read `LogonType Interactive, RunLevel Limited,
ExecutionTimeLimit PT72H, RestartCount 0` while the installer registers `S4U / Highest /
PT6H / 4 x PT30M`; its XML carried no <ExecutionTimeLimit> element at all, so it had not
been produced by the current installer. That drift sat there for weeks because the only way
to see it was to run PowerShell by hand.

This module reads the task and hands `/health` a plain summary. It deliberately cannot
change anything: registering a task needs elevation, and a web route spawning an elevated
process would be a worse problem than the one it fixes. The page prints the command; a
human runs it.

Everything here is best-effort. A machine with no such task, a non-Windows build, a
localized Windows, or schtasks missing entirely must all produce a readable state rather
than an exception — this is diagnostics, and diagnostics that can break the page they are
diagnosing are worse than none.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field

TASK_NAME = "StoneScannerRefresh"

# What install-refresh-task.ps1 registers, spelled the way the TASK XML spells it — which is
# NOT the way the PowerShell cmdlet spells it. `-RunLevel Highest` is written to XML as
# "HighestAvailable" (and `Limited` as "LeastPrivilege"). Comparing the cmdlet's vocabulary
# against the XML's reports drift on a task that is configured exactly right, which is worse
# than reporting nothing: the page would send someone to re-register a correct task.
EXPECTED = {
    "LogonType": "S4U",
    "RunLevel": "HighestAvailable",
    "ExecutionTimeLimit": "PT6H",
}

def reinstall_command() -> str:
    """The command, with this install's real path — a printed `<project>` placeholder is not
    something anyone can paste. Says elevated, because registering a task needs it and the
    page deliberately will not do it for you."""
    from pathlib import Path
    script = Path(__file__).resolve().parent.parent / "install-refresh-task.ps1"
    return (f'In an ELEVATED PowerShell:  powershell -NoProfile -ExecutionPolicy Bypass '
            f'-File "{script}"')


# Scheduler states that are not failures: not-yet-run, running, queued. Colouring these as
# problems trains people to ignore the colour.
BENIGN_RESULTS = {0x00000000, 0x00041300, 0x00041301, 0x00041303, 0x00041325}

# The two results that mean "registered, has never run". A task in this state reports its
# last run time as Windows' 11/30/1999 sentinel, so a page that prints the field verbatim
# reads "last ran 11/30/1999 12:00:00 AM and has not run yet" — true, and nonsense.
#
# Keyed on the RESULT CODE, not on the date. `schtasks /FO LIST /V` is localized and its date
# format follows the machine's locale, so pattern-matching the sentinel would work here and
# fail somewhere else; the code is the same integer everywhere.
NEVER_RUN_RESULTS = {0x00041300, 0x00041303}

# Task Scheduler reports the same failure under two sign conventions: `schtasks /FO LIST /V`
# prints -2147024629 where Get-ScheduledTaskInfo returns 2147942667, and both are 0x8007010B.
# Normalising to unsigned first means the table needs one entry per condition, not two.
_RESULTS = {
    0x00000000: "completed successfully",
    0x00000001: "the refresh script reported an error",
    0x000000C8: "gave up waiting for the project drive ({drive}) - it was not attached",
    0x00041300: "scheduled, has not run yet",
    0x00041301: "currently running",
    0x00041303: "has not run yet",
    0x00041325: "queued",
    0x8007010B: "the project directory ({drive}) was not available - the drive was detached",
    0x80070002: "a file the task needs was not found",
    0x800704DD: "the task could not run because no user was logged on",
}


def _unsigned(code: int) -> int:
    """Normalise a signed 32-bit result to its unsigned form."""
    return code & 0xFFFFFFFF


def _project_drive() -> str:
    """The drive the project lives on, so a drive-absent message can name it."""
    from pathlib import Path
    try:
        return Path(__file__).resolve().drive or "the project drive"
    except Exception:  # noqa: BLE001
        return "the project drive"


def describe_result(code: int | None) -> str:
    """A human sentence for a LastTaskResult, whichever sign convention it arrived in."""
    if code is None:
        return "unknown"
    u = _unsigned(int(code))
    known = _RESULTS.get(u)
    if known:
        return known.format(drive=_project_drive())
    return f"exited with an unrecognised code (0x{u:08X})"


@dataclass
class TaskState:
    """Everything /health needs. `supported` False means: do not draw this block."""

    supported: bool = True
    installed: bool = False
    reason: str = ""                      # why we cannot say more, when we cannot
    last_run: str = ""
    last_result: int | None = None
    last_result_text: str = ""
    next_run: str = ""
    settings: dict[str, str] = field(default_factory=dict)
    drift: list[str] = field(default_factory=list)   # human phrases, e.g. "RunLevel is Limited, expected Highest"
    command: str = ""
    degraded: bool = False           # installed, but the run fields could not be read

    @property
    def never_run(self) -> bool:
        """Registered but not yet fired — so the page should skip the run time entirely.

        False when `last_result` is None, which is the important half: an unreadable field
        must keep reading as "unknown" rather than becoming a confident "has not run yet".
        Saying "never" when we mean "we could not tell" is the exact overstatement this
        module was written to stop.
        """
        return (self.installed and self.last_result is not None
                and _unsigned(self.last_result) in NEVER_RUN_RESULTS)

    @property
    def failing(self) -> bool:
        """A genuine problem, as opposed to a benign scheduler state or an unread field."""
        return self.installed and (self.degraded
                                   or (self.last_result is not None
                                       and _unsigned(self.last_result) not in BENIGN_RESULTS))


def _run(args: list[str]) -> tuple[int, bytes]:
    # CREATE_NO_WINDOW because the packaged app is built console=False: without it every
    # /health render flashes two console windows at whoever is looking at the page.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        p = subprocess.run(args, capture_output=True, timeout=15, creationflags=flags)
        return p.returncode, (p.stdout or b"")
    except Exception:  # noqa: BLE001 - a missing schtasks must not raise into a page render
        return 1, b""


def _query_xml() -> str:
    """The task definition. schtasks writes this as UTF-16 — decoding it as UTF-8 yields
    mojibake that no XML parser will accept."""
    rc, out = _run(["schtasks", "/query", "/tn", TASK_NAME, "/xml"])
    if rc != 0 or not out:
        return ""
    for enc in ("utf-16", "utf-16-le", "utf-8-sig", "utf-8"):
        try:
            text = out.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if "<" in text:
            return text
    # Last resort, lossy. Despite the XML declaring encoding="UTF-16", schtasks on this
    # machine emits 8-bit console-codepage bytes, so one accented character in the project
    # path or the user name makes every strict decode above fail. Returning "" there would
    # report a live, correctly registered task as "not installed" — the worst possible lie
    # for this block to tell. A mangled character in a path is a far smaller price.
    text = out.decode("utf-8", errors="replace")
    return text if "<" in text else ""


def _parse_xml(xml: str) -> dict[str, str]:
    """Pull the three settings the installer pins. Regex rather than an XML parser because
    the document is namespaced and we want three scalars, not a tree."""
    out: dict[str, str] = {}
    for tag, key in (("LogonType", "LogonType"),
                     ("RunLevel", "RunLevel"),
                     ("ExecutionTimeLimit", "ExecutionTimeLimit")):
        m = re.search(rf"<{tag}>([^<]+)</{tag}>", xml)
        if m:
            out[key] = m.group(1).strip()
    if not xml:
        return out
    # ABSENCE IS A VALUE HERE, and treating it as "nothing to compare" is what made the
    # original drift invisible. Windows omits <RunLevel> entirely when it is LeastPrivilege
    # (the default) and omits <ExecutionTimeLimit> on a task that predates the current
    # installer — which is exactly the state this issue was filed about. A drift check that
    # skips absent keys cannot see either.
    out.setdefault("RunLevel", "LeastPrivilege")
    out.setdefault("ExecutionTimeLimit", "(not set)")
    return out


_LIST_FIELDS = {
    "Last Run Time": "last_run",
    "Last Result": "last_result",
    "Next Run Time": "next_run",
}


def _query_list() -> dict[str, str]:
    """Run times and last result. `/FO LIST /V` is LOCALIZED, so every field here is
    best-effort: on a non-English Windows the labels differ and we simply learn less."""
    rc, out = _run(["schtasks", "/query", "/tn", TASK_NAME, "/fo", "list", "/v"])
    if rc != 0 or not out:
        return {}
    text = out.decode("utf-8", errors="replace")
    if "<" not in text and ":" not in text:
        text = out.decode("utf-16", errors="replace")
    found: dict[str, str] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        label, _, value = line.partition(":")
        key = _LIST_FIELDS.get(label.strip())
        if key and key not in found:
            found[key] = value.strip()
    return found


def check(expected: dict[str, str] | None = None) -> TaskState:
    """Read the task. Never raises."""
    if sys.platform != "win32":
        # build_mac.sh exists; the page must simply not draw this block there.
        return TaskState(supported=False, reason="scheduled tasks are Windows-only")
    try:
        cmd = reinstall_command()
        xml = _query_xml()
        if not xml:
            # Not installed is a NORMAL state: the task is deliberately not auto-installed,
            # so a fresh checkout has none and that is not an error to shout about.
            return TaskState(installed=False, command=cmd,
                             reason="no scheduled task is registered on this machine")
        settings = _parse_xml(xml)
        info = _query_list()
        code = None
        raw = info.get("last_result", "")
        if raw:
            m = re.search(r"-?\d+", raw)
            if m:
                code = int(m.group(0))
        want = EXPECTED if expected is None else expected
        drift = [f"{k} is {settings[k]}, expected {v}"
                 for k, v in want.items()
                 if k in settings and settings[k] != v]
        # The task exists but its run fields did not parse — the localized-labels case AC-6
        # allows. Say "could not read", never "never ran": a task that genuinely has never
        # run reports the 11/30/1999 sentinel, so a blank field ONLY ever means unread. This
        # page exists to stop the app overstating what it knows; it must not start here.
        degraded = not info
        return TaskState(
            installed=True,
            command=cmd,
            degraded=degraded,
            reason=("the task's run history could not be read on this system's locale"
                    if degraded else ""),
            last_run=info.get("last_run", ""),
            last_result=code,
            last_result_text=describe_result(code),
            next_run=info.get("next_run", ""),
            settings=settings,
            drift=drift,
        )
    except Exception as e:  # noqa: BLE001 - diagnostics must never break the page
        return TaskState(installed=False, command=reinstall_command(),
                         reason=f"could not read the task ({type(e).__name__})")
