"""TeamShift carry fixes: native approval for every send, cron blocks, neutral text, stripped tree.

No real mail is sent: SMTP goes through a recording transport, and smtplib itself is made to fail.
Engine-dependent checks skip when the engine is not importable.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import smtplib
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import approval
import commands
import mail
import plugin_settings
import storage
import tools

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PY = sorted(p for p in ROOT.rglob("*.py") if not ({"tests", ".git"} & set(p.relative_to(ROOT).parts)))
ENGINE_WORDS = re.compile(r"\bHermes\b|\bDesktop\b|\bTUI\b|\bCLI\b|Settings → Plugins")


def _call(fn, args, **kwargs):
    return json.loads(fn(args, **kwargs))


def _has_engine() -> bool:
    """The engine can load plugins. Checked without importing it here: the plugin's own top-level
    ``tools`` module shadows the engine package of that name in this process."""
    import importlib.util

    try:
        return importlib.util.find_spec("hermes_cli.plugins") is not None
    except Exception:
        return False


class _NoRealSmtp(unittest.TestCase):
    """A seeded brand with one qualified lead, an email draft, a sender and a recording transport."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        storage.set_data_dir(Path(self.tmp.name))
        approval.clear()
        seeded = _call(tools.import_customers, {"seed_demo": True})
        self.business_id = seeded["business_id"]
        self.lead_id = next(item["id"] for item in seeded["prospects"] if item["decision"] == "qualified")
        self.draft = _call(tools.draft_outreach, {"lead_id": self.lead_id, "channel": "email"})["draft"]
        with storage.unit_of_work() as conn:
            plugin_settings.save_wizard_account(conn, self.business_id, {
                "host": "smtp.example.com", "port": 587, "security": "starttls",
                "from_address": "nia@example.com", "from_name": "Nia Demo", "signature_text": "Nia Demo, Cedarline",
            })
        self.calls = []
        mail.set_transport_for_tests(lambda account, user, password, message: self.calls.append(message) or (250, False))
        patches = [
            mock.patch.object(commands, "_secrets", lambda: ("nia@example.com", "not-a-real-password", "")),
            mock.patch.object(smtplib, "SMTP", side_effect=AssertionError("real SMTP opened")),
            mock.patch.object(smtplib, "SMTP_SSL", side_effect=AssertionError("real SMTP opened")),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.hook = approval.make_hook(commands.send_preview, tools.ledger_preview)

    def tearDown(self):
        mail.set_transport_for_tests(None)
        approval.clear()
        commands.bind(None)
        approval.bind(None)
        storage.set_data_dir(None)
        self.tmp.cleanup()

    def send_args(self):
        return {"draft_id": self.draft["id"]}


class NativeApprovalTests(_NoRealSmtp):
    def test_card_shows_exact_email_and_send_goes_out_once_after_it(self):
        directive = self.hook(tool_name=approval.SEND_TOOL, args=self.send_args(), session_id="s1", task_id="t1")
        self.assertEqual(directive["action"], "approve")
        self.assertTrue(directive["rule_key"].startswith("salespilot:salespilot_send_email:"))
        card = directive["message"]
        self.assertIn(self.draft["recipient"], card)
        self.assertIn(self.draft["subject"], card)
        self.assertIn(self.draft["body"].strip(), card)
        self.assertIn("Nia Demo, Cedarline", card)  # the signature that will be appended is shown too
        self.assertIn("nia@example.com", card)
        result = _call(commands.send_email, self.send_args(), session_id="s1", task_id="t1")
        self.assertTrue(result["smtp_accepted"], result)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["To"], self.draft["recipient"])
        self.assertEqual(self.calls[0]["Subject"], self.draft["subject"])
        self.assertIn(self.calls[0].get_body(("plain",)).get_content().strip(), card)

    def test_send_without_native_yes_fails_closed(self):
        refused = _call(commands.send_email, {**self.send_args(), "human_confirmed": True, "approved": True})
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["code"], "host_approval_unavailable")
        self.assertEqual(self.calls, [])

    def test_a_yes_is_single_use(self):
        self.hook(tool_name=approval.SEND_TOOL, args=self.send_args())
        # The first yes is spent by a refusal path that never reaches SMTP: a changed draft.
        with storage.unit_of_work() as conn:
            conn.execute("UPDATE drafts SET body=? WHERE id=?", (self.draft["body"] + "\nP.S. a new line", self.draft["id"]))
        changed = _call(commands.send_email, self.send_args())
        self.assertEqual(changed["code"], "host_approval_unavailable")
        again = _call(commands.send_email, self.send_args())
        self.assertEqual(again["code"], "host_approval_unavailable")
        self.assertEqual(self.calls, [])

    def test_yes_is_bound_to_recipient_subject_and_body(self):
        for column, value in (("recipient", "someone.else@example.org"), ("subject", "A different subject"),
                              ("body", "A different body.")):
            with self.subTest(column=column):
                self.hook(tool_name=approval.SEND_TOOL, args=self.send_args())
                with storage.unit_of_work() as conn:
                    original = conn.execute(f"SELECT {column} FROM drafts WHERE id=?", (self.draft["id"],)).fetchone()[0]
                    conn.execute(f"UPDATE drafts SET {column}=? WHERE id=?", (value, self.draft["id"]))
                result = _call(commands.send_email, self.send_args())
                self.assertFalse(result.get("smtp_accepted", False), result)
                self.assertEqual(self.calls, [])
                with storage.unit_of_work() as conn:
                    conn.execute(f"UPDATE drafts SET {column}=? WHERE id=?", (original, self.draft["id"]))

    def test_yes_does_not_carry_to_another_session_or_draft(self):
        self.hook(tool_name=approval.SEND_TOOL, args=self.send_args(), session_id="owner-chat")
        other = _call(commands.send_email, self.send_args(), session_id="another-chat")
        self.assertEqual(other["code"], "host_approval_unavailable")
        self.assertEqual(self.calls, [])

    def test_each_call_gets_its_own_rule_key(self):
        keys = {self.hook(tool_name=approval.SEND_TOOL, args=self.send_args())["rule_key"] for _ in range(3)}
        self.assertEqual(len(keys), 3)  # an "always" answer can never pre-approve another email

    def test_denied_or_blocked_call_leaves_no_usable_yes(self):
        self.hook(tool_name=approval.SEND_TOOL, args=self.send_args(), session_id="s")
        approval.post_tool_call(tool_name=approval.SEND_TOOL, args=self.send_args(), session_id="s", status="blocked")
        result = _call(commands.send_email, self.send_args(), session_id="s")
        self.assertEqual(result["code"], "host_approval_unavailable")
        self.assertEqual(self.calls, [])

    def test_hook_failure_blocks(self):
        broken = approval.make_hook(lambda draft_id: 1 / 0, tools.ledger_preview)
        directive = broken(tool_name=approval.SEND_TOOL, args=self.send_args())
        self.assertEqual(directive["action"], "block")

    def test_overlong_email_is_blocked_rather_than_shown_cut(self):
        with storage.unit_of_work() as conn:
            conn.execute("UPDATE drafts SET body=? WHERE id=?", ("x" * (approval.BODY_LIMIT + 10), self.draft["id"]))
        directive = self.hook(tool_name=approval.SEND_TOOL, args=self.send_args())
        self.assertEqual(directive["action"], "block")

    def test_approvals_switched_off_blocks_sending(self):
        with mock.patch.object(approval, "approvals_bypassed", lambda: True):
            directive = self.hook(tool_name=approval.SEND_TOOL, args=self.send_args())
        self.assertEqual(directive["action"], "block")
        self.assertEqual(self.calls, [])

    def test_other_tools_are_not_gated(self):
        self.assertIsNone(self.hook(tool_name="salespilot_report", args={}))
        self.assertIsNone(self.hook(tool_name="terminal", args={"command": "ls"}))
        self.assertIsNone(self.hook(tool_name=approval.LEDGER_TOOL, args={"draft_id": self.draft["id"], "decision": "request"}))

    def test_ledger_approve_needs_native_yes(self):
        args = {"draft_id": self.draft["id"], "decision": "approve", "human_confirmed": True}
        self.assertEqual(_call(tools.request_approval, dict(args))["code"], "host_approval_unavailable")
        directive = self.hook(tool_name=approval.LEDGER_TOOL, args=dict(args))
        self.assertEqual(directive["action"], "approve")
        self.assertIn("does not send", directive["message"])
        saved = _call(tools.request_approval, dict(args))
        self.assertEqual(saved["approval"]["status"], "approved")
        self.assertEqual(self.calls, [])

    def test_slash_command_shows_body_and_rechecks(self):
        asked = []

        class Host:
            def ask_choice(self, question, choices):
                asked.append(question)
                return "send"

        commands.bind(Host())
        result = json.loads(commands.slash_send(self.draft["id"]))
        self.assertTrue(result["smtp_accepted"])
        self.assertIn(self.draft["body"].strip(), asked[0])
        self.assertEqual(len(self.calls), 1)


class CronTests(_NoRealSmtp):
    def test_cron_session_blocks_send_and_ledger_even_from_a_delegated_child(self):
        class NoCronIdentity:  # what a subagent delegated from a cron run sees
            def current_cron_execution(self):
                return None

            def ask_choice(self, question, choices):
                return "send"

        approval.bind(NoCronIdentity())
        commands.bind(NoCronIdentity())
        with mock.patch.dict(os.environ, {"HERMES_CRON_SESSION": "1"}):
            self.assertTrue(approval.unattended())
            send = self.hook(tool_name=approval.SEND_TOOL, args=self.send_args())
            ledger = self.hook(tool_name=approval.LEDGER_TOOL, args={"draft_id": self.draft["id"], "decision": "approve"})
            self.assertEqual(send["action"], "block")
            self.assertIn("unattended", send["message"])
            self.assertEqual(ledger["action"], "block")
        # Even a ticket issued outside cron cannot be spent inside a cron run.
        self.hook(tool_name=approval.SEND_TOOL, args=self.send_args())
        with mock.patch.dict(os.environ, {"HERMES_CRON_SESSION": "1"}):
            refused = _call(commands.send_email, self.send_args())
            self.assertEqual(refused["code"], "unattended")
            slash = json.loads(commands.slash_send(self.draft["id"]))
            self.assertEqual(slash["code"], "unattended")
        self.assertEqual(self.calls, [])
        self.assertFalse(approval.unattended())

    def test_cron_identity_blocks(self):
        class InCron:
            def current_cron_execution(self):
                return object()

        approval.bind(InCron())
        self.assertEqual(self.hook(tool_name=approval.SEND_TOOL, args=self.send_args())["action"], "block")


def _user_strings(path: Path) -> list[str]:
    """String literals in a module, minus docstrings (developer text a person never sees)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in docstrings]


class WhiteLabelTests(unittest.TestCase):
    def test_no_engine_name_in_runtime_string_literals(self):
        for path in RUNTIME_PY:
            with self.subTest(path=str(path.relative_to(ROOT))):
                bad = [s for s in _user_strings(path) if ENGINE_WORDS.search(s)]
                self.assertEqual(bad, [])

    def test_no_engine_name_in_manifest_tool_text_or_skill(self):
        import schemas

        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        visible = [line for line in manifest.splitlines() if re.match(r"\s*(description|label):", line)]
        visible += [schema["description"] for schema in schemas.TOOLS]
        visible += [prop.get("description", "") for schema in schemas.TOOLS
                    for prop in schema["parameters"]["properties"].values()]
        skill = (ROOT / "skills" / "salespilot" / "SKILL.md").read_text(encoding="utf-8")
        visible.append(skill.split("---", 2)[2])
        self.assertEqual([v for v in visible if ENGINE_WORDS.search(v)], [])


class StrippedTreeTests(unittest.TestCase):
    def test_stripped_paths_are_absent(self):
        for name in ("desktop", "scripts", "docs", "adapters/evolution.py", "tests/desktop"):
            self.assertFalse((ROOT / name).exists(), name)

    def test_runtime_does_not_import_stripped_paths(self):
        for path in RUNTIME_PY:
            text = path.read_text(encoding="utf-8")
            for name in ("evolution", "scripts", "desktop", "docs"):
                self.assertIsNone(re.search(rf"^\s*(from|import)\s+[.\w]*\b{name}\b", text, re.M), (path.name, name))


_ENGINE_SCRIPT = textwrap.dedent("""
    import json, os, sys, tempfile
    from pathlib import Path
    from unittest import mock
    import smtplib
    import hermes_cli.plugins_discovery as disc
    disc._get_enabled_plugins = lambda: {"hermes-salespilot"}
    from hermes_cli.plugins import discover_plugins, get_plugin_manager
    discover_plugins(force=True)
    plugin = get_plugin_manager()._plugins["hermes-salespilot"]
    from tools.registry import registry
    names = sorted(n for n in registry.get_all_tool_names() if n.startswith("salespilot_"))
    toolsets = sorted({registry.get_entry(n).toolset for n in names})
    pkg = "hermes_plugins.hermes_salespilot"
    storage, tools, commands, mail, settings = (sys.modules[f"{pkg}.{m}"] for m in
                                                 ("storage", "tools", "commands", "mail", "plugin_settings"))
    storage.set_data_dir(Path(tempfile.mkdtemp()))
    seeded = json.loads(tools.import_customers({"seed_demo": True}))
    lead = next(i["id"] for i in seeded["prospects"] if i["decision"] == "qualified")
    draft = json.loads(tools.draft_outreach({"lead_id": lead, "channel": "email"}))["draft"]
    with storage.unit_of_work() as conn:
        settings.save_wizard_account(conn, seeded["business_id"], {"host": "smtp.example.com", "port": 587,
            "security": "starttls", "from_address": "nia@example.com", "from_name": "Nia"})
    calls = []
    mail.set_transport_for_tests(lambda a, u, p, m: calls.append(m["To"]) or (250, False))
    commands._secrets = lambda: ("nia@example.com", "not-a-real-password", "")
    smtplib.SMTP = smtplib.SMTP_SSL = None
    import tools.approval as gate
    import model_tools
    asked = []
    def answer(choice):
        def fake(tool_name, reason, *, rule_key="", approval_callback=None):
            asked.append((tool_name, rule_key, reason))
            return {"approved": choice, "message": "BLOCKED: the owner said no"}
        return fake
    out = {"tools": len(names), "toolsets": toolsets, "hooks": sorted(plugin.hooks_registered),
           "commands": plugin.commands_registered}
    with mock.patch.object(gate, "request_tool_approval", answer(False)):
        denied = model_tools.handle_function_call("salespilot_send_email", {"draft_id": draft["id"]}, task_id="t")
    out["denied_calls"] = len(calls)
    out["denied_blocked"] = "the owner said no" in denied
    with mock.patch.object(gate, "request_tool_approval", answer(True)):
        sent = json.loads(model_tools.handle_function_call("salespilot_send_email", {"draft_id": draft["id"]}, task_id="t"))
    out["approved_accepted"] = bool(sent.get("smtp_accepted"))
    out["approved_calls"] = calls
    out["recipient"] = draft["recipient"]
    out["card_has_body"] = draft["body"].strip() in asked[-1][2]
    out["rule_keys_distinct"] = len({k for _, k, _ in asked}) == len(asked) == 2
    with mock.patch.dict(os.environ, {"HERMES_CRON_SESSION": "1"}), \\
         mock.patch.object(gate, "request_tool_approval", answer(True)):
        cron = model_tools.handle_function_call("salespilot_send_email", {"draft_id": draft["id"]}, task_id="t")
    out["cron_blocked"] = "unattended" in cron and len(asked) == 2
    print("RESULT " + json.dumps(out))
""")


@unittest.skipUnless(_has_engine(), "needs the engine")
class EngineIntegrationTests(unittest.TestCase):
    def test_stripped_copy_loads_and_engine_gate_controls_the_send(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            shutil.copytree(ROOT, home / "plugins" / "hermes-salespilot",
                            ignore=shutil.ignore_patterns(".git", "tests", "__pycache__", "README.md", "catalog", "assets"))
            (home / "config.yaml").write_text("plugins:\n  enabled: [hermes-salespilot]\n  isolation: in_process\n")
            env = {k: v for k, v in os.environ.items()
                   if not k.startswith(("HERMES_", "SALESPILOT_", "SMTP", "IMAP"))}
            env.update(HERMES_HOME=str(home), HOME=tmp)
            proc = subprocess.run([sys.executable, "-c", _ENGINE_SCRIPT], env=env, cwd=tmp,
                                  capture_output=True, text=True, timeout=300)
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        self.assertIsNotNone(line, proc.stderr[-3000:])
        out = json.loads(line[len("RESULT "):])
        self.assertEqual(out["tools"], 12)
        self.assertEqual(out["toolsets"], ["salespilot"])
        self.assertEqual(out["hooks"], ["post_tool_call", "pre_tool_call"])
        self.assertTrue(out["denied_blocked"])
        self.assertEqual(out["denied_calls"], 0)
        self.assertTrue(out["approved_accepted"])
        self.assertEqual(out["approved_calls"], [out["recipient"]])
        self.assertTrue(out["card_has_body"])
        self.assertTrue(out["rule_keys_distinct"])
        self.assertTrue(out["cron_blocked"])


if __name__ == "__main__":
    unittest.main()
