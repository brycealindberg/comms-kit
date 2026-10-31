#!/usr/bin/env python3
"""Prove that the kit's code never keeps data next to itself.

Static: every use of __file__ in core/ and channels/ (non-test python) must lead to a code
file, or to a folder used only for imports. Anything else is listed. A line may carry
`# kit: code path` to say it is reviewed.

Behavior: with a temp HOME and config data_dir pointing inside it, hash the code tree, run
the commands in tools/data_path_commands.json, hash again. The tree must be unchanged and
every file written must sit under the data dir (temp files under HOME/tmp are allowed).

Exit codes: 0 ok, 1 defect, 2 could not check (core/bin/comms absent, nothing scanned).
"""
import argparse
import ast
import json
import os
import re
import shutil
import sys

import kit_common as kc

DEFAULT_COMMANDS = os.path.join(os.path.dirname(os.path.realpath(__file__)),
                                "data_path_commands.json")
CODE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".swift"}
MANIFEST_NAMES = {"channel.json", "package.json", "package-lock.json", "config.example.json",
                  "data_path_commands.json"}
ALLOW_COMMENT = re.compile(r"#\s*kit:\s*code path")
IMPORT_CALLS = {"sys.path.insert", "sys.path.append", "sys.path.extend", "site.addsitedir"}
SAME_PATH_FUNCS = {"abspath", "realpath", "normpath", "fspath", "str", "Path", "PurePath",
                   "PosixPath", "resolve", "absolute", "expanduser"}
NAME_ATTRS = {"name", "stem", "suffix", "suffixes", "parts"}
NAME_FUNCS = {"basename", "splitext", "split"}
NOTPATH = object()


class PV:
    """A path derived from __file__. `path` is the concrete path, or None when unknown."""

    def __init__(self, path):
        self.path = path


def is_code(path):
    base = os.path.basename(path)
    suffix = os.path.splitext(base)[1].lower()
    if suffix in CODE_SUFFIXES:
        return True
    if suffix == ".json" and (base in MANIFEST_NAMES or base.endswith(("manifest.json",
                                                                        ".schema.json"))):
        return True
    if os.path.isfile(path):
        try:
            with open(path, "rb") as fh:
                return fh.read(2) == b"#!"
        except OSError:
            return False
    return False


class Scanner:
    def __init__(self, module_path, source):
        self.module_path = module_path
        self.lines = source.split("\n")
        self.tree = ast.parse(source)
        self.env = {}
        self.uses = []  # (line, message)

    # -- evaluation of path expressions
    def key(self, node):
        if isinstance(node, (ast.Name, ast.Attribute)):
            try:
                return ast.unparse(node)
            except Exception:
                return None
        return None

    def join(self, base, parts):
        if base.path is None:
            return PV(None)
        path = base.path
        for part in parts:
            if not isinstance(part, str):
                return PV(None)
            path = os.path.join(path, part)
        return PV(os.path.normpath(path))

    def ev(self, node):
        if isinstance(node, ast.Constant):
            return node.value if isinstance(node.value, (str, int)) else None
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return PV(self.module_path)
            return self.env.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self.ev(node.value)
            if isinstance(base, PV):
                if node.attr == "parent":
                    return PV(os.path.dirname(base.path) if base.path else None)
                if node.attr in NAME_ATTRS:
                    return NOTPATH
                return None
            return self.env.get(self.key(node))
        if isinstance(node, ast.Subscript):
            inner = node.value
            if (isinstance(inner, ast.Attribute) and inner.attr == "parents"
                    and isinstance(self.ev(inner.value), PV)
                    and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, int)):
                base, path = self.ev(inner.value), None
                if base.path is not None:
                    path = base.path
                    for _ in range(node.slice.value + 1):
                        path = os.path.dirname(path)
                return PV(path)
            return None
        if isinstance(node, ast.BinOp):
            left, right = self.ev(node.left), self.ev(node.right)
            if isinstance(node.op, ast.Div) and isinstance(left, PV) and isinstance(right, str):
                return self.join(left, [right])
            if isinstance(node.op, ast.Add):
                if isinstance(left, PV) and isinstance(right, str):
                    return PV(os.path.normpath(left.path + right) if left.path else None)
                if isinstance(left, str) and isinstance(right, PV):
                    return PV(os.path.normpath(left + right.path) if right.path else None)
            if isinstance(left, PV) or isinstance(right, PV):
                return PV(None)
            return None
        if isinstance(node, ast.JoinedStr):
            pieces, tainted = [], False
            for part in node.values:
                if isinstance(part, ast.Constant):
                    pieces.append(str(part.value))
                elif isinstance(part, ast.FormattedValue):
                    value = self.ev(part.value)
                    if isinstance(value, PV):
                        tainted = True
                        pieces.append(value.path)
                    elif isinstance(value, str):
                        pieces.append(value)
                    else:
                        pieces.append(None)
            if not tainted:
                return None
            if any(p is None for p in pieces):
                return PV(None)
            return PV(os.path.normpath("".join(pieces)))
        if isinstance(node, ast.IfExp):
            branches = [self.ev(node.body), self.ev(node.orelse)]
            return PV(None) if any(isinstance(b, PV) for b in branches) else None
        if isinstance(node, ast.Call):
            return self.ev_call(node)
        return None

    def ev_call(self, node):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None)
        if name is None:
            return None
        args = [self.ev(a) for a in node.args]
        receiver = self.ev(func.value) if isinstance(func, ast.Attribute) else None
        if name in NAME_FUNCS and any(isinstance(a, PV) for a in args + [receiver]):
            return NOTPATH
        if name == "dirname" and args and isinstance(args[0], PV):
            return PV(os.path.dirname(args[0].path) if args[0].path else None)
        if name in SAME_PATH_FUNCS:
            if isinstance(receiver, PV):
                return receiver
            if args and isinstance(args[0], PV):
                return args[0] if len(args) == 1 or name != "Path" else self.join(args[0], args[1:])
            return None
        if name == "join" and args and isinstance(args[0], PV):
            return self.join(args[0], args[1:])
        if name == "joinpath" and isinstance(receiver, PV):
            return self.join(receiver, args)
        if name == "with_name" and isinstance(receiver, PV) and args and isinstance(args[0], str):
            return PV(os.path.join(os.path.dirname(receiver.path), args[0])
                      if receiver.path else None)
        if name == "with_suffix" and isinstance(receiver, PV) and args \
                and isinstance(args[0], str):
            return PV(os.path.splitext(receiver.path)[0] + args[0] if receiver.path else None)
        return None

    # -- assignments (derivations are not uses)
    def build_env(self):
        assigns = [n for n in ast.walk(self.tree)
                   if isinstance(n, (ast.Assign, ast.AnnAssign)) and getattr(n, "value", None)]
        assigns.sort(key=lambda n: (n.lineno, n.col_offset))
        for _ in range(3):
            for node in assigns:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                value = self.ev(node.value)
                for target in targets:
                    key = self.key(target)
                    if key is None:
                        continue
                    if isinstance(value, (PV, str)):
                        self.env[key] = value
                    else:
                        self.env.pop(key, None)

    def allowed(self, node):
        start = getattr(node, "lineno", 0)
        end = getattr(node, "end_lineno", start)
        return any(ALLOW_COMMENT.search(self.lines[i - 1])
                   for i in range(start, end + 1) if 0 < i <= len(self.lines))

    def report(self, node, value):
        if self.allowed(node):
            return
        if value.path is None:
            message = "__file__ leads to a path that cannot be shown to be a code file"
        elif is_code(value.path):
            return
        elif os.path.isdir(value.path):
            message = "__file__ leads to a folder used for more than imports"
        else:
            message = "__file__ leads to a non-code path"
        self.uses.append((node.lineno, message))

    def visit(self, node):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and getattr(node, "value", None):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any((self.key(t) or "").startswith("sys.path") for t in targets) or any(
                    isinstance(t, ast.Subscript) and ast.unparse(t.value) == "sys.path"
                    for t in targets):
                return
            if isinstance(self.ev(node.value), (PV, str)):
                return
        if isinstance(node, ast.Call):
            try:
                name = ast.unparse(node.func)
            except Exception:
                name = ""
            if name in IMPORT_CALLS:
                for arg in node.args:
                    if not isinstance(self.ev(arg), PV):
                        self.visit(arg)
                return
        value = self.ev(node)
        if isinstance(value, PV):
            self.report(node, value)
            return
        if value is NOTPATH:
            return
        for child in ast.iter_child_nodes(node):
            self.visit(child)

    def scan(self):
        self.build_env()
        self.visit(self.tree)
        return sorted(set(self.uses))


def static_part(repo):
    files = kc.python_sources(repo)
    if not files:
        kc.die("ERROR: no python files under core/ or channels/ to scan")
    defects = []
    for rel in files:
        path = os.path.join(repo, rel)
        with open(path, "rb") as fh:
            source = fh.read().decode("utf-8", "replace")
        try:
            scanner = Scanner(os.path.realpath(path), source)
        except SyntaxError:
            kc.die(f"ERROR: {rel} does not parse")
        defects.extend(f"{rel}:{line}: {message}" for line, message in scanner.scan())
    return len(files), defects


def command_argv(repo, argv):
    first = os.path.join(repo, argv[0])
    if os.path.isfile(first):
        if first.endswith(".py") or kc.has_python_shebang(first):
            return [sys.executable, first, *argv[1:]]
        return [first, *argv[1:]]
    return list(argv)


def behavior_part(repo, commands_path, real_home):
    spec = kc.load_json(commands_path, "the command list")
    commands = spec.get("commands") if isinstance(spec, dict) else None
    if not commands or not all(isinstance(c, dict) and isinstance(c.get("argv"), list)
                               and c["argv"] for c in commands):
        kc.die("ERROR: the command list is empty or malformed")

    work = kc.make_workdir("comms-paths-")
    try:
        home = os.path.join(work, "home")
        data_dir = os.path.join(home, "data")
        tmpdir = os.path.join(home, "tmp")
        for d in (home, tmpdir, os.path.join(work, "conf")):
            os.makedirs(d)
        log = os.path.join(work, "stub.log")
        stubs = kc.make_stubs(os.path.join(work, "stubs"), log)
        example = os.path.join(repo, "config.example.json")
        if os.path.isfile(example):
            config = kc.temp_config(os.path.join(work, "conf"), example, data_dir=data_dir)
        else:
            config = os.path.join(work, "conf", "config.json")
            with open(config, "w", encoding="utf-8") as fh:
                json.dump({"data_dir": data_dir}, fh)
        env = kc.clean_env(home, stubs, config, tmpdir=tmpdir)

        watched = kc.real_home_dirs(real_home)
        before_tree, before_real = kc.tree_hash(repo), kc.snapshot(watched)
        defects = []
        for cmd in commands:
            name = str(cmd.get("name") or " ".join(cmd["argv"][1:]) or cmd["argv"][0])
            expect = cmd.get("expect_exit", 0)
            code, _ = kc.run(command_argv(repo, cmd["argv"]), env, repo,
                             int(cmd.get("timeout", 120)))
            if code is None:
                defects.append(f"command '{name}': timed out or could not start")
            elif code != expect:
                defects.append(f"command '{name}': exit {code}, expected {expect}")
        after_tree = kc.tree_hash(repo)
        for rel in sorted(set(before_tree) | set(after_tree)):
            if before_tree.get(rel) != after_tree.get(rel):
                what = ("added" if rel not in before_tree else
                        "removed" if rel not in after_tree else "changed")
                defects.append(f"{rel.replace(os.sep, '/')}: code tree {what}")
        written = 0
        for base, _, names in os.walk(home):
            for fname in names:
                full = os.path.join(base, fname)
                if full.startswith(data_dir + os.sep):
                    written += 1
                elif not full.startswith(tmpdir + os.sep):
                    rel = os.path.relpath(full, home).replace(os.sep, "/")
                    defects.append(f"{rel}: written under HOME but outside the data dir")
        if not written:
            defects.append("no command wrote a file under the data dir, so this run proved nothing")
        for bad in kc.forbidden(kc.stub_calls(log)):
            defects.append(f"a command reached a real tool: {bad}")
        for root in kc.snapshot_diff(before_real, kc.snapshot(watched)):
            defects.append(f"{os.path.relpath(root, real_home)}: real state changed")
        return written, defects
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--repo")
    ap.add_argument("--static", action="store_true", help="only the __file__ scan")
    ap.add_argument("--behavior", action="store_true", help="only the run-and-hash part")
    ap.add_argument("--commands", default=DEFAULT_COMMANDS)
    ap.add_argument("--real-home", default=kc.real_home_default())
    args = ap.parse_args()
    repo = kc.repo_arg(args.repo)
    do_static = args.static or not args.behavior
    do_behavior = args.behavior or not args.static

    if not os.path.isfile(os.path.join(repo, "core", "bin", "comms")):
        kc.die("ERROR: core/bin/comms is absent; nothing to check")
    problems = []
    scanned = 0
    if do_static:
        scanned, found = static_part(repo)
        problems += found
    written = 0
    if do_behavior:
        written, found = behavior_part(repo, args.commands, args.real_home)
        problems += found
    for line in problems[:300]:
        print(line)
    if problems:
        print(f"data paths FAILED: {len(problems)} problems")
        return kc.EXIT_DEFECT
    if do_behavior:
        print(f"data paths ok: {written} files written under the data dir, code tree unchanged")
    else:
        print(f"data paths static ok: {scanned} files scanned")
    return kc.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
