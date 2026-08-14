#!/usr/bin/env python3
"""Find tests that can pass without checking anything.

    python3 tools/audit-tests.py            # report
    python3 tools/audit-tests.py --strict   # exit 1 if any NO_ASSERT or LOOP_ONLY found

Not a lint pass. The point is coverage honesty: a suite of 640 tests tells you nothing
about what is verified until you know how many of them can pass vacuously. This project hit
the same vacuous shape three times in one session -- a test that iterates a derived
collection and passes silently when that collection is empty -- and two were caught only by
mutation testing. Static analysis finds the shape everywhere at once; mutation testing then
confirms the ones that matter.

What gets flagged:

  NO_ASSERT   the function contains no assertion of any kind. It passes if the code under
              test merely fails to raise.
  LOOP_ONLY   every assertion sits inside a `for`, and nothing established that the
              iterable is non-empty. Zero iterations means zero assertions and a green test.
  COND_ONLY   every assertion sits inside an `if`. If the branch is never taken -- and the
              condition often depends on state the test did not force -- nothing is checked.

A guard counts as: asserting the collection itself, asserting its len(), or comparing it to
a literal. That is the fix for LOOP_ONLY and it is one line:

    rows = derive()
    assert rows, "nothing to check -- the fixture stopped producing rows"
    for r in rows: ...

Deliberately NOT flagged: tests using pytest.raises (the raise IS the assertion),
pytest.fail, unittest self.assert*, and tests whose asserts live in a called helper. The
last is a real blind spot -- a helper that stops asserting silently defeats this -- so a
helper-only test is reported separately as DELEGATED rather than pretended to be clean.
"""
import ast
import sys
from pathlib import Path

ASSERTING_CALLS = {"fail", "raises", "warns", "approx", "xfail"}

# Tests where an empty collection is a LEGITIMATE outcome, reviewed individually, with the
# reason and the thing that covers the vacuity hole instead. Adding an entry is a deliberate
# act -- the point of this tool is that a green suite means something, and an exemption
# without a replacement check quietly gives that back.
#
# All three below are parametrised over hostile paths where the correct behaviour for many
# inputs is to forward NOTHING, so asserting the collection is non-empty is simply wrong --
# it broke 42 parametrised cases when applied mechanically. They are covered instead by
# test_the_hostile_path_harness_actually_forwards_something, which checks once, on paths
# that must forward, that the harness is exercising anything at all.
REVIEWED = {
    "test_no_request_path_can_redirect_the_internal_call":
        "empty `calls` is correct for non-gated paths; harness covered by "
        "test_the_hostile_path_harness_actually_forwards_something",
    "test_only_declared_endpoints_are_ever_forwarded":
        "empty `seen` is correct for non-gated paths; same harness check covers it",
    "test_no_path_can_redirect_the_internal_call_off_the_box":
        "empty `calls` is correct for non-gated paths; same harness check covers it",
}


def _is_assertion(node):
    """Assert statements, plus the call forms that stand in for one."""
    if isinstance(node, ast.Assert):
        return True
    if isinstance(node, (ast.Call, ast.With, ast.withitem)):
        pass
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name in ASSERTING_CALLS or name.startswith("assert"):
            return True
    return False


def _provably_nonempty(node):
    """True when the iterable cannot be empty by construction.

    Without this the tool flags `for h in (23, 0, 2, 6)` as vacuous, which is nonsense and
    is exactly how a scanner earns a reputation for crying wolf. Only literals and a few
    constructions over literals qualify -- anything derived from code under test does not,
    because that is the case worth flagging.
    """
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return bool(node.elts)
    if isinstance(node, ast.Dict):
        return bool(node.keys)
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, ast.Call):
        f = node.func
        name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
        if name == "range":
            args = [a.value for a in node.args if isinstance(a, ast.Constant)]
            if len(args) == len(node.args):
                return (args[0] > 0) if len(args) == 1 else (args[1] > args[0])
            return False
        # zip/product/chain over literals stay non-empty; .items()/.values() on a literal
        # dict likewise.
        if name in {"zip", "product", "chain"} and node.args:
            return all(_provably_nonempty(a) for a in node.args)
        if name in {"items", "values", "keys"} and isinstance(f, ast.Attribute):
            return _provably_nonempty(f.value)
    return False


def _guards_nonempty(stmts, target_src):
    """Did anything before the loop establish that the iterable has contents?"""
    for s in stmts:
        for n in ast.walk(s):
            if isinstance(n, ast.Assert) and target_src and target_src in ast.dump(n.test):
                return True
    return False


def audit_function(fn, source):
    asserts, in_loop, in_cond, calls = [], [], [], []
    for node in ast.walk(fn):
        if _is_assertion(node):
            asserts.append(node)
        elif isinstance(node, ast.Call):
            f = node.func
            calls.append(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))

    if not asserts:
        # A test whose checking happens inside a helper is not vacuous, but this tool
        # cannot see through the call. Say so instead of guessing either way.
        helpers = [c for c in calls if c.startswith(("assert_", "check_", "expect_"))]
        return ("DELEGATED", f"no direct assert; calls {helpers[0]}()") if helpers \
            else ("NO_ASSERT", "no assertion of any kind")

    # Where do the assertions live?
    loop_bound = cond_bound = free = 0
    for a in asserts:
        depth_loop = depth_cond = False
        for parent in ast.walk(fn):
            if isinstance(parent, (ast.For, ast.While, ast.AsyncFor)):
                if any(a is d for d in ast.walk(parent)):
                    depth_loop = True
            elif isinstance(parent, ast.If):
                if any(a is d for d in ast.walk(parent)):
                    depth_cond = True
        if depth_loop:
            loop_bound += 1
        elif depth_cond:
            cond_bound += 1
        else:
            free += 1

    if free:
        return None                       # at least one assertion always runs

    if loop_bound:
        # Every loop that CONTAINS an assertion must be safe. The first version `continue`d
        # past the safe ones and then fell through to the finding anyway, so teaching it
        # about literals changed nothing and the count stayed at 25 -- a fix that reported
        # success while doing nothing.
        unguarded = []
        for node in ast.walk(fn):
            if not isinstance(node, (ast.For, ast.AsyncFor)):
                continue
            if not any(_is_assertion(d) for d in ast.walk(node)):
                continue                      # loop holds no assertion; irrelevant here
            if _provably_nonempty(node.iter):
                continue                      # a literal cannot be empty
            before = [s for s in fn.body if s.lineno < node.lineno]
            key = getattr(node.iter, "id", None) or (
                node.iter.func.attr if isinstance(node.iter, ast.Call)
                and isinstance(node.iter.func, ast.Attribute) else None)
            if _guards_nonempty(before, ast.dump(node.iter)) or (
                    key and _guards_nonempty(before, repr(key))):
                continue
            unguarded.append(node)
        if not unguarded:
            return None
        return ("LOOP_ONLY", f"assert only runs inside a loop over "
                             f"`{ast.unparse(unguarded[0].iter)[:48]}` with no non-empty guard")

    if cond_bound:
        return ("COND_ONLY", "every assert is inside a conditional")
    return None


def main():
    strict = "--strict" in sys.argv
    root = Path(__file__).resolve().parent.parent
    findings, total = [], 0

    for path in sorted((root / "tests").rglob("test_*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        src = path.read_text().splitlines()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) or not fn.name.startswith("test_"):
                continue
            total += 1
            r = audit_function(fn, src)
            if r and fn.name in REVIEWED:
                r = ("REVIEWED", REVIEWED[fn.name])
            if r:
                findings.append((path.relative_to(root), fn.lineno, fn.name, *r))

    by_kind = {}
    for f in findings:
        by_kind.setdefault(f[3], []).append(f)

    print(f"{total} test functions examined\n")
    for kind in ("NO_ASSERT", "LOOP_ONLY", "COND_ONLY", "DELEGATED", "REVIEWED"):
        rows = by_kind.get(kind, [])
        print(f"── {kind}: {len(rows)}")
        for rel, line, name, _, why in rows:
            print(f"     {rel}:{line}  {name}")
            print(f"       {why}")
        if rows:
            print()

    hard = len(by_kind.get("NO_ASSERT", [])) + len(by_kind.get("LOOP_ONLY", []))
    # A reviewed entry naming a test that no longer exists is a stale exemption, and a
    # stale exemption list grows until it permits everything.
    names = {f[2] for f in findings}
    stale = [k for k in REVIEWED if k not in names]
    if stale:
        print(f"!! stale REVIEWED entries (no longer flagged, remove them): {stale}\n")
    print(f"summary: {hard} test(s) can pass without checking anything; "
          f"{len(by_kind.get('COND_ONLY', []))} may not check on every run; "
          f"{len(by_kind.get('DELEGATED', []))} assert via a helper (unverifiable here).")
    if strict and hard:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
