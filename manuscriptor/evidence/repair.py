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
    n_failed = 0
    for entry in missing:
        cite_key = entry["cite_key"]
        doi = entry.get("doi")
        zot_key = entry.get("zotero_key")
        if not zot_key and not doi:
            print(f"  skip {cite_key}: no Zotero key and no DOI")
            n_failed += 1
            continue
        target = zot_key or doi
        print(f"  find-pdf {cite_key} ({target})...", end=" ", flush=True)
        try:
            # `item find-pdf`, not `find-pdf`: the bare form is a usage error,
            # and it shipped that way from cite-evidence. Every lookup in the
            # first live repair run failed on it, counted as "no PDF found".
            result = subprocess.run(
                ["zotero-cli", "item", "find-pdf", target],
                capture_output=True,
                encoding="utf-8", errors="replace",
                timeout=120,
            )
            # Exit status alone over-reports: `item find-pdf` exits 0 for
            # NOT_FOUND too, and the first live run printed "ok" for lookups
            # that found nothing. The verdict is the first word of stdout.
            verdict = (result.stdout or "").strip()
            if result.returncode == 0 and verdict.startswith("OK"):
                print("ok")
                n_fixed += 1
            elif result.returncode == 0 and verdict.startswith("NOT_FOUND"):
                print("NOT_FOUND (no open-access copy)")
                n_failed += 1
            else:
                print(f"FAILED ({(result.stderr or verdict).strip()[:80]})")
                n_failed += 1
        except subprocess.TimeoutExpired:
            print("TIMEOUT")
            n_failed += 1
        except FileNotFoundError:
            print("zotero-cli not found")
            return 2

    print(f"\nfixed: {n_fixed}   failed: {n_failed}")
    if n_fixed:
        print(f"now re-run: manuscriptor evidence {build_dir.parent}/...")
    return 0
