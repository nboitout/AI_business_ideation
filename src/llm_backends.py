"""Model backends other than OpenRouter.

claude-cli: each call is one headless `claude -p` invocation of the Claude Code
CLI, billed to the Claude subscription the CLI is logged in with (Pro or Max)
instead of an API account.  Model ids of the form "claude-cli/<model>" select
it (see config.is_claude_cli_model).

Every call is stateless, as the OpenRouter calls were: the prompt goes in as
the only user message, Claude Code's agent system prompt is replaced by a
one-line neutral one, tools, MCP servers, skills, and session persistence are
off, and the working directory is an empty temporary directory so that no
project CLAUDE.md or settings are discovered.

Subscription usage limits never surface to the callers.  The evaluator scores a
failed call as a tie and the fidelity auditor as a rejection, so a limit that
propagated would silently distort the search.  Instead every call pauses until
the limit resets and then retries; the run can also be interrupted at any time
and resumed from the call ledger by rerunning the same command.  An
authentication failure ends the process instead of being retried.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import re
import shutil
import tempfile
import time

import config

logger = logging.getLogger(__name__)

QUOTA_PATTERN = re.compile(
    r"usage limit|hit your (?:usage |session |weekly )?limit|limit reached|limit will reset|out of (?:extra )?usage",
    re.IGNORECASE,
)
RATE_PATTERN = re.compile(r"rate.?limit|overloaded|too many requests", re.IGNORECASE)
AUTH_PATTERN = re.compile(
    r"not logged in|please run /login|invalid api key|authentication|oauth token|credit balance",
    re.IGNORECASE,
)
RESET_EPOCH_PATTERN = re.compile(r"\|(\d{10})\b")
# "resets 11:10pm (Europe/Bucharest)", "resets Sep 27, 3pm (Europe/Paris)"
RESET_CLOCK_PATTERN = re.compile(
    r"resets\s+(?:(?P<month>[A-Z][a-z]{2})[a-z]*\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>am|pm)\b(?:\s*\((?P<tz>[^)]+)\))?",
    re.IGNORECASE,
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


def _object(properties: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_A_OR_B = {"type": "string", "enum": ["A", "B"]}

# The reply format each role's prompt asks for ("Respond with a JSON object:
# ..."), fields in the prompt's order.  Claude often writes these replies with
# unescaped double quotes inside the analysis or plan text, which breaks the
# JSON; after five failed parses the evaluator would record a tie.
# normalize_reply() recovers such replies by cutting each field's value
# between the known keys.  (`claude --json-schema` is not used: with long
# string fields its tool call intermittently merges the next field into the
# previous string and fails after several paid turns.)  The Narrator answers in
# plain text and has no entry.
ROLE_SCHEMAS = {
    "Evaluator": _object({"analysis": _STRING, "winner": _A_OR_B}),
    "Generator": _object({"ideas": {"type": "array", "items": _STRING}}),
    "Specialized Generator": _object({"plan": _STRING, "evidence_notes": _STRING}),
    "Mutator": _object({"component": _STRING, "plan": _STRING}),
    "Crossover": _object({
        "component_map": _object({
            name: _A_OR_B
            for name in ("problem", "customer", "solution", "delivery_model", "revenue_logic", "distinctiveness")
        }),
        "plan": _STRING,
    }),
    "Fidelity Auditor": _object({
        "passed": {"type": "boolean"},
        "unsupported_claims": {"type": "array", "items": _STRING},
        "analysis": _STRING,
    }),
}


class ClaudeCliError(RuntimeError):
    """A failed CLI call that the caller's retry policy may retry."""


@dataclass
class CliOutcome:
    kind: str  # "ok", "quota", "rate", "auth", or "error"
    text: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    served_model: str = ""
    resume_at: float | None = None


_paused_until = 0.0
_backend_checked = False
_workdir: str | None = None
_served_models: dict[str, int] = {}


def get_served_models() -> dict[str, int]:
    """Return {model actually served: live calls} for the current process."""
    return dict(_served_models)


def _empty_workdir() -> str:
    global _workdir
    if _workdir is None:
        _workdir = tempfile.mkdtemp(prefix="conceptual-search-claude-")
    return _workdir


def claude_cli_command(model: str) -> list[str]:
    # The full path: on Windows the CLI is claude.cmd or claude.exe, which a
    # subprocess does not find from the bare name.
    return [
        shutil.which(config.CLAUDE_CLI_BIN) or config.CLAUDE_CLI_BIN,
        "-p",
        "--model", config.claude_cli_model_name(model),
        "--output-format", "json",
        "--system-prompt", config.CLAUDE_CLI_SYSTEM_PROMPT,
        "--tools", "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--effort", config.CLAUDE_CLI_EFFORT,
    ]


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    # The paper's calls ran with reasoning disabled.
    env["MAX_THINKING_TOKENS"] = "0"
    return env


def _time_zone(name: str | None):
    """The named zone, or the machine's local zone when it cannot be resolved
    (Windows has no zone database unless the tzdata package is installed)."""
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name.strip())
        except Exception:
            pass
    return datetime.now().astimezone().tzinfo


def parse_reset_time(message: str, now: datetime | None = None) -> float | None:
    """Epoch seconds of the reset a usage-limit message announces, if any."""
    match = RESET_EPOCH_PATTERN.search(message)
    if match:
        return float(match.group(1))
    match = RESET_CLOCK_PATTERN.search(message)
    if match is None:
        return None
    zone = _time_zone(match.group("tz"))
    now = (now or datetime.now(timezone.utc)).astimezone(zone)
    hour = int(match.group("hour")) % 12 + (12 if match.group("ampm").lower() == "pm" else 0)
    minute = int(match.group("minute") or 0)
    if match.group("month"):
        month = _MONTHS.index(match.group("month")[:3].lower()) + 1
        reset = now.replace(month=month, day=int(match.group("day")), hour=hour, minute=minute, second=0, microsecond=0)
        if reset < now - timedelta(days=1):
            reset = reset.replace(year=reset.year + 1)
    else:
        reset = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= now:
            reset += timedelta(days=1)
    delay = (reset - now).total_seconds()
    if not 0 <= delay <= 8 * 24 * 3600:
        return None
    return reset.timestamp()


def classify_failure(message: str, api_status: object = None) -> CliOutcome:
    """Sort a failed call into quota, rate, auth, or other error."""
    if AUTH_PATTERN.search(message) or api_status in (401, 403):
        return CliOutcome("auth", text=message)
    if QUOTA_PATTERN.search(message):
        reset = parse_reset_time(message)
        resume_at = reset + 60 if reset is not None else None
        return CliOutcome("quota", text=message, resume_at=resume_at)
    if RATE_PATTERN.search(message) or api_status in (429, 529):
        return CliOutcome("rate", text=message)
    return CliOutcome("error", text=message)


def parse_cli_output(returncode: int, stdout: str, stderr: str) -> CliOutcome:
    """Interpret one `claude -p --output-format json` invocation."""
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        data = None
    if not isinstance(data, dict):
        message = (stderr.strip() or stdout.strip() or f"exit status {returncode}")[-2000:]
        return classify_failure(message)
    if data.get("is_error") or data.get("subtype") != "success" or returncode != 0:
        message = str(data.get("result") or data.get("subtype") or stderr.strip() or f"exit status {returncode}")
        return classify_failure(message[-2000:], data.get("api_error_status"))
    usage = data.get("usage") or {}
    input_tokens = sum(
        int(usage.get(name) or 0)
        for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    served = ",".join(sorted((data.get("modelUsage") or {}).keys()))
    return CliOutcome(
        "ok",
        text=str(data.get("result") or ""),
        input_tokens=input_tokens,
        output_tokens=int(usage.get("output_tokens") or 0),
        served_model=served,
    )


_UNESCAPED_QUOTE = re.compile(r'(?<!\\)"')


def _json_body(text: str) -> str:
    """The text from the first "{" to the last "}" (drops fences and stray prose)."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else text


def _repair_string(raw: str) -> str | None:
    if len(raw) < 2 or not (raw.startswith('"') and raw.endswith('"')):
        return None
    inner = _UNESCAPED_QUOTE.sub('\\"', raw[1:-1])
    try:
        value = json.loads(f'"{inner}"', strict=False)
    except ValueError:
        return None
    return value if isinstance(value, str) else None


def _repair_value(raw: str, spec: dict):
    try:
        value = json.loads(raw, strict=False)
    except ValueError:
        value = None
        if spec["type"] == "string":
            value = _repair_string(raw)
        elif spec["type"] == "array" and raw.startswith("[") and raw.endswith("]"):
            inner = raw[1:-1].strip()
            items = [_repair_string(f'"{item}"') for item in re.split(r'"\s*,\s*"', inner[1:-1])] if inner else []
            value = None if None in items else items
    if spec["type"] == "string" and isinstance(value, str) and "enum" in spec:
        value = value.strip()
        return value if value in spec["enum"] else None
    expected = {"string": str, "array": list, "object": dict, "boolean": bool}[spec["type"]]
    return value if isinstance(value, expected) else None


def _repair_by_keys(body: str, schema: dict) -> dict | None:
    """Cut each field's value between the known keys, in the prompt's order."""
    properties = schema["properties"]
    spans = []
    cursor = 0
    for key in properties:
        match = re.compile(r'"%s"\s*:' % re.escape(key)).search(body, cursor)
        if match is None:
            return None
        spans.append((key, match.start(), match.end()))
        cursor = match.end()
    result = {}
    for index, (key, _start, value_start) in enumerate(spans):
        value_end = spans[index + 1][1] if index + 1 < len(spans) else body.rfind("}")
        raw = body[value_start:value_end].strip().rstrip(",").strip()
        value = _repair_value(raw, properties[key])
        if value is None:
            return None
        result[key] = value
    return result


def normalize_reply(role: str, text: str) -> str:
    """Return a role's reply as valid JSON when it is malformed but recoverable.

    A reply that already parses (after stripping a Markdown fence, as the
    callers do) is returned unchanged, so well-formed replies are handled
    exactly as in the paper's pipeline.  Unrecoverable replies are also
    returned unchanged and fail the callers' parse as before.
    """
    schema = ROLE_SCHEMAS.get(role)
    if schema is None:
        return text
    body = _json_body(text)
    try:
        json.loads(re.sub(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", r"\1", text, flags=re.DOTALL))
        return text
    except ValueError:
        pass
    try:
        repaired = json.loads(body, strict=False)
    except ValueError:
        repaired = _repair_by_keys(body, schema)
    if not isinstance(repaired, dict):
        return text
    return json.dumps(repaired, ensure_ascii=False)


def _pause(outcome: CliOutcome) -> None:
    global _paused_until
    now = time.time()
    if outcome.kind == "quota":
        resume_at = outcome.resume_at or now + config.CLAUDE_CLI_LIMIT_WAIT
    else:
        resume_at = now + config.CLAUDE_CLI_RATE_WAIT
    if resume_at <= _paused_until:
        return
    _paused_until = resume_at
    label = "Claude usage limit reached" if outcome.kind == "quota" else "Claude rate limit or overload"
    logger.warning(
        "%s (%s); pausing until %s, then continuing by itself. Interrupting (Ctrl+C) is "
        "also safe: rerun the same command and completed calls replay from the ledger.",
        label,
        outcome.text.strip()[:200],
        datetime.fromtimestamp(resume_at).strftime("%Y-%m-%d %H:%M:%S"),
    )


async def _wait_while_paused() -> None:
    while (remaining := _paused_until - time.time()) > 0:
        await asyncio.sleep(min(remaining, 60))


async def _run_once(model: str, prompt: str) -> CliOutcome:
    proc = await asyncio.create_subprocess_exec(
        *claude_cli_command(model),
        cwd=_empty_workdir(),
        env=_child_env(),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")),
            timeout=config.CLAUDE_CLI_TIMEOUT,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return parse_cli_output(
        proc.returncode,
        stdout.decode("utf-8", errors="replace"),
        stderr.decode("utf-8", errors="replace"),
    )


async def claude_cli_chat(model: str, prompt: str, semaphore: asyncio.Semaphore) -> CliOutcome:
    """Return a successful outcome, pausing through usage and rate limits."""
    global _backend_checked
    if not _backend_checked:
        try:
            config.require_live_backend(model)
        except RuntimeError as exc:
            raise SystemExit(f"ERROR: {exc}") from exc
        _backend_checked = True
    while True:
        await _wait_while_paused()
        async with semaphore:
            outcome = await _run_once(model, prompt)
        if outcome.kind == "ok":
            _served_models[outcome.served_model] = _served_models.get(outcome.served_model, 0) + 1
            return outcome
        if outcome.kind in ("quota", "rate"):
            _pause(outcome)
            continue
        if outcome.kind == "auth":
            # SystemExit is not an Exception, so neither the retry policy nor
            # the callers' error handling turns it into ties or rejections.
            raise SystemExit(
                f"Claude CLI authentication failed: {outcome.text.strip()[:300]}\n"
                "Run `claude` once and log in with your subscription, then rerun this command."
            )
        raise ClaudeCliError(outcome.text.strip()[:500] or "Claude CLI call failed")
