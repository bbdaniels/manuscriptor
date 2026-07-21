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
        print("ERROR: zotero-cli not found on PATH. Install it (see ~/.claude memory: reference_zotero_cli.md)")
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
            result = subprocess.run(
                ["zotero-cli", "find-pdf", target],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                print("ok")
                n_fixed += 1
            else:
                print(f"FAILED ({result.stderr.strip()[:80]})")
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
