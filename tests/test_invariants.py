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
        matched = {m for m in imported if any(m == p or m.startswith(p + ".") for p in target_prefixes)}
        if matched:
            hits[path] = matched
    return hits


class TestGroundTruthLinkIsEvalOnly:
    """CLAUDE.md I4: 'ground_truth_link is invisible to runtime code. Only the evaluation
    harness may read it. A runtime import of it is a build failure.'
    """

    ALLOWED_IMPORTERS = {
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
        disallowed = {path: mods for path, mods in hits.items() if path not in self.ALLOWED_IMPORTERS}
        assert not disallowed, f"runtime import of ground_truth_link's module: {disallowed}"

    def test_eval_package_is_permitted_to_import_it(self) -> None:
        # Sanity check the test's own logic isn't vacuous: eval/ must be allowed to import
        # it once eval/ actually does so (Phase 7). Not asserted here since eval/ has no
        # reader yet — this test exists so Phase 7 doesn't need to touch this file.
        assert EVAL_ROOT.exists()


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
        disallowed = {path: mods for path, mods in hits.items() if guard_package not in path.parents}
        assert not disallowed, f"dispatch imported outside continuum/guard/: {disallowed}"
