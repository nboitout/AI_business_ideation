"""The claude-cli backend, exercised against a fake `claude` executable."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agents_common
import config
import llm_backends
from agents_common import Evaluator, _chat
from rank_originals import order_with_tie_breaks

MODEL = "claude-cli/claude-sonnet-5"


def _has_zone(name: str) -> bool:
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return True
    except Exception:
        return False

# Replays the scripted responses in $FAKE_CLAUDE_SCRIPT (one per invocation,
# the last one repeating) and records each invocation in $FAKE_CLAUDE_LOG.
FAKE_CLAUDE = textwrap.dedent(
    """\
    #!/usr/bin/env python3
    import json, os, sys
    prompt = sys.stdin.read()
    log = os.environ["FAKE_CLAUDE_LOG"]
    calls = []
    if os.path.exists(log):
        calls = json.load(open(log, encoding="utf-8"))
    calls.append({"argv": sys.argv[1:], "cwd": os.getcwd(), "prompt": prompt,
                  "cwd_entries": os.listdir("."),
                  "max_thinking_tokens": os.environ.get("MAX_THINKING_TOKENS")})
    json.dump(calls, open(log, "w", encoding="utf-8"))
    script = json.load(open(os.environ["FAKE_CLAUDE_SCRIPT"], encoding="utf-8"))
    step = script[min(len(calls), len(script)) - 1]
    if step["kind"] == "crash":
        sys.stderr.write(step["message"])
        sys.exit(2)
    ok = step["kind"] == "ok"
    print(json.dumps({
        "type": "result",
        "subtype": "success" if ok else "error_during_execution",
        "is_error": not ok,
        "api_error_status": step.get("status"),
        "result": step["message"],
        "usage": {"input_tokens": 900, "cache_read_input_tokens": 100, "output_tokens": 40},
        "modelUsage": {"claude-sonnet-5": {}} if ok else {},
        **({"structured_output": step["structured"]} if "structured" in step else {}),
    }))
    sys.exit(0 if ok else 1)
    """
)


class ClaudeCliBackendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        script = tmp / "fake_claude.py"
        script.write_text(FAKE_CLAUDE, encoding="utf-8")
        if os.name == "nt":
            # Windows runs the CLI as claude.cmd or claude.exe, not a script.
            self.fake = tmp / "claude.cmd"
            self.fake.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            self.fake = script
            self.fake.chmod(self.fake.stat().st_mode | stat.S_IEXEC)
        self.log = tmp / "calls.json"
        self.script = tmp / "script.json"
        self.env = patch.dict(os.environ, {
            "FAKE_CLAUDE_LOG": str(self.log),
            "FAKE_CLAUDE_SCRIPT": str(self.script),
            "AI_ENTREP_CALL_CACHE": str(tmp / "ledger.sqlite3"),
        })
        self.env.start()
        os.environ.pop("ANTHROPIC_API_KEY", None)
        os.environ.pop("AI_ENTREP_MOCK_LLM", None)
        self.settings = patch.multiple(
            config, CLAUDE_CLI_BIN=str(self.fake), CLAUDE_CLI_LIMIT_WAIT=1, CLAUDE_CLI_RATE_WAIT=1,
        )
        self.settings.start()
        llm_backends._paused_until = 0.0
        llm_backends._backend_checked = False
        llm_backends._served_models.clear()
        agents_common._semaphore = None

    def tearDown(self):
        self.settings.stop()
        self.env.stop()
        if agents_common._cache_connection is not None:
            agents_common._cache_connection.close()
            agents_common._cache_connection = None
            agents_common._cache_connection_path = ""
        self.tmp.cleanup()

    def script_responses(self, *steps):
        self.script.write_text(json.dumps([{"kind": kind, "message": message} for kind, message in steps]), encoding="utf-8")

    def calls(self):
        return json.loads(self.log.read_text(encoding="utf-8")) if self.log.exists() else []

    def chat(self, prompt="Compare A and B."):
        return asyncio.run(llm_backends.claude_cli_chat(MODEL, prompt, asyncio.Semaphore(2)))

    def test_call_is_a_stateless_plain_completion(self):
        self.script_responses(("ok", '```json\n{"winner": "A"}\n```'))
        outcome = self.chat("PROMPT TEXT")
        self.assertEqual(outcome.text, '```json\n{"winner": "A"}\n```')
        self.assertEqual((outcome.input_tokens, outcome.output_tokens), (1000, 40))
        self.assertEqual(outcome.served_model, "claude-sonnet-5")
        (call,) = self.calls()
        argv = call["argv"]
        self.assertEqual(call["prompt"], "PROMPT TEXT")
        self.assertEqual(argv[argv.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(argv[argv.index("--system-prompt") + 1], config.CLAUDE_CLI_SYSTEM_PROMPT)
        self.assertEqual(argv[argv.index("--tools") + 1], "")
        for flag in ("-p", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence"):
            self.assertIn(flag, argv)
        self.assertEqual(call["cwd_entries"], [])
        self.assertEqual(call["max_thinking_tokens"], "0")

    def test_malformed_reply_is_recovered_and_ledger_keeps_raw_text(self):
        # Claude quoted a phrase without escaping it: invalid JSON, same verdict.
        raw = '{"analysis": "unclear how "proving originality" holds up", "winner": "B"}'
        self.script_responses(("ok", raw))
        with patch.object(config, "EVALUATOR_MODEL", MODEL):
            winner, analysis = asyncio.run(Evaluator()._match("plan one", "plan two"))
        self.assertEqual((winner, analysis), ("B", 'unclear how "proving originality" holds up'))
        self.assertEqual(len(self.calls()), 1)
        ledger = agents_common._get_cache_connection().execute("SELECT response FROM calls").fetchall()
        self.assertEqual(ledger, [(raw,)])

    def test_usage_limit_pauses_and_retries_instead_of_failing(self):
        self.script_responses(
            ("error", "Claude AI usage limit reached"),
            ("error", "API Error: 429 rate limit"),
            ("ok", '{"winner": "B"}'),
        )
        with self.assertLogs(llm_backends.logger, "WARNING"):
            outcome = self.chat()
        self.assertEqual(outcome.text, '{"winner": "B"}')
        self.assertEqual(len(self.calls()), 3)

    def test_reset_epoch_in_message_sets_resume_time(self):
        outcome = llm_backends.classify_failure("Claude AI usage limit reached|1750000000")
        self.assertEqual(outcome.kind, "quota")
        self.assertEqual(outcome.resume_at, 1750000060.0)
        self.assertEqual(llm_backends.classify_failure("overloaded", 529).kind, "rate")

    def test_authentication_failure_stops_the_process(self):
        self.script_responses(("error", "Invalid API key · Please run /login"))
        with self.assertRaises(SystemExit):
            self.chat()
        self.assertEqual(len(self.calls()), 1)

    def test_other_failures_raise_a_retryable_error(self):
        self.script_responses(("crash", "segmentation fault"))
        with self.assertRaises(llm_backends.ClaudeCliError):
            self.chat()

    def test_api_key_in_environment_is_refused(self):
        self.script_responses(("ok", "{}"))
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with self.assertRaises(SystemExit):
                self.chat()
        self.assertEqual(self.calls(), [])

    def test_chat_routes_through_the_cli_and_the_ledger(self):
        self.script_responses(("ok", '{"analysis": "x", "winner": "A"}'))
        first = asyncio.run(_chat(MODEL, "same prompt", role="Evaluator", context="t"))
        agents_common._semaphore = None
        second = asyncio.run(_chat(MODEL, "same prompt", role="Evaluator", context="t"))
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls()), 1)  # the second call replayed from the ledger

    def test_usage_limit_never_becomes_a_tie(self):
        self.script_responses(
            ("error", "You've hit your usage limit"),
            ("ok", '{"analysis": "B is clearer", "winner": "B"}'),
        )
        with patch.object(config, "EVALUATOR_MODEL", MODEL), self.assertLogs(llm_backends.logger, "WARNING"):
            winner, _analysis = asyncio.run(Evaluator()._match("plan one", "plan two"))
        self.assertEqual(winner, "B")


class ReplyRepairTest(unittest.TestCase):
    def test_well_formed_replies_are_untouched(self):
        for text in ('{"analysis": "fine", "winner": "B"}', '```json\n{"analysis": "fine", "winner": "B"}\n```'):
            self.assertEqual(llm_backends.normalize_reply("Evaluator", text), text)
        self.assertEqual(llm_backends.normalize_reply("Narrator", 'He said "hi"'), 'He said "hi"')

    def test_each_role_format_is_recovered(self):
        cases = {
            "Evaluator": ('{"analysis": "x "y"\nz", "winner": " A "}', {"analysis": 'x "y"\nz', "winner": "A"}),
            "Fidelity Auditor": (
                '{"passed": false, "unsupported_claims": ["the "core" works", "units shipped"], "analysis": "says "live""}',
                {"passed": False, "unsupported_claims": ['the "core" works', "units shipped"], "analysis": 'says "live"'},
            ),
            "Specialized Generator": ('{"plan": "## Problem\nThey say "hi".", "evidence_notes": "n"}',
                                      {"plan": '## Problem\nThey say "hi".', "evidence_notes": "n"}),
            "Mutator": ('Sure:\n{"component": "customer", "plan": "A "new" segment"}',
                        {"component": "customer", "plan": 'A "new" segment'}),
            "Generator": ('{"ideas": ["Add a "pilot" plan", "Clarify pricing"]}',
                          {"ideas": ['Add a "pilot" plan', "Clarify pricing"]}),
        }
        for role, (text, expected) in cases.items():
            with self.subTest(role=role):
                self.assertEqual(json.loads(llm_backends.normalize_reply(role, text)), expected)

    def test_unrecoverable_replies_are_left_to_fail_as_before(self):
        for text in ('{"analysis": "no verdict"}', '{"analysis": "x "y"", "winner": "C"}', "no json at all"):
            self.assertEqual(llm_backends.normalize_reply("Evaluator", text), text)


class ResetTimeTest(unittest.TestCase):
    NOW = datetime(2026, 9, 25, 17, 22, 26, tzinfo=timezone.utc)

    def test_epoch_and_unparseable_messages(self):
        self.assertEqual(llm_backends.parse_reset_time("Claude AI usage limit reached|1790000000"), 1790000000.0)
        self.assertIsNone(llm_backends.parse_reset_time("usage limit reached"))

    @unittest.skipUnless(_has_zone("Europe/Bucharest"), "no time-zone database (install tzdata)")
    def test_clock_time_in_the_named_zone(self):
        # The message from a real session limit: 20:22 in Bucharest, reset at 23:10.
        message = "You've hit your session limit · resets 11:10pm (Europe/Bucharest)"
        reset = llm_backends.parse_reset_time(message, self.NOW)
        self.assertEqual(datetime.fromtimestamp(reset, timezone.utc), datetime(2026, 9, 25, 20, 10, tzinfo=timezone.utc))
        outcome = llm_backends.classify_failure(message)
        self.assertEqual(outcome.kind, "quota")
        self.assertIsNotNone(outcome.resume_at)

    @unittest.skipUnless(_has_zone("Europe/Paris"), "no time-zone database (install tzdata)")
    def test_dated_weekly_reset(self):
        reset = llm_backends.parse_reset_time("Weekly limit · resets Sep 29, 3pm (Europe/Paris)", self.NOW)
        self.assertEqual(datetime.fromtimestamp(reset, timezone.utc), datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc))

    def test_time_already_past_today_means_tomorrow(self):
        now = datetime.now().astimezone()
        earlier = (now - timedelta(hours=1)).replace(minute=0)
        label = f"{earlier.hour % 12 or 12}{'pm' if earlier.hour >= 12 else 'am'}"
        reset = llm_backends.parse_reset_time(f"resets {label}", now)
        self.assertTrue(now.timestamp() < reset <= now.timestamp() + 24 * 3600)


class SeedBoundaryTieBreakTest(unittest.TestCase):
    def test_boundary_tie_is_broken_head_to_head(self):
        # Mirrors the paper: two ventures tie across the seed boundary and the
        # one that won their head-to-head stays out of the seed set.
        ids = ["top", "soloist", "clockchain", "bottom"]
        rates = [1.0, 0.5, 0.5, 0.0]
        details = {(1, 2): ("plan_1", ""), (2, 1): ("plan_1", "")}
        order, notes, unresolved = order_with_tie_breaks(ids, rates, details, boundary=2)
        self.assertEqual([ids[i] for i in order], ["top", "soloist", "clockchain", "bottom"])
        self.assertFalse(unresolved)
        self.assertIn("soloist 2", notes[1])

    def test_unresolved_boundary_tie_is_flagged(self):
        ids = ["a", "b", "c", "d"]
        details = {(1, 2): ("plan_1", ""), (2, 1): ("plan_1", "")}
        _order, _notes, unresolved = order_with_tie_breaks(ids, [1.0, 0.5, 0.5, 0.0], {**details, (2, 1): ("plan_2", "")}, 2)
        self.assertTrue(unresolved)


if __name__ == "__main__":
    unittest.main()
