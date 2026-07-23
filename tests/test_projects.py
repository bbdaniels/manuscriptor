# tests/test_projects.py
from __future__ import annotations
import json
from pathlib import Path
from manuscriptor.source.projects import list_projects, _cwds_from_frontmatter, _manuscript_roots

DOC = "\\documentclass{article}\n\\begin{document}\nhi\n\\end{document}\n"

def _mk_project(vault: Path, name: str, cwds: list[str]):
    d = vault / name
    d.mkdir(parents=True)
    body = "---\ncwds:\n" + "".join(f"  - {c}\n" for c in cwds) + "---\n# Tasks\n"
    (d / "Tasks.md").write_text(body)

def test_frontmatter_cwds_parses_block_list():
    fm = "---\ncwds:\n  - /a/b\n  - /a/b/**\nreads:\n  - /x\n---\nbody"
    assert _cwds_from_frontmatter(fm) == ["/a/b", "/a/b/**"]

def test_no_frontmatter_returns_empty():
    assert _cwds_from_frontmatter("# Tasks\nno frontmatter") == []

def test_manuscript_roots_finds_documentclass_in_subdir(tmp_path: Path):
    proj = tmp_path / "proj"; (proj / "manuscript").mkdir(parents=True)
    (proj / "manuscript" / "main.tex").write_text(DOC)
    roots = _manuscript_roots(proj)
    assert roots == [proj / "manuscript"]

def test_list_projects_maps_name_to_root(tmp_path: Path):
    vault = tmp_path / "vault"; vault.mkdir()
    code = tmp_path / "estonia-ecm" / "latex"; code.mkdir(parents=True)
    (code / "main.tex").write_text(DOC)
    _mk_project(vault, "Estonia ECM", [str(tmp_path / "estonia-ecm"), str(tmp_path / "estonia-ecm") + "/**"])
    got = list_projects(vault)
    assert got == [{"name": "Estonia ECM", "root": str(code), "main": "main.tex"}]

def test_missing_vault_is_empty_not_error(tmp_path: Path):
    assert list_projects(tmp_path / "nope") == []

def test_project_without_manuscript_is_skipped(tmp_path: Path):
    vault = tmp_path / "v"; vault.mkdir()
    empty = tmp_path / "empty"; empty.mkdir()
    _mk_project(vault, "No Paper", [str(empty)])
    assert list_projects(vault) == []
