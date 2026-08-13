"""Resolving `\\includegraphics` the way LaTeX resolves it.

qutub-ayush declares `\\graphicspath{{../outputs/}{figures/}}` and then writes
its includes the way the graphics package intends -- no directory and no
suffix, `\\includegraphics[width=...]{f-ayush-1}`. Nothing in Manuscriptor
implemented either half, so pandoc emitted `<img src="f-ayush-1">` verbatim, the
copier looked for a file of exactly that name beside `main.tex`, found none, and
skipped it in silence. Every figure in the paper was a broken-image icon.

Three of the manuscript's cases are only visible on a real tree and each has a
test here: a figure that exists only as `.jpg`, a figure that exists as both
`.pdf` and `.png` in one directory, and a `figures/` full of BROKEN symlinks
into `../outputs/` -- which is `manuscript/outputs/` from inside `figures/` and
does not exist. A broken symlink fails `is_file`, so it has to be passed over
for the next candidate rather than resolved to and staged as nothing.

And the destination guard is the reason the staging is indirect. `../outputs/`
resolves OUTSIDE the manuscript directory, while `_copy_assets` refuses any
destination escaping the output root -- a real guard, since an `<img src>` is
attacker-reachable in a way an author's `\\graphicspath` is not. So an
out-of-tree figure is staged under an in-tree MAPPED name that contains no `..`
by construction, and the guard keeps refusing everything it refused before.
"""
from __future__ import annotations

from pathlib import Path

from manuscriptor.render import graphics
from manuscriptor.render.pandoc import normalize_for_pandoc
from manuscriptor.render.postprocess import manuscript_source, stage_assets


def write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def doc(body: str, *, graphicspath: str = "") -> str:
    head = "\\documentclass{article}\n"
    if graphicspath:
        head += "\\graphicspath{" + graphicspath + "}\n"
    return head + "\\begin{document}\n" + body + "\n\\end{document}\n"


# ------------------------------------------------------------- graphicspath


def test_graphicspath_entries_are_read_in_order():
    assert graphics.graphics_dirs(
        "\\graphicspath{{../outputs/}{figures/}}"
    ) == ["../outputs/", "figures/"]


def test_no_graphicspath_is_no_entries():
    assert graphics.graphics_dirs("\\documentclass{article}") == []


def test_a_commented_graphicspath_is_not_the_graphicspath():
    assert graphics.graphics_dirs(
        "% \\graphicspath{{old/}}\n\\graphicspath{{figures/}}\n"
    ) == ["figures/"]


# ----------------------------------------------------------- extension search


def test_an_extensionless_include_finds_the_png(tmp_path):
    write(tmp_path / "figures" / "f-one.png")
    out = normalize_for_pandoc(
        doc("\\includegraphics[width=0.5\\textwidth]{f-one}", graphicspath="{figures/}"),
        cwd=tmp_path,
    )
    assert "{figures/f-one.png}" in out


def test_an_extensionless_include_finds_a_jpg(tmp_path):
    # qutub-ayush's `f-med-unlab` exists only as a .jpg.
    write(tmp_path / "figures" / "f-med-unlab.jpg")
    out = normalize_for_pandoc(
        doc("\\includegraphics{f-med-unlab}", graphicspath="{figures/}"), cwd=tmp_path)
    assert "{figures/f-med-unlab.jpg}" in out


def test_the_manuscript_directory_is_searched_too(tmp_path):
    write(tmp_path / "loose.png")
    out = normalize_for_pandoc(
        doc("\\includegraphics{loose}", graphicspath="{figures/}"), cwd=tmp_path)
    assert "{loose.png}" in out


def test_a_raster_is_preferred_to_a_pdf_of_the_same_name(tmp_path):
    # The pipeline rasterizes a PDF figure anyway, so the author's own raster is
    # both cheaper and closer to what they exported.
    write(tmp_path / "figures" / "f-combinations.pdf")
    write(tmp_path / "figures" / "f-combinations.png")
    out = normalize_for_pandoc(
        doc("\\includegraphics{f-combinations}", graphicspath="{figures/}"), cwd=tmp_path)
    assert "{figures/f-combinations.png}" in out


def test_a_pdf_only_figure_still_resolves(tmp_path):
    write(tmp_path / "figures" / "f-consort.pdf")
    out = normalize_for_pandoc(
        doc("\\includegraphics{f-consort}", graphicspath="{figures/}"), cwd=tmp_path)
    assert "{figures/f-consort.pdf}" in out


def test_an_include_that_already_names_its_file_is_left_alone(tmp_path):
    write(tmp_path / "figures" / "f.png")
    out = normalize_for_pandoc(doc("\\includegraphics{figures/f.png}"), cwd=tmp_path)
    assert "{figures/f.png}" in out


def test_an_unresolvable_include_is_unchanged(tmp_path):
    out = normalize_for_pandoc(
        doc("\\includegraphics{nowhere}", graphicspath="{figures/}"), cwd=tmp_path)
    assert "{nowhere}" in out


def test_nothing_is_resolved_without_a_manuscript_directory(tmp_path):
    # The Word/compile path calls `normalize_for_pandoc` on source that LaTeX
    # itself will read, and LaTeX does its own searching.
    src = doc("\\includegraphics{f-one}", graphicspath="{figures/}")
    write(tmp_path / "figures" / "f-one.png")
    assert "{f-one}" in normalize_for_pandoc(src)


def test_a_commented_include_is_not_resolved(tmp_path):
    write(tmp_path / "figures" / "f-one.png")
    out = normalize_for_pandoc(
        doc("% \\includegraphics{f-one}\n\\includegraphics{f-one}",
            graphicspath="{figures/}"),
        cwd=tmp_path,
    )
    assert out.count("{figures/f-one.png}") == 1
    assert "% \\includegraphics{f-one}" in out


# --------------------------------------------------------------- broken links


def test_a_broken_symlink_is_passed_over_for_the_next_candidate(tmp_path):
    # Exactly qutub-ayush's `figures/`: the symlinks point at `../outputs/`,
    # which from inside `figures/` is `manuscript/outputs/` and does not exist.
    outputs = write(tmp_path / "outputs" / "f-med-types.pdf").parent
    manuscript = tmp_path / "manuscript"
    (manuscript / "figures").mkdir(parents=True)
    (manuscript / "figures" / "f-med-types.pdf").symlink_to("../outputs/f-med-types.pdf")
    assert not (manuscript / "figures" / "f-med-types.pdf").is_file()
    out = normalize_for_pandoc(
        doc("\\includegraphics{f-med-types}", graphicspath="{figures/}{../outputs/}"),
        cwd=manuscript,
    )
    resolved = graphics.source_path(
        out.split("\\includegraphics{")[1].split("}")[0], manuscript)
    assert resolved == (outputs / "f-med-types.pdf").resolve()


# ------------------------------------------------------- out-of-tree staging


def test_an_out_of_tree_figure_is_named_in_tree(tmp_path):
    outputs = write(tmp_path / "outputs" / "f-kl.png").parent
    manuscript = tmp_path / "manuscript"
    manuscript.mkdir()
    out = normalize_for_pandoc(
        doc("\\includegraphics{f-kl}", graphicspath="{../outputs/}"), cwd=manuscript)
    rel = out.split("\\includegraphics{")[1].split("}")[0]
    assert rel.startswith(graphics.EXTERNAL_PREFIX + "/")
    assert ".." not in Path(rel).parts, "a staged name may never climb"
    assert graphics.source_path(rel, manuscript) == (outputs / "f-kl.png").resolve()


def test_an_out_of_tree_figure_is_staged_under_the_output_root(tmp_path):
    write(tmp_path / "outputs" / "f-kl.png", b"raster")
    manuscript = tmp_path / "manuscript"
    manuscript.mkdir()
    out_dir = tmp_path / "out"
    rel = graphics.staged_rel((tmp_path / "outputs" / "f-kl.png"), manuscript)
    html, staged = stage_assets(f'<img src="{rel}" />', manuscript, out_dir)
    assert staged == [rel]
    assert (out_dir / rel).read_bytes() == b"raster"
    assert out_dir.resolve() in (out_dir / rel).resolve().parents


def test_the_escape_guard_still_refuses_a_hostile_path(tmp_path):
    # Unchanged, and it must stay that way: an `<img src>` is reachable by
    # anything that can put a string in the HTML, where a `\graphicspath` is the
    # author's own instruction about their own tree.
    write(tmp_path / "secret.txt", b"s")
    manuscript = tmp_path / "manuscript"
    manuscript.mkdir()
    out_dir = tmp_path / "out" / "sub"
    _html, staged = stage_assets('<img src="../secret.txt" />', manuscript, out_dir)
    assert staged == []
    assert not (tmp_path / "out" / "secret.txt").exists()
    assert manuscript_source("../secret.txt", manuscript) is None


def test_a_staged_external_asset_is_refreshed_only_when_the_build_staged_it(tmp_path):
    from manuscriptor.render.postprocess import refresh_asset

    source = write(tmp_path / "outputs" / "f-kl.png", b"one")
    manuscript = tmp_path / "manuscript"
    manuscript.mkdir()
    cache = tmp_path / "cache"
    rel = graphics.staged_rel(source, manuscript)
    # Nothing staged: the route may not read outside the manuscript on a name a
    # browser made up.
    assert refresh_asset(rel, manuscript, cache) is False
    assert not (cache / rel).exists()
    # Staged by a build, and now stale: it refreshes.
    stage_assets(f'<img src="{rel}" />', manuscript, cache)
    source.write_bytes(b"two")
    assert refresh_asset(rel, manuscript, cache) is True
    assert (cache / rel).read_bytes() == b"two"
