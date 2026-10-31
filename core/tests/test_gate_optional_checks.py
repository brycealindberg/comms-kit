#!/usr/bin/env python3
"""The gate on a machine with none of the owner's private checks.

Ways this can go wrong: a clean install blocks every send in enforce mode because a private file
is absent; or a machine that DOES rely on the developer guard loses it silently when the file goes
missing; or a private approval variable keeps working after it left the config.
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
CORE = os.path.dirname(HERE)

CHECKER = """
import re
BUDGETS = {"whatsapp": (10, 4000)}
MONEY = re.compile(r"deposit")
def normalize_for_matching(text):
    return text
def check(bubbles, channel):
    return [], [], {}
"""


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = os.path.join(tmp.name, "home")
        self.kit = os.path.join(tmp.name, "kit")
        self.kit_checker = os.path.join(self.kit, "channels", "whatsapp", "draft_check.py")
        os.makedirs(self.home)
        os.makedirs(os.path.dirname(self.kit_checker))
        os.makedirs(os.path.join(self.kit, "core"))
        with open(self.kit_checker, "w", encoding="utf-8") as fh:
            fh.write(CHECKER)
        self.config = os.path.join(tmp.name, "config.json")
        self.settings = {"data_dir": os.path.join(self.home, "data")}
        self.write_config()
        env = patch.dict(os.environ, {"HOME": self.home, "COMMS_KIT_CONFIG": self.config,
                                      "GATE_MODE": "enforce", "DEV_OK": "", "CUSTOM_OK": ""})
        env.start()
        self.addCleanup(env.stop)
        spec = importlib.util.spec_from_file_location(
            "_gate_under_test", os.path.join(CORE, "outbound_gate.py"))
        self.G = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.G  # a dataclass reads its own module while it is defined
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(self.G)
        core = patch.object(self.G, "_core_dir", lambda: os.path.join(self.kit, "core"))
        core.start()
        self.addCleanup(core.stop)

    def write_config(self):
        with open(self.config, "w", encoding="utf-8") as fh:
            json.dump(self.settings, fh)

    def payload(self):
        return self.G.OutboundPayload("whatsapp", "fixture", ["hello from the test"],
                                      recipients=["+12065550123"])


class DraftCheckLookup(Base):
    def test_a_clean_install_uses_the_kit_default(self):
        self.assertEqual(os.path.abspath(self.G.draft_check_path()),
                         os.path.abspath(self.kit_checker))

    def test_your_own_checker_wins_over_the_default(self):
        own = os.path.join(self.home, ".claude", "skills", "whatsapp", "draft_check.py")
        os.makedirs(os.path.dirname(own))
        with open(own, "w", encoding="utf-8") as fh:
            fh.write(CHECKER)
        self.assertEqual(self.G.draft_check_path(), own)

    def test_with_neither_the_gate_reports_an_error_and_refuses(self):
        os.remove(self.kit_checker)
        verdict = self.G.evaluate(self.payload())
        self.assertEqual(verdict.checks["style"], "error")
        self.assertFalse(verdict.allow)


class DevGuard(Base):
    def test_no_guard_anywhere_is_off_and_a_clean_send_passes_enforce(self):
        verdict = self.G.evaluate(self.payload())
        self.assertEqual(verdict.checks["dev"], "off")
        self.assertTrue(verdict.allow)

    def test_a_configured_guard_that_went_missing_refuses_the_send(self):
        self.settings["outbound"] = {"dev_guard": os.path.join(self.home, "gone.py")}
        self.write_config()
        verdict = self.G.evaluate(self.payload())
        self.assertEqual(verdict.checks["dev"], "error")
        self.assertFalse(verdict.allow)


class DevApproval(Base):
    def test_dev_ok_counts(self):
        with patch.dict(os.environ, {"DEV_OK": "1"}):
            self.assertTrue(self.G.Approvals.from_env().dev_ok)

    def test_another_name_counts_only_when_the_config_lists_it(self):
        with patch.dict(os.environ, {"CUSTOM_OK": "1"}):
            self.assertFalse(self.G.Approvals.from_env().dev_ok)
            self.settings["outbound"] = {"dev_ok_env": ["CUSTOM_OK"]}
            self.write_config()
            self.assertTrue(self.G.Approvals.from_env().dev_ok)


if __name__ == "__main__":
    unittest.main()
