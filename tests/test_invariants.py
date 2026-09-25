"""CLAUDE.md invariants enforced as build failures, not comments (CLAUDE.md §2, §11:
"if something in this file turns out to be wrong once you are in the code, tell me" — the
flip side is that what's *right* must be checked by the build, not trusted by convention).

Covers:
  I2 - the compliance guard is the single egress point.
  I4 - ground_truth_link is invisible to runtime code.
  I7 - no provider SDK imported outside continuum/llm/.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import ClassVar

REPO_ROOT = Path(__file__).parent.parent
CONTINUUM_ROOT = REPO_ROOT / "continuum"
EVAL_ROOT = REPO_ROOT / "eval"

PROVIDER_SDK_MODULES = ("openai", "anthropic", "ollama", "google.generativeai", "cohere")


def _iter_python_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.py"))


def _imported_module_names(path: Path) -> set[str]:
    """All module names this file imports, as dotted strings, e.g. "continuum.guard.dispatch"."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            # `from continuum.db.models import eval as eval_models` — record the target
            # attribute too, since the module-level name alone wouldn't catch this form.
            for alias in node.names:
                names.add(f"{node.module}.{alias.name}")
    return names


def _importers_of(root: Path, target_prefixes: tuple[str, ...]) -> dict[Path, set[str]]:
    hits: dict[Path, set[str]] = {}
    for path in _iter_python_files(root):
        imported = _imported_module_names(path)
        matched = {
            m for m in imported if any(m == p or m.startswith(p + ".") for p in target_prefixes)
        }
        if matched:
            hits[path] = matched
    return hits


class TestGroundTruthLinkIsEvalOnly:
    """CLAUDE.md I4: 'ground_truth_link is invisible to runtime code. Only the evaluation
    harness may read it. A runtime import of it is a build failure.'
    """

    # CLAUDE.md I4: "Only the evaluation harness may read it" — i.e. anything under eval/ —
    # plus the specific continuum/ files that legitimately touch the table's own module
    # without reading ground_truth_link rows at runtime: the model definition itself, the
    # models package's re-export, the writer (collisions/build), and Alembic's env.py
    # (which imports the whole models package to populate autogenerate metadata).
    ALLOWED_IMPORTER_FILES: ClassVar[set[Path]] = {
        CONTINUUM_ROOT / "db" / "models" / "eval.py",
        CONTINUUM_ROOT / "db" / "models" / "__init__.py",
        CONTINUUM_ROOT / "foundation" / "collisions.py",
        CONTINUUM_ROOT / "foundation" / "build.py",
        CONTINUUM_ROOT / "db" / "migrations" / "env.py",
    }

    def test_no_disallowed_runtime_import(self) -> None:
        hits = _importers_of(
            CONTINUUM_ROOT, ("continuum.db.models.eval", "continuum.db.models.eval_models")
        )
        disallowed = {
            path: mods for path, mods in hits.items() if path not in self.ALLOWED_IMPORTER_FILES
        }
        assert not disallowed, f"runtime import of ground_truth_link's module: {disallowed}"

    def test_eval_package_actually_uses_the_carve_out(self) -> None:
        # Confirms this test file's own logic isn't vacuous: eval/ really does import
        # ground_truth_link's module (eval.resolution_metrics, Phase 2) and is correctly
        # excluded from the disallowed-importers check above rather than just never tripping
        # it because nothing under eval/ happens to import it yet.
        hits = _importers_of(
            EVAL_ROOT, ("continuum.db.models.eval", "continuum.db.models.eval_models")
        )
        assert hits, "expected at least one eval/ module to import ground_truth_link's module"


class TestNoProviderSdkOutsideLlmPackage:
    """CLAUDE.md I7: 'All model calls go through the LiteLLM gateway. No provider SDK
    imported anywhere outside continuum/llm/.'
    """

    def test_no_disallowed_provider_sdk_import(self) -> None:
        hits = _importers_of(CONTINUUM_ROOT, PROVIDER_SDK_MODULES)
        llm_package = CONTINUUM_ROOT / "llm"
        disallowed = {path: mods for path, mods in hits.items() if llm_package not in path.parents}
        assert not disallowed, f"provider SDK imported outside continuum/llm/: {disallowed}"


class TestGuardIsSingleEgressPoint:
    """CLAUDE.md I2: 'The compliance guard is the single egress point. No code path may
    dispatch an outbound message without a guard verdict.'
    """

    def test_dispatch_module_only_imported_within_guard_package(self) -> None:
        hits = _importers_of(CONTINUUM_ROOT, ("continuum.guard.dispatch",))
        guard_package = CONTINUUM_ROOT / "guard"
        disallowed = {
            path: mods for path, mods in hits.items() if guard_package not in path.parents
        }
        assert not disallowed, f"dispatch imported outside continuum/guard/: {disallowed}"


class TestTrueUpliftIsEvalOnly:
    """CLAUDE.md I4 (same rule as ground_truth_link): the oracle causal truth written by
    continuum.foundation.true_uplift is readable only by eval/."""

    def test_no_runtime_module_mentions_the_truth_file(self) -> None:
        allowed = {
            CONTINUUM_ROOT / "foundation" / "true_uplift.py",
            CONTINUUM_ROOT / "foundation" / "causal.py",  # docstring documents the file
            CONTINUUM_ROOT / "foundation" / "build.py",  # passes the output path to the writer
        }
        offenders = [
            path
            for path in _iter_python_files(CONTINUUM_ROOT)
            if path not in allowed and "true_uplift" in path.read_text(encoding="utf-8")
        ]
        assert not offenders, f"runtime code touches the eval-only truth file: {offenders}"


class TestOnlyDispatchRecordsRuntimeContacts:
    """CLAUDE.md I2, second angle: a RUNTIME contact_event is the record that a message went
    out, so only the guard's dispatch may write one."""

    def test_runtime_contact_source_is_referenced_only_inside_the_guard_and_models(self) -> None:
        allowed_dirs = (CONTINUUM_ROOT / "guard", CONTINUUM_ROOT / "db")
        offenders = [
            path
            for path in _iter_python_files(CONTINUUM_ROOT)
            if "ContactSource.RUNTIME" in path.read_text(encoding="utf-8")
            and not any(d in path.parents for d in allowed_dirs)
        ]
        assert not offenders, f"runtime contacts written outside the guard: {offenders}"
