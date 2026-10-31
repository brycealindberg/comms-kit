#!/usr/bin/env python3
"""LIVE end-to-end proof of `react`, in the owner's OWN Slack self-DM only.

Config keys (all required): slack.e2e_workspace (your own workspace, never a client's),
slack.self_dm (your DM with yourself), slack.owner_user_id (your user id), slack.e2e_react_ts
(the ts of one message you wrote in that DM). The run refuses any other chat.
Medium case: thumbs up added under the name `thumbsup` (Slack stores it as `+1`), the same
again under `👍` must send nothing, a heart added next to it (Slack keeps several per
person), then both removed. Each step is checked by reading the reactions back from Slack.
Prints PASS/FAIL per step; exit 0 = all pass.

Run: python3 -I e2e_react_selfdm.py   (from this folder)
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
CLI = os.path.join(HERE, "slack_bridge.py")


def _config(key):
    """A required config value. The kit's kitconfig.py is found by COMMS_KIT_CORE, the kit layout, then the live Mac."""
    import importlib.util  # noqa: PLC0415
    folders = [os.environ.get("COMMS_KIT_CORE") or "", os.path.join(HERE, "..", "..", "core"),  # kit: code path
               os.path.expanduser("~/.claude/communications")]
    for folder in folders:
        path = os.path.abspath(os.path.join(os.path.expanduser(folder), "kitconfig.py")) if folder else ""
        if path and os.path.exists(path):
            spec = importlib.util.spec_from_file_location("kitconfig", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            try:
                return str(module.require(key))
            except module.ConfigError as err:
                sys.exit(f"refusing: {err}")
    sys.exit("refusing: kitconfig.py not found (set COMMS_KIT_CORE)")


WS, DM, SELF = _config("slack.e2e_workspace"), _config("slack.self_dm"), _config("slack.owner_user_id")
TS = _config("slack.e2e_react_ts")     # one message the owner wrote to themselves


def cli(*args):
    p = subprocess.run([sys.executable, CLI, *args], capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr


def mine():
    sys.path.insert(0, HERE)
    import slack_bridge as S  # noqa: PLC0415
    info = S._react_call(DM, WS, TS, "-", "check")
    if not info.get("ok") or info.get("self") != SELF:
        sys.exit(f"refusing: check did not come back as the owner's self-DM: {json.dumps(info)[:300]}")
    return sorted(S._norm_slack_name(r["name"]) for r in info.get("reactions") or []
                  if SELF in (r.get("users") or []))


def main():
    results = []

    def step(name, ok, out):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        if not ok:
            print("      " + out.strip().replace("\n", "\n      "))

    for leftover in mine():                       # clean start after a failed run
        cli("react", DM, TS, f":{leftover}:", "--workspace", WS, "--remove", "--yes")

    code, out = cli("react", DM, TS, "thumbsup", "--workspace", WS)
    step("dry run writes nothing", code == 0 and "Dry run" in out and mine() == [], out)

    code, out = cli("react", DM, TS, "thumbsup", "--workspace", WS, "--yes")
    step("thumbsup lands as +1", code == 0 and "Reacted :+1:" in out and mine() == ["+1"], out)

    code, out = cli("react", DM, TS, "👍", "--workspace", WS, "--yes")
    step("same reaction as a raw emoji sends nothing", code == 0 and "already reacted" in out, out)

    code, out = cli("react", DM, TS, "heart", "--workspace", WS, "--yes")
    step("heart lands next to it", code == 0 and mine() == ["+1", "heart"], out)

    code, out = cli("react", DM, TS, "--workspace", WS, "--remove", "--yes")
    step("--remove with two reactions asks which", code != 0 and "name the one" in out, out)

    cli("react", DM, TS, "heart", "--workspace", WS, "--remove", "--yes")
    code, out = cli("react", DM, TS, "+1", "--workspace", WS, "--remove", "--yes")
    step("both removed", code == 0 and mine() == [], out)

    print(f"\n{sum(results)}/{len(results)} passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
