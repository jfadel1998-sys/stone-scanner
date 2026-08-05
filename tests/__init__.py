"""Test package.

`run_all` folds in an unmerged spill crawl before crawling (AIL-29), and it finds one by
looking at `%ProgramData%\\StoneScanner\\data\\stonescan.db` — a real file on any machine
where the local fallback copy has been built and launched. Every test that calls `run_all`
against a temp database would then merge the live 88k-material catalog into it: slow, and
the results would depend on whether the developer had run `build_exe.ps1`.

Point it at a path that cannot exist, for the whole suite. Tests that exercise the merge
pass an explicit spill database instead.
"""

import os
from pathlib import Path

os.environ.setdefault("STONESCAN_SPILL",
                      str(Path(__file__).resolve().parent / "_no_spill_here" / "nothing.db"))
