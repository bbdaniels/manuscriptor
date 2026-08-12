"""Repair stage — explicit opt-in to fetch missing PDFs via zotero-cli.

Reads missing.json, iterates each entry, and invokes `zotero-cli find-pdf`
which downloads the PDF into the Zotero library. This is the ONLY code path
in cite-evidence that writes to Zotero — never reached by `build`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def run(*, build_dir: Path) -> int:
    missing_path = build_dir / "missing.json"
    if not missing_path.exists():
        print(f"no missing.json at {missing_path} — nothing to repair")
        return 0
    missing: list[dict] = json.loads(missing_path.read_text(encoding="utf-8"))
    if not missing:
        print("missing.json is empty — nothing to repair")
        return 0

    if not shutil.which("zotero-cli"):
        print("ERROR: zotero-cli is not on PATH. This stage fetches each missing PDF "
              "through it, so install zotero-cli and run the command again.")
        return 2

    print(f"attempting repair on {len(missing)} entries; this writes to your Zotero library")
    print("press Ctrl-C within 5 seconds to cancel")
    import time
    try:
        time.sleep(5)
    except KeyboardInterrupt:
        print("\ncancelled")
        return 1

    n_fixed = 0
    n_absent = 0
    n_error = 0
    n_unfetchable = 0
    for entry in missing:
        cite_key = entry["cite_key"]
        doi = entry.get("doi")
        zot_key = entry.get("zotero_key")
        if not zot_key:
            # Not a failure: there is nothing to look the item up BY, so no
            # lookup was ever attempted. Counting these as failures conflated
            # "tried and failed" with "never attemptable".
            #
            # A DOI is NOT a fallback target here. `item find-pdf` is Zotero's
            # Find Available PDF: it attaches to an item, so it takes a Zotero
            # item key and nothing else. Passing a DOI returns
            # "ERROR: item 10.xxxx/yyyy not found" with exit 0 — an invisible
            # failure that this code counted as a lookup fault.
            #
            # Nor is resolving the DOI to a key the fix. resolve.py already
            # ran that exact lookup (rung 2, `zot.search_by_doi`) before this
            # entry was written to missing.json; a DOI arriving here without a
            # key means that search already came back empty. Repeating it would
            # be a second implementation of a lookup that has already failed.
            # No item means nothing to attach to, so the entry is unfetchable.
            why = "no Zotero item" if doi else "no DOI or Zotero key"
            print(f"  skip {cite_key}: cannot be fetched ({why})")
            n_unfetchable += 1
            continue
        print(f"  find-pdf {cite_key} ({zot_key})...", end=" ", flush=True)
        try:
            # `item find-pdf`, not `find-pdf`: the bare form is a usage error,
            # and it shipped that way from cite-evidence. Every lookup in the
            # first live repair run failed on it, counted as "no PDF found".
            result = subprocess.run(
                ["zotero-cli", "item", "find-pdf", zot_key],
                capture_output=True,
                encoding="utf-8", errors="replace",
                timeout=120,
            )
            # Exit status alone over-reports: `item find-pdf` exits 0 for
            # NOT_FOUND, ERROR and TIMEOUT too, so the verdict is the first
            # token of stdout. The success token is FOUND, not OK: the bridge
            # (cli_anything/zotero/core/jsbridge.py, find_pdf) returns
            # 'FOUND: <key>' / 'NOT_FOUND: …' / 'ERROR: …' / 'TIMEOUT: …'.
            # 'OK: ' belongs to the SIBLING method attach_pdf, and the earlier
            # patch here matched that instead — so a successful fetch fell
            # through to the failure branch and n_fixed could never move.
            verdict = (result.stdout or "").strip()
            if result.returncode == 0 and verdict.startswith("FOUND"):
                print("ok")
                n_fixed += 1
            elif result.returncode == 0 and verdict.startswith("NOT_FOUND"):
                print("NOT_FOUND (no open-access copy)")
                n_absent += 1
            elif result.returncode == 0 and verdict.startswith("TIMEOUT"):
                # Zotero may still be downloading; distinct from "no copy".
                print("TIMEOUT (Zotero still working — retry shortly)")
                n_error += 1
            elif result.returncode == 0 and verdict.startswith("ERROR"):
                # A lookup fault, not an absence: an item key the bridge could
                # not resolve (deleted, or from another library). Note this
                # exits 0, so the failure is invisible in the exit status.
                print(f"ERROR ({verdict[:80]})")
                n_error += 1
            else:
                print(f"FAILED ({(result.stderr or verdict).strip()[:80]})")
                n_error += 1
        except subprocess.TimeoutExpired:
            print("TIMEOUT (subprocess exceeded 120s)")
            n_error += 1
        except FileNotFoundError:
            print("zotero-cli not found")
            return 2

    print(f"\nfixed: {n_fixed}   no open-access copy: {n_absent}   "
          f"errors: {n_error}   cannot be fetched (no Zotero item): {n_unfetchable}")
    if n_fixed:
        print(f"now re-run: manuscriptor evidence {build_dir.parent}/...")
        return 0
    # Nothing was attached, so nothing downstream can have changed. Returning
    # 0 here is what made the server announce "repair finished; re-running the
    # evidence pass" after a run that fixed nothing, and spend a full re-run
    # to arrive at the identical result. The caller (server/app.py
    # repair_handler.then_rerun) treats a non-zero rc as "skip the re-run and
    # report the pass as not-ok" — not as a crash — which is exactly right.
    print("nothing was fetched — the evidence pass would return the same result")
    return 1
