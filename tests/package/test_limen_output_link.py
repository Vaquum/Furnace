"""run_round bridges <round>/limen_output to Limen's results/limen_output.

Limen >= 3.14 writes its artefacts under <cwd>/results/<uel.output_path>, so a
round's output lands in <round>/results/limen_output/. Furnace's readers (the
round-status poll, the round page, inference, and run_round's own post-run
check) all read <round>/limen_output/. _link_limen_output reconciles the two
with one relative symlink, so those readers keep working unchanged.
"""

from __future__ import annotations

from pathlib import Path

from furnace.run_round import _link_limen_output


def test_links_limen_output_to_results_subdir(tmp_path: Path) -> None:
    _link_limen_output(tmp_path)
    link = tmp_path / "limen_output"
    assert link.is_symlink()
    # relative target, so the round directory stays relocatable
    assert link.readlink() == Path("results") / "limen_output"


def test_link_resolves_once_limen_writes(tmp_path: Path) -> None:
    _link_limen_output(tmp_path)
    # limen creates results/limen_output and grows results.csv there
    real = tmp_path / "results" / "limen_output"
    real.mkdir(parents=True)
    (real / "results.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    link = tmp_path / "limen_output"
    assert link.is_dir()  # resolves through the symlink
    assert (link / "results.csv").read_text(encoding="utf-8") == "a,b\n1,2\n"


def test_link_is_idempotent(tmp_path: Path) -> None:
    _link_limen_output(tmp_path)
    _link_limen_output(tmp_path)  # second call must not raise
    assert (tmp_path / "limen_output").is_symlink()


def test_link_does_not_clobber_an_existing_real_dir(tmp_path: Path) -> None:
    # An old-Limen round may already have a real limen_output/ directory;
    # the bridge must leave it alone rather than fail or replace it.
    real = tmp_path / "limen_output"
    real.mkdir()
    _link_limen_output(tmp_path)
    assert real.is_dir()
    assert not real.is_symlink()
