#!/usr/bin/env python3
"""Check that config keys in the code and in config.example.json agree.

Walks core/ and channels/ (non-test python, skipping kitconfig.py itself) for calls to
kitconfig.get / .require / .path / .get_list / .set_value. Every key must be a string
literal (a non-literal key is a defect), and must exist in config.example.json. Every key in
the example file must be used, or be listed under "_external_keys" in that file.

Keys starting with "_" in the example file are notes, not keys. A nested object gives dotted
keys ("slack.token"). Using a parent key ("slack") counts as using all its children.

Exit codes: 0 ok, 1 defect, 2 could not check.
"""
import argparse
import ast
import os
import sys

import kit_common as kc

ACCESSORS = {"get", "require", "path", "get_list", "set_value"}
MODULE = "kitconfig"


def flatten(data, prefix=""):
    """Leaf paths of the example file, plus every parent path."""
    paths, leaves = set(), set()
    for key, value in data.items():
        if not prefix and key.startswith("_"):
            continue
        path = f"{prefix}{key}"
        paths.add(path)
        if isinstance(value, dict) and value:
            sub_paths, sub_leaves = flatten(value, path + ".")
            paths |= sub_paths
            leaves |= sub_leaves
        else:
            leaves.add(path)
    return paths, leaves


def receivers_and_names(tree):
    """Names that mean the kitconfig module, and bare names imported from it."""
    modules, funcs = {MODULE}, {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[-1] == MODULE:
                    modules.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == MODULE:
                    modules.add(alias.asname or alias.name)
                elif (node.module or "").split(".")[-1] == MODULE and alias.name in ACCESSORS:
                    funcs[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.Assign) and is_module_loader(node.value):
            # kc = _comms_module("kitconfig")
            modules.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return modules, funcs


def is_module_loader(value):
    """A call that gives back the kitconfig module: _load("kitconfig") or _kitconfig()."""
    if not isinstance(value, ast.Call):
        return False
    name = value.func.id if isinstance(value.func, ast.Name) else None
    return MODULE in (name or "") or any(
        isinstance(a, ast.Constant) and a.value == MODULE for a in value.args)


def direct_calls(tree, modules, funcs):
    """Yield (call node, accessor name) for every call straight into kitconfig."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in ACCESSORS:
            base = func.value
            base_name = base.id if isinstance(base, ast.Name) else (
                base.attr if isinstance(base, ast.Attribute) else None)
            if base_name in modules or is_module_loader(base):
                yield node, func.attr
        elif isinstance(func, ast.Name) and func.id in funcs:
            yield node, funcs[func.id]


def calls_in(tree):
    """Yield (call node, accessor name) for every kitconfig accessor call. A function that only
    passes its own first argument on, such as _conf(key), is an accessor too: its callers hold
    the literal keys."""
    modules, funcs = receivers_and_names(tree)
    wrappers, inner = {}, set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.args.args:
            first = fn.args.args[0].arg
            for call, accessor in direct_calls(fn, modules, funcs):
                arg = key_arg(call)
                if isinstance(arg, ast.Name) and arg.id == first:
                    wrappers[fn.name] = accessor
                    inner.add(id(call))
    for call, accessor in direct_calls(tree, modules, funcs):
        if id(call) not in inner:
            yield call, accessor
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in wrappers:
            yield node, wrappers[node.func.id]


def key_arg(call):
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg in ("key", "name"):
            return kw.value
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo")
    args = ap.parse_args()
    repo = kc.repo_arg(args.repo)
    example = kc.load_json(os.path.join(repo, "config.example.json"), "config.example.json")
    if not isinstance(example, dict):
        kc.die("ERROR: config.example.json is not an object")
    external = example.get("_external_keys", [])
    if not isinstance(external, list):
        kc.die("ERROR: _external_keys must be a list")
    known, leaves = flatten(example)

    files = [f for f in kc.python_sources(repo) if os.path.basename(f) != "kitconfig.py"]
    if not files:
        kc.die("ERROR: no python files under core/ or channels/ to scan")
    problems, used = [], set()
    for rel in files:
        with open(os.path.join(repo, rel), "rb") as fh:
            source = fh.read().decode("utf-8", "replace")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            kc.die(f"ERROR: {rel} does not parse")
        for call, accessor in sorted(calls_in(tree), key=lambda c: c[0].lineno):
            arg = key_arg(call)
            where = f"{rel}:{call.lineno}"
            if not (isinstance(arg, ast.Constant) and isinstance(arg.value, str)):
                problems.append(f"{where}: kitconfig.{accessor} key is not a string literal")
            elif arg.value not in known:
                problems.append(f"{where}: key '{arg.value}' is not in config.example.json")
            else:
                used.add(arg.value)
    for leaf in sorted(leaves):
        prefixes = {leaf} | {leaf.rsplit(".", i)[0] for i in range(1, leaf.count(".") + 1)}
        if not (prefixes & used) and not (prefixes & set(external)):
            problems.append(f"config.example.json: key '{leaf}' is never used")
    for line in problems[:300]:
        print(line)
    if problems:
        print(f"config keys FAILED: {len(problems)} problems")
        return kc.EXIT_DEFECT
    print(f"config keys ok: {len(leaves)} keys")
    return kc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
