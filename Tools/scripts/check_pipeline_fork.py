#!/usr/bin/env python3
"""Local verification harness for the experimental CPython pipeline fork.

This intentionally wraps CPython's existing regrtest and regeneration targets
rather than introducing a second test framework.  Run it with the interpreter
built from this checkout, normally::

    ./python Tools/scripts/check_pipeline_fork.py
"""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shlex
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]

FOCUSED_TESTS = (
    "test_pipeline",
    "test_ast",
    "test_annotationlib",
    "test_symtable",
    "test_compile",
    "test_dis",
    "test_code",
    "test_frame",
    "test_inspect",
    "test_token",
    "test_tokenize",
    "test_syntax",
    "test_grammar",
    "test_fstring",
    "test_tstring",
    "test_peg_generator",
)

PIPELINE_OPCODE_DECL = re.compile(
    r"\b(?:inst|op|macro|family)\s*\(\s*[A-Z0-9_]*(?:PIPE|TOPIC)[A-Z0-9_]*"
)


def display_command(cmd: list[str]) -> str:
    try:
        return shlex.join(cmd)
    except AttributeError:  # pragma: no cover - only for unusually old hosts
        return " ".join(repr(part) for part in cmd)


def run(cmd: list[str], *, cwd: Path = ROOT) -> None:
    print(f"+ {display_command(cmd)}", flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def git_output(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    return proc.stdout


def require_source_tree() -> None:
    expected = (
        ROOT / "Grammar" / "python.gram",
        ROOT / "Grammar" / "Tokens",
        ROOT / "Parser" / "Python.asdl",
    )
    missing = [str(path) for path in expected if not path.is_file()]
    if missing:
        raise SystemExit(
            "pipeline verifier could not locate the CPython source tree; "
            f"missing: {', '.join(missing)}"
        )


def require_clean_tracked_tree() -> None:
    dirty = git_output("status", "--porcelain", "--untracked-files=no")
    if dirty:
        print("refusing regeneration check: tracked worktree is dirty", file=sys.stderr)
        print(
            "commit/stash your changes or use the default test-only mode",
            file=sys.stderr,
        )
        print(dirty, file=sys.stderr, end="")
        raise SystemExit(2)


def focused_tests() -> None:
    print("== focused CPython tests ==", flush=True)
    run([sys.executable, "-m", "test", *FOCUSED_TESTS])


def structural_checks() -> None:
    print("== pipeline structural checks ==", flush=True)

    import ast
    import token

    assert token.EXACT_TOKEN_TYPES["|>"] == token.VBARGREATER
    assert token.EXACT_TOKEN_TYPES["$"] == token.DOLLAR
    assert ast.Pipeline._fields == ("value", "body")
    assert ast.PipeTopic._fields == ()

    for relative in ("Python/bytecodes.c", "Python/optimizer_bytecodes.c"):
        path = ROOT / relative
        text = path.read_text(encoding="utf-8")
        match = PIPELINE_OPCODE_DECL.search(text)
        if match is not None:
            raise AssertionError(
                f"pipeline-specific opcode declaration found in {relative}: "
                f"{match.group(0)!r}"
            )


def ensure_make_regen_supported() -> None:
    if sys.platform == "win32":
        raise SystemExit(
            "regeneration modes currently require CPython's Unix Make build; "
            "run the default focused-test mode on Windows"
        )
    if not (ROOT / "Makefile").exists():
        raise SystemExit(
            "regeneration modes require an in-tree configured Unix build "
            "(no top-level Makefile found)"
        )


def require_no_regen_diff() -> None:
    run(["git", "diff", "--exit-code", "--"], cwd=ROOT)


def regen_owned() -> None:
    ensure_make_regen_supported()
    require_clean_tracked_tree()
    for pass_number in (1, 2):
        print(f"== owned regeneration pass {pass_number} ==", flush=True)
        run(["make", "regen-token", "regen-ast", "regen-pegen"])
        require_no_regen_diff()


def regen_all() -> None:
    ensure_make_regen_supported()
    require_clean_tracked_tree()
    for pass_number in (1, 2):
        print(f"== full regeneration pass {pass_number} ==", flush=True)
        run(["make", "regen-all"])
        require_no_regen_diff()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--regen-check",
        action="store_true",
        help="run focused checks and verify token/AST/parser regeneration twice",
    )
    modes.add_argument(
        "--full",
        action="store_true",
        help="run focused checks and verify full CPython regeneration twice",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    require_source_tree()
    focused_tests()
    structural_checks()
    if args.regen_check:
        regen_owned()
    elif args.full:
        regen_all()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
