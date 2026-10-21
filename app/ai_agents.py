"""AI agents that can investigate plugins: Codex (OpenAI), Claude Code (Anthropic), Antigravity CLI
and Gemini CLI (Google). Freebuff is listed but can't be used yet (no non-interactive mode).

Every agent is a command-line tool run non-interactively, the same way: one prompt in, one JSON
answer out that must match a schema. The prompts themselves live in app/codex_research.py and are
identical for every agent. Only the command line differs here.

  Codex        `codex exec`, prompt on stdin, --output-schema (the CLI enforces the schema).
  Claude       `claude -p`, prompt on stdin, --json-schema (enforced), subscription only.
  Antigravity  `agy --print=<prompt> --output-format json --json-schema <file>` (enforced; the prompt
               has to be an argument: agy doesn't read it from stdin).
  Gemini CLI   `gemini -p ... -o json`, prompt on stdin. No schema flag: the schema is written into
               the prompt, the answer is parsed out of the text, checked here, and repaired once.
Whatever an agent returns goes through `conform()` (the schema check), so a loose answer is either
fixed up or rejected with a clear reason, never stored half-shaped.

Billing: research must run on what the owner already signed in with (a subscription, a free
account), never silently on a pay-per-use API key. Claude and Antigravity get API-key variables
stripped from their environment. Gemini CLI can only be used with an API key nowadays (Google
stopped serving its free Google sign-in on 2026-06-18), so it only runs after the owner allows it
in Configuration > AI (`<id>_allow_billed`).

Model lists come from the vendor itself where the tool can list them (`codex debug models`,
`agy models`, Anthropic's models API with a key); otherwise a fixed, documented list with a note.
Never free text.

Nothing here applies anything. Each call is research only: read-only sandbox for Codex, web search
and fetch only for Claude and Gemini CLI (a policy file denies every other tool), and Antigravity's
print mode denies commands and runs in its terminal sandbox, in an empty temporary folder.
"""

import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

CALL_TIMEOUT_SECONDS = 240
INSTALL_TIMEOUT_SECONDS = 900
MODELS_TTL_SECONDS = 600
STATUS_TTL_SECONDS = 15
AGY_TIMEOUT_SECONDS = 420  # Antigravity reads pages slowly; a picture search took ~4-6 minutes
MAX_ARG_PROMPT = 30000      # Windows command lines stop at 32767 characters


class ResearchError(Exception):
    pass


class ResearchCancelled(ResearchError):
    """The owner pressed Stop while this call was running."""


@dataclass(frozen=True)
class AgentConfig:
    agent: str
    model: str
    effort: str | None
    allow_billed: bool = False   # the owner allowed an agent that bills an API key (Gemini CLI)


# The CLIs are console programs: never let one open a console window (the backend has none to share).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------- cancelling ----------

_active: set = set()
_active_lock = threading.Lock()
_cancelled = False


def begin_batch() -> None:
    """Clears a previous Stop so new calls can start."""
    global _cancelled
    _cancelled = False


def _kill_tree(proc) -> None:
    """Kills a CLI and everything it started (Gemini CLI is node behind a .cmd; agy starts a
    language server)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           creationflags=_NO_WINDOW, timeout=20)
        else:
            proc.kill()
    except (OSError, subprocess.TimeoutExpired):
        pass


def cancel_running() -> None:
    """Stop button: kills every call in flight (and its child processes), so no more tokens are spent."""
    global _cancelled
    _cancelled = True
    with _active_lock:
        procs = list(_active)
    for proc in procs:
        _kill_tree(proc)


def is_cancelled() -> bool:
    return _cancelled


def _run_cancellable(cmd: list, prompt: str | None, env: dict | None = None, cwd: str | None = None,
                     timeout: int = CALL_TIMEOUT_SECONDS, watch=None):
    """Runs a CLI to completion. The process is registered so cancel_running() can kill it.
    `prompt` goes to stdin (None: stdin is closed at once). `watch(line)` sees every stderr line as
    it arrives and may return a message: the call is then stopped and fails with that message (used
    to stop Gemini CLI's minutes-long retry loops on a quota or overload error)."""
    if _cancelled:
        raise ResearchCancelled("Stopped.")
    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        # Pinned to UTF-8 in both directions. The prompt asks for Spanish text, and the CLIs
        # reject non-UTF-8 stdin on a machine with a locale-default encoding.
        encoding="utf-8", errors="replace", text=True, env=env, cwd=cwd, creationflags=_NO_WINDOW,
    )
    with _active_lock:
        _active.add(proc)
    out, err, abort = [], [], []

    def read_out():
        out.append(proc.stdout.read())

    def read_err():
        for line in proc.stderr:
            err.append(line)
            if watch and not abort:
                msg = watch(line)
                if msg:
                    abort.append(msg)
                    _kill_tree(proc)

    def write_in():
        try:
            proc.stdin.write(prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            pass

    threads = [threading.Thread(target=read_out, daemon=True), threading.Thread(target=read_err, daemon=True)]
    if prompt is not None:
        threads.append(threading.Thread(target=write_in, daemon=True))
    for t in threads:
        t.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        for t in threads:
            t.join(timeout=10)
        with _active_lock:
            _active.discard(proc)
    if _cancelled:
        raise ResearchCancelled("Stopped.")
    if abort:
        raise ResearchError(abort[0])
    return subprocess.CompletedProcess(cmd, proc.returncode, "".join(out), "".join(err))


# ---------- API keys (presence in the process, user or system environment) ----------

def _registry_env(name: str) -> str | None:
    """A Windows user or system environment variable, for an app started before the variable was set."""
    if os.name != "nt":
        return None
    try:
        import winreg
    except ImportError:
        return None
    places = (
        (winreg.HKEY_CURRENT_USER, "Environment"),
        (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
    )
    for root, sub in places:
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _ = winreg.QueryValueEx(key, name)
        except OSError:
            continue
        if value:
            return str(value)
    return None


def api_key(name: str) -> str | None:
    return os.environ.get(name) or _registry_env(name) or None


def _env_without(*names: str) -> dict:
    return {k: v for k, v in os.environ.items() if k.upper() not in names}


# Google variables that would switch a Google CLI from the signed-in account to an API key or a
# Cloud project (billed per use).
_GOOGLE_BILLED_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
                       "GOOGLE_APPLICATION_CREDENTIALS", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")

# ---------- small helpers ----------

def _quick(cmd: list, timeout: int = 20) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None


def npm_available() -> bool:
    return shutil.which("npm") is not None


def _json_in(text: str):
    """The JSON object in `text` (a CLI may print log lines around it)."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ResearchError("The AI's output wasn't valid JSON.")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ResearchError(f"The AI's output wasn't valid JSON: {exc}")


def extract_json(text: str):
    """The answer object in a model's free text: the whole text, a ```json block, or the last
    complete {...} in it. Returns None when there is none."""
    text = (text or "").strip().lstrip("﻿")
    if not text:
        return None
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    if "```" in text:
        for block in text.split("```")[1::2]:
            block = block.strip()
            if block.lower().startswith("json"):
                block = block[4:]
            try:
                value = json.loads(block.strip())
                if isinstance(value, dict):
                    return value
            except ValueError:
                continue
    decoder = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i >= 0:
        try:
            value, end = decoder.raw_decode(text, i)
            if isinstance(value, dict):
                found = value
                i = text.find("{", end)
                continue
        except ValueError:
            pass
        i = text.find("{", i + 1)
    return found


# ---------- the schema check (every agent's answer goes through it) ----------

_TYPES = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "array": list,
          "object": dict, "null": type(None)}


def _types_of(schema: dict) -> list:
    t = schema.get("type")
    return t if isinstance(t, list) else ([t] if t else [])


def _matches(value, types: list) -> bool:
    for t in types:
        py = _TYPES.get(t)
        if py is None:
            continue
        if t in ("integer", "number") and isinstance(value, bool):
            continue
        if isinstance(value, py):
            return True
    return not types


def conform(value, schema: dict, path: str = "answer") -> tuple:
    """Fits an answer to its schema: unknown keys are dropped, a missing key that may be null or a
    list becomes null / [], and a string where a list of strings is wanted becomes a one-item list.
    Returns (value, problems); problems are what couldn't be fixed (wrong types, values outside an
    enum, missing required text)."""
    problems = []
    types = _types_of(schema)
    if value is None and "null" in types:
        return None, problems
    if "object" in types and isinstance(value, dict):
        props = schema.get("properties", {})
        out = {}
        for key, sub in props.items():
            if key in value:
                out[key], sub_problems = conform(value[key], sub, f"{path}.{key}")
                problems += sub_problems
            elif key in schema.get("required", []):
                sub_types = _types_of(sub)
                if "null" in sub_types:
                    out[key] = None
                elif "array" in sub_types:
                    out[key] = []
                else:
                    problems.append(f"{path}.{key} is missing")
        if schema.get("additionalProperties", True) is not False:
            out.update({k: v for k, v in value.items() if k not in props})
        return out, problems
    if "array" in types:
        if isinstance(value, str) and _types_of(schema.get("items", {})) == ["string"]:
            value = [value]
        if isinstance(value, list):
            out = []
            for i, item in enumerate(value):
                fixed, sub_problems = conform(item, schema.get("items", {}), f"{path}[{i}]")
                problems += sub_problems
                out.append(fixed)
            return out, problems
    if not _matches(value, types):
        problems.append(f"{path} should be {' or '.join(types)}, got {type(value).__name__}")
        return value, problems
    enum = schema.get("enum")
    if enum is not None and value not in enum:
        if isinstance(value, str) and value.lower() in enum:
            return value.lower(), problems
        problems.append(f"{path} must be one of {', '.join(json.dumps(e) for e in enum)}, got {json.dumps(value)}")
    return value, problems


def schema_instructions(schema: dict) -> str:
    """For agents without a schema flag: the answer format, written into the prompt."""
    return ("Answer with ONLY one JSON object: no prose before or after it, no code fences. It must match "
            "this JSON Schema exactly: every required key present, no other keys, null or [] where you found "
            "nothing, enum values spelled exactly as listed.\n" + json.dumps(schema, ensure_ascii=False))


def _repair_prompt(schema: dict, previous: str, problems: list) -> str:
    return ("Your previous answer didn't match the required JSON format.\n"
            + ("Problems: " + "; ".join(problems[:12]) + "\n" if problems else "No JSON object was found in it.\n")
            + "Previous answer:\n" + (previous or "")[:6000] + "\n\n"
            "Rewrite it as the corrected JSON object. Keep the same findings, don't search again, don't add anything "
            "you didn't find. " + schema_instructions(schema))


# ---------- agents ----------

class Agent:
    id = ""
    label = ""
    binary = ""
    npm_package = ""
    install_url = ""            # the tool's official install page (shown in Configuration > AI)
    install_hint = ""           # how to install when VEXT can't do it (no npm package)
    blurb = ""                  # one line about the tool
    default_model = ""
    default_effort = "low"
    efforts: tuple = ("low", "medium", "high")
    model_key = ""
    effort_key = ""
    supported = True            # False: listed, but VEXT can't run it (unsupported_reason says why)
    unsupported_reason = ""
    recommended = False
    native_schema = True        # the CLI enforces the JSON schema itself
    billed_note = ""            # set when the agent's current sign-in bills per use (see billing())

    @property
    def consent_key(self) -> str:
        return f"{self.id}_allow_billed"

    def path(self) -> str | None:
        return shutil.which(self.binary)

    def installed(self) -> bool:
        return self.path() is not None

    def version(self) -> str | None:
        if not self.installed():
            return None
        proc = _quick([self.path(), "--version"])
        if proc is None or proc.returncode != 0:
            return None
        return (proc.stdout or proc.stderr or "").strip().splitlines()[0] if (proc.stdout or proc.stderr or "").strip() else None

    def signed_in(self) -> tuple[bool, str]:
        raise NotImplementedError

    def billing(self) -> str | None:
        """A plain-language warning when this agent, as signed in now, would bill per use. None when
        it runs on a subscription or a free account."""
        return None

    def signin_command(self) -> str:
        raise NotImplementedError

    def models(self) -> dict:
        """{models: [{id, label, description, efforts, default_effort}], source, note}"""
        raise NotImplementedError

    def run(self, prompt: str, schema: dict, model: str, effort: str | None) -> dict:
        raise NotImplementedError

    def install(self) -> str:
        """Installs the CLI globally with npm. Only called from an explicit click in the AI tab."""
        if not self.npm_package:
            raise ResearchError(f"{self.label} isn't installed with npm. {self.install_hint or ''} "
                                f"See {self.install_url}".strip())
        if not npm_available():
            raise ResearchError("npm isn't installed. Install Node.js from nodejs.org first, then try again.")
        try:
            proc = subprocess.run(
                ["npm", "install", "-g", self.npm_package],
                capture_output=True, encoding="utf-8", errors="replace", timeout=INSTALL_TIMEOUT_SECONDS, shell=True,
                creationflags=_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            raise ResearchError("The install timed out. Check your connection and try again.")
        except OSError as exc:
            raise ResearchError(f"Could not run npm: {exc}")
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        if proc.returncode != 0:
            raise ResearchError(f"npm couldn't install {self.label}: " + output[-500:])
        return output[-500:]


class CodexAgent(Agent):
    id = "codex"
    label = "Codex"
    binary = "codex"
    npm_package = "@openai/codex"
    install_url = "https://developers.openai.com/codex/cli"
    blurb = "OpenAI's coding agent, on your ChatGPT plan."
    default_model = "gpt-5.6-luna"
    default_effort = "low"
    model_key = "codex_model"
    effort_key = "codex_effort"

    def signed_in(self):
        proc = _quick([self.path(), "login", "status"]) if self.installed() else None
        if proc is None:
            return False, "Couldn't check the sign-in."
        text = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, text.splitlines()[0] if text else ("Signed in" if proc.returncode == 0 else "Not signed in")

    def signin_command(self):
        return "codex login"

    def models(self):
        proc = _quick([self.path(), "debug", "models"], timeout=45) if self.installed() else None
        models = []
        if proc is not None and proc.returncode == 0:
            try:
                data = json.loads(proc.stdout)
                entries = data["models"] if isinstance(data, dict) else data
                listed = [m for m in entries if m.get("visibility") == "list"] or entries
                for m in sorted(listed, key=lambda m: m.get("priority", 99)):
                    levels = [lv.get("effort") for lv in m.get("supported_reasoning_levels", []) if lv.get("effort")]
                    models.append({
                        "id": m["slug"], "label": m.get("display_name") or m["slug"],
                        "description": m.get("description") or "",
                        "efforts": levels or list(self.efforts),
                        "default_effort": m.get("default_reasoning_level") or self.default_effort,
                    })
            except (ValueError, KeyError, TypeError):
                models = []
        if models:
            return {"models": models, "source": "Codex's own model catalog for this account (codex debug models).", "note": ""}
        return {
            "models": [{"id": self.default_model, "label": self.default_model, "description": "",
                        "efforts": list(self.efforts), "default_effort": self.default_effort}],
            "source": "Built-in", "note": "Couldn't read Codex's model catalog. Check that Codex is installed and signed in.",
        }

    def run(self, prompt, schema, model, effort):
        with tempfile.TemporaryDirectory(prefix="vext_codex_") as tmp:
            schema_path = Path(tmp) / "schema.json"
            output_path = Path(tmp) / "result.json"
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            cmd = [self.path(), "exec", "-m", model]
            if effort:
                cmd += ["-c", f"model_reasoning_effort={effort}"]
            cmd += ["-s", "read-only", "--skip-git-repo-check",
                    "--output-schema", str(schema_path), "-o", str(output_path), "-"]
            try:
                proc = _run_cancellable(cmd, prompt)
            except subprocess.TimeoutExpired:
                raise ResearchError(f"Codex research timed out after {CALL_TIMEOUT_SECONDS}s.")
            except OSError as exc:
                raise ResearchError(f"Could not run codex: {exc}")
            if proc.returncode != 0:
                text = (proc.stderr or proc.stdout or "").strip()
                # Codex echoes the prompt and hook lines before its own "ERROR: ..." line (a usage
                # limit, a sign-in problem): that line is the useful part.
                errors = [ln[6:].strip() for ln in text.splitlines() if ln.startswith("ERROR:")]
                tail = errors[-1] if errors else text[-500:]
                raise ResearchError(f"Codex exited with an error: {tail or 'no output'}")
            if not output_path.exists():
                raise ResearchError("Codex finished but produced no result file.")
            raw = output_path.read_bytes()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                # Codex writes this file in the Windows code page on some machines. Accents would
                # otherwise come back as replacement characters.
                text = raw.decode("cp1252", errors="replace")
            try:
                return json.loads(text.lstrip("﻿"))
            except json.JSONDecodeError as exc:
                raise ResearchError(f"Codex's output wasn't valid JSON: {exc}")


_CLAUDE_SYSTEM_PROMPT = ("You are a careful research assistant for a personal audio-plugin catalog. "
                         "Use web search when you need to. Never guess. Return only the structured result.")

# Claude Code's own aliases: the CLI resolves each to the newest model of that family.
_CLAUDE_ALIASES = [
    ("haiku", "Haiku (latest)", "Fastest and cheapest. Good for classification and lookups."),
    ("sonnet", "Sonnet (latest)", "Balanced speed and quality."),
    ("opus", "Opus (latest)", "Most capable of the Opus line."),
    ("fable", "Fable (latest)", "Most capable for the hardest tasks."),
]


class ClaudeAgent(Agent):
    id = "claude"
    label = "Claude"
    binary = "claude"
    npm_package = "@anthropic-ai/claude-code"
    install_url = "https://code.claude.com/docs/en/setup"
    blurb = "Anthropic's Claude Code, on your Claude subscription."
    default_model = "haiku"
    default_effort = "low"
    # The levels `claude --effort` documents. Not every model supports every level.
    efforts = ("low", "medium", "high", "xhigh", "max")
    model_key = "claude_model"
    effort_key = "claude_effort"

    def signed_in(self):
        proc = _quick([self.path(), "auth", "status"]) if self.installed() else None
        if proc is None:
            return False, "Couldn't check the sign-in."
        try:
            data = _json_in(proc.stdout or "")
        except ResearchError:
            return False, "Couldn't read the sign-in status."
        if not data.get("loggedIn"):
            return False, "Not signed in."
        method = data.get("authMethod") or ""
        return True, "Signed in with a Claude subscription." if method == "claude.ai" else f"Signed in ({method or 'unknown method'})."

    def signin_command(self):
        return "claude auth login"

    def _api_models(self, key: str) -> list:
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/models?limit=1000",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [
            {"id": m["id"], "label": m.get("display_name") or m["id"], "description": "",
             "efforts": list(self.efforts), "default_effort": self.default_effort}
            for m in data.get("data", []) if m.get("id")
        ]

    def models(self):
        key = api_key("ANTHROPIC_API_KEY")
        note = ""
        if key:
            try:
                models = self._api_models(key)
                if models:
                    return {"models": models, "source": "Anthropic's official models API (key found in your environment variables).", "note": ""}
                note = "Anthropic's models API returned no models, so the built-in list is shown."
            except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
                note = f"Couldn't read Anthropic's models API ({exc}), so the built-in list is shown."
        else:
            note = ("A Claude subscription can't list models. Set ANTHROPIC_API_KEY in your user or system "
                    "environment variables to load Anthropic's live list. Using Claude Code's own aliases.")
        models = [{"id": i, "label": label, "description": desc, "efforts": list(self.efforts),
                   "default_effort": self.default_effort} for i, label, desc in _CLAUDE_ALIASES]
        return {"models": models, "source": "Built-in list of Claude Code's model aliases.", "note": note}

    def run(self, prompt, schema, model, effort):
        cmd = [self.path(), "-p", "--output-format", "json", "--json-schema", json.dumps(schema),
               "--model", model, "--no-session-persistence",
               "--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch,WebFetch",
               "--permission-mode", "dontAsk", "--system-prompt", _CLAUDE_SYSTEM_PROMPT,
               "--setting-sources", ""]
        if effort:
            cmd += ["--effort", effort]
        # Research runs on the signed-in subscription: an API key in the environment would
        # silently switch it to pay-per-token billing.
        env = _env_without("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
        with tempfile.TemporaryDirectory(prefix="vext_claude_") as tmp:
            try:
                proc = _run_cancellable(cmd, prompt, env=env, cwd=tmp)
            except subprocess.TimeoutExpired:
                raise ResearchError(f"Claude research timed out after {CALL_TIMEOUT_SECONDS}s.")
            except OSError as exc:
                raise ResearchError(f"Could not run claude: {exc}")
        out = (proc.stdout or "").strip()
        if proc.returncode != 0 and not out:
            tail = (proc.stderr or "").strip()[-500:]
            raise ResearchError(f"Claude exited with an error: {tail or 'no output'}")
        data = _json_in(out)
        if data.get("is_error"):
            raise ResearchError("Claude reported an error: " + str(data.get("result") or data.get("subtype") or "unknown")[:400])
        result = data.get("structured_output")
        if result is None:
            try:
                result = json.loads(data.get("result") or "")
            except (TypeError, ValueError):
                raise ResearchError("Claude finished but returned no structured result.")
        return result


class AntigravityAgent(Agent):
    """Google Antigravity's CLI (`agy`), a native binary installed by Google's own installer, signed
    in with a Google account (the free individual quota, or a Google AI Pro / Ultra plan). Its print
    mode takes a JSON schema and returns `structured_output`."""
    id = "antigravity"
    label = "Antigravity"
    binary = "agy"
    install_url = "https://antigravity.google/docs/cli"
    install_hint = ("Install the Antigravity CLI with Google's installer (PowerShell: "
                    "irm https://antigravity.google/cli/install.ps1 | iex), then click Check again.")
    blurb = "Google's Antigravity CLI, on your Google account (Gemini, plus Claude and GPT-OSS models)."
    default_model = "gemini-3.8-flash-low"
    default_effort = ""
    efforts = ()                # the reasoning level is part of the model's name (…-low / -medium / -high)
    model_key = "antigravity_model"
    effort_key = "antigravity_effort"

    def path(self):
        found = shutil.which(self.binary)
        if found:
            return found
        # Google's installer puts it here and edits the user PATH, which an app started before the
        # install doesn't see yet.
        local = os.environ.get("LOCALAPPDATA")
        for sub in (("agy", "bin", "agy.exe"), ("Antigravity", "agy.exe")):
            p = Path(local, *sub) if local else None
            if p and p.is_file():
                return str(p)
        return None

    def _list_models(self):
        proc = _quick([self.path(), "models"], timeout=40) if self.installed() else None
        if proc is None:
            return None, "Couldn't run agy."
        models = []
        for line in (proc.stdout or "").splitlines():
            if "\t" in line:
                mid, label = line.split("\t", 1)
                if mid.strip():
                    models.append((mid.strip(), label.strip() or mid.strip()))
        if proc.returncode == 0 and models:
            return models, ""
        text = ((proc.stderr or "") + (proc.stdout or "")).strip()
        return None, text.splitlines()[-1][:200] if text else "agy returned no models."

    def signed_in(self):
        models, why = self._list_models()
        if models:
            return True, "Signed in with a Google account."
        return False, f"Not signed in ({why})" if why else "Not signed in."

    def signin_command(self):
        # agy has no login command: its first interactive start opens Google's sign-in in the browser.
        return f'"{self.path()}"' if self.path() else self.binary

    def models(self):
        models, why = self._list_models()
        if models:
            def describe(mid):
                if mid.endswith("-low"):
                    return "Low reasoning: fastest and lightest."
                if mid.endswith("-high"):
                    return "High reasoning: slower, more thorough."
                return ""
            return {"models": [{"id": m, "label": label, "description": describe(m), "efforts": [],
                                "default_effort": ""} for m, label in models],
                    "source": "Antigravity's own model list for this account (agy models); the reasoning level is part of each name.",
                    "note": ""}
        return {"models": [{"id": self.default_model, "label": "Gemini 3.8 Flash (Low)", "description": "",
                            "efforts": [], "default_effort": ""}],
                "source": "Built-in", "note": f"Couldn't read Antigravity's model list: {why}"}

    def _call(self, prompt, schema_path, model, effort, tmp):
        work = Path(tmp, "work")
        work.mkdir(exist_ok=True)
        cmd = [self.path(), f"--print={prompt}", "--output-format", "json", "--json-schema", str(schema_path),
               "--model", model, "--sandbox", "--disable-slash-commands",
               "--print-timeout", f"{AGY_TIMEOUT_SECONDS - 30}s"]
        if effort:
            cmd += ["--effort", effort]
        env = _env_without(*_GOOGLE_BILLED_VARS)
        # agy reads its permission rules from <home>/.gemini/antigravity-cli/settings.json, and print
        # mode denies (and ends the turn on) anything that would need a prompt, reading a web page
        # included. VEXT's own home for it allows reading pages and denies commands, edits and MCP,
        # without touching the owner's own agy settings or history. The sign-in is in Windows'
        # credential store, so it is the same account.
        home = _agy_home()
        env["USERPROFILE"] = env["HOME"] = home
        try:
            proc = _run_cancellable(cmd, None, env=env, cwd=str(work), timeout=AGY_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            raise ResearchError(f"Antigravity research timed out after {AGY_TIMEOUT_SECONDS}s.")
        except OSError as exc:
            raise ResearchError(f"Could not run agy: {exc}")
        out = (proc.stdout or "").strip()
        data = extract_json(out)
        if not data:
            tail = ((proc.stderr or "") + out).strip()[-500:]
            raise ResearchError(f"Antigravity exited with an error: {tail or 'no output'}")
        if data.get("status") not in (None, "SUCCESS"):
            raise ResearchError("Antigravity reported an error: " + str(data.get("error") or data.get("response") or data.get("status"))[:400])
        result = data.get("structured_output")
        if not isinstance(result, dict):
            result = extract_json(data.get("response") or "")
        return result if isinstance(result, dict) else None, bool(data.get("denied_actions"))

    def run(self, prompt, schema, model, effort):
        prompt = _AGY_PREFACE + prompt
        if len(prompt) > MAX_ARG_PROMPT:
            raise ResearchError("The prompt is too long for Antigravity (it takes the prompt on the command line).")
        with tempfile.TemporaryDirectory(prefix="vext_agy_") as tmp:
            schema_path = Path(tmp, "schema.json")
            schema_path.write_text(json.dumps(schema), encoding="utf-8")
            result, denied = self._call(prompt, schema_path, model, effort, tmp)
            if result is None and denied:
                # Print mode denies commands, and a turn whose command was denied can end with no
                # answer at all. Once more, told plainly.
                result, denied = self._call(
                    _AGY_PREFACE + "Your previous attempt tried to run a command, which is denied here, and "
                    "ended without an answer. Use only web search and reading web pages this time.\n\n"
                    + prompt[len(_AGY_PREFACE):], schema_path, model, effort, tmp)
        if result is None:
            raise ResearchError("Antigravity finished but returned no structured result"
                                + (" (it tried to run a command, which VEXT doesn't allow)." if denied else "."))
        return result


_AGY_SETTINGS = {
    "permissions": {
        "allow": ["read_url(*)"],
        "deny": ["command(*)", "mcp(*)", "write_to_file(*)", "edit(*)", "edit_file(*)", "delete_file(*)"],
    },
}


def _agy_home() -> str:
    from app.config import PROJECT_ROOT
    home = Path(PROJECT_ROOT) / "cache" / "agy-home"
    settings = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_AGY_SETTINGS, indent=2)
    try:
        current = settings.read_text(encoding="utf-8")
    except OSError:
        current = None
    if current != text:
        settings.write_text(text, encoding="utf-8")
    return str(home)


_AGY_PREFACE = ("Tools: use only web search and reading web pages (URL fetch). Running commands, scripts or a shell "
                "(curl, PowerShell, Python) and editing files are not allowed here and will fail, so don't try.\n\n")


# Gemini CLI can't list models. These are the Gemini models Gemini CLI 0.6x itself accepts (its
# VALID_GEMINI_MODELS set); which ones a key can use depends on the key's tier.
_GEMINI_MODELS = [
    ("gemini-3.5-flash-lite", "Gemini 3.5 Flash-Lite",
     "Fastest and lightest. The one that answered on a free-tier key in testing."),
    ("gemini-3.8-flash", "Gemini 3.8 Flash", "Newest Flash. Often busy (503) for free-tier keys."),
    ("gemini-3.5-flash", "Gemini 3.5 Flash", "Balanced."),
    ("gemini-3.1-pro-preview", "Gemini 3.1 Pro (preview)", "Most capable. Slower; usually paid tier only."),
    ("gemini-2.5-pro", "Gemini 2.5 Pro", "Gemini CLI's own default. Slower; often busy for free-tier keys."),
]

# Only web search and web fetch may run; everything else (shell, file edits, MCP tools) is denied.
_GEMINI_POLICY = """[[rule]]
toolName = ["google_web_search", "web_fetch"]
decision = "allow"
priority = 990

[[rule]]
toolName = "*"
decision = "deny"
priority = 980
"""


class GeminiAgent(Agent):
    """Google's Gemini CLI (npm @google/gemini-cli). Since 2026-06-18 Google serves it only to paid
    Code Assist licences and API keys (free Google sign-in users were moved to Antigravity), so in
    practice it runs on a Gemini API key, which can bill. It has no schema flag: the schema goes into
    the prompt and the answer is checked and repaired once."""
    id = "gemini"
    label = "Gemini CLI"
    binary = "gemini"
    npm_package = "@google/gemini-cli"
    install_url = "https://geminicli.com/docs/get-started/installation/"
    blurb = "Google's Gemini CLI. Today it mostly runs on a Gemini API key (Antigravity replaced its free sign-in)."
    default_model = "gemini-3.5-flash-lite"
    default_effort = ""
    efforts = ()                # Gemini CLI has no effort or thinking flag
    model_key = "gemini_model"
    effort_key = "gemini_effort"
    native_schema = False

    def _command(self) -> list | None:
        """node + the CLI's script, so no cmd.exe sits between VEXT and Gemini CLI (cmd.exe would
        re-parse the arguments). Falls back to the npm shim."""
        shim = self.path()
        if not shim:
            return None
        script = Path(shim).parent / "node_modules" / "@google" / "gemini-cli" / "bundle" / "gemini.js"
        node = shutil.which("node")
        if script.is_file() and node:
            return [node, "--no-warnings=DEP0040", str(script)]
        return [shim]

    def _auth_type(self) -> str | None:
        home = Path(os.environ.get("GEMINI_CLI_HOME") or Path.home())
        try:
            settings = json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
            chosen = ((settings.get("security") or {}).get("auth") or {}).get("selectedType")
        except (OSError, ValueError, AttributeError):
            chosen = None
        if chosen:
            return chosen
        if api_key("GEMINI_API_KEY"):
            return "gemini-api-key"
        if api_key("GOOGLE_GENAI_USE_VERTEXAI"):
            return "vertex-ai"
        if api_key("GOOGLE_GENAI_USE_GCA"):
            return "oauth-personal"
        return None

    def signed_in(self):
        auth = self._auth_type()
        if auth == "gemini-api-key":
            return True, "Uses a Gemini API key."
        if auth == "vertex-ai":
            return True, "Uses Vertex AI (a Google Cloud project)."
        if auth == "oauth-personal":
            return True, ("Signed in with Google. Google only serves this to Gemini Code Assist Standard or "
                          "Enterprise licences since June 2026; with a free account, use Antigravity.")
        if auth:
            return True, f"Signed in ({auth})."
        return False, "Not signed in. Start Gemini CLI once and choose how to sign in (/auth)."

    def billing(self):
        auth = self._auth_type()
        if auth == "gemini-api-key":
            return ("Gemini CLI uses your Gemini API key. Google charges the key's project when billing is on "
                    "for it; a key without billing uses the free tier (small daily quota).")
        if auth == "vertex-ai":
            return "Gemini CLI uses Vertex AI, which bills your Google Cloud project."
        return None

    def signin_command(self):
        return "gemini"

    def models(self):
        return {"models": [{"id": i, "label": label, "description": desc, "efforts": [], "default_effort": ""}
                           for i, label, desc in _GEMINI_MODELS],
                "source": "Built-in list: the Gemini models Gemini CLI itself accepts.",
                "note": "Gemini CLI can't list the models your key may use. If one answers 'busy' or 'not found', pick another."}

    @staticmethod
    def _watch():
        seen = {"503": 0}

        def watch(line: str):
            low = line.lower()
            if ("status 429" in line or "RESOURCE_EXHAUSTED" in line or "exceeded your current quota" in low
                    or "quota exceeded" in low):
                return ("Gemini's quota for this key is used up (429). Wait for it to reset (free-tier keys reset "
                        "daily) or use another agent.")
            if "status 503" in line:
                seen["503"] += 1
                if seen["503"] >= 2:
                    return ("Google says this model is busy (503, high demand). Pick a lighter model (Gemini 3.5 "
                            "Flash-Lite) in Configuration > AI, or try again later.")
            if "API key not valid" in line or "API_KEY_INVALID" in line:
                return "Google rejected the Gemini API key. Sign in to Gemini CLI again."
            if "status 404" in line and "models/" in line:
                return "This Gemini model isn't available to your key. Pick another one in Configuration > AI."
            return None
        return watch

    def _call(self, prompt: str, model: str, tmp: str, policy: str) -> str:
        """One headless Gemini CLI run. Returns the model's text answer."""
        cmd = self._command() + ["-m", model, "-o", "json", "--approval-mode", "default", "--policy", policy,
                                 "--skip-trust", "-p", "Do what the text above asks."]
        work = Path(tmp, "work")
        work.mkdir(exist_ok=True)
        try:
            proc = _run_cancellable(cmd, prompt, cwd=str(work), watch=self._watch())
        except subprocess.TimeoutExpired:
            raise ResearchError(f"Gemini CLI research timed out after {CALL_TIMEOUT_SECONDS}s.")
        except OSError as exc:
            raise ResearchError(f"Could not run gemini: {exc}")
        data = extract_json(proc.stdout or "")
        if not data:
            lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip() and not ln.startswith(" ")]
            raise ResearchError("Gemini CLI exited with an error: " + (" ".join(lines[-3:])[-500:] or "no output"))
        if data.get("error"):
            err = data["error"]
            raise ResearchError("Gemini CLI reported an error: " + str(err.get("message") if isinstance(err, dict) else err)[:400])
        return data.get("response") or ""

    def run(self, prompt, schema, model, effort):
        with tempfile.TemporaryDirectory(prefix="vext_gemini_") as tmp:
            policy = Path(tmp, "vext-policy.toml")
            policy.write_text(_GEMINI_POLICY, encoding="utf-8")
            text = self._call(prompt + "\n\n" + schema_instructions(schema), model, tmp, str(policy))
            answer = extract_json(text)
            problems = [] if answer is not None else ["no JSON object"]
            if answer is not None:
                answer, problems = conform(answer, schema)
            if problems:
                # One repair round: the same model rewrites its own answer in the right shape.
                text = self._call(_repair_prompt(schema, text, problems), model, tmp, str(policy))
                answer = extract_json(text)
                if answer is None:
                    raise ResearchError("Gemini CLI's answer wasn't valid JSON, even after asking it to fix the format.")
        return answer


class FreebuffAgent(Agent):
    """Freebuff (Codebuff's free, ad-supported CLI). Listed because it is free and has light, fast
    models, but VEXT can't use it: freebuff 0.2.x is an interactive terminal app only (its sole
    command is `login`; -p/--print/--json are rejected, a piped prompt waits for a terminal). The
    headless path Codebuff offers is its paid SDK with a paid API key. Tracked upstream in
    CodebuffAI/freebuff issue #947."""
    id = "freebuff"
    label = "Freebuff"
    binary = "freebuff"
    npm_package = "freebuff"
    install_url = "https://freebuff.com/"
    blurb = "Free (ad-supported), with light, fast models."
    default_model = ""
    default_effort = ""
    efforts = ()
    model_key = "freebuff_model"
    effort_key = "freebuff_effort"
    supported = False
    recommended = True
    unsupported_reason = ("Freebuff has no non-interactive mode yet: it only runs as an interactive terminal app, "
                          "so VEXT can't send it a question and read the answer. VEXT will use it once Freebuff "
                          "adds a print/headless mode (requested in its issue #947).")
    issue_url = "https://github.com/CodebuffAI/freebuff/issues/947"

    def signed_in(self):
        creds = Path.home() / ".config" / "manicode" / "credentials.json"
        try:
            if creds.stat().st_size > 2:
                return True, "Signed in to Freebuff."
        except OSError:
            pass
        return False, "Not signed in."

    def signin_command(self):
        return "freebuff login"

    def models(self):
        return {"models": [], "source": "", "note": ""}

    def run(self, prompt, schema, model, effort):
        raise ResearchError(self.unsupported_reason)


AGENTS: dict[str, Agent] = {a.id: a for a in (
    CodexAgent(), ClaudeAgent(), AntigravityAgent(), GeminiAgent(), FreebuffAgent())}


def get(agent_id: str) -> Agent:
    if agent_id not in AGENTS:
        raise ResearchError(f"Unknown AI agent: {agent_id}")
    return AGENTS[agent_id]


def run(cfg: AgentConfig, prompt: str, schema: dict) -> dict:
    """One research call: the agent's answer, checked against `schema` (see conform)."""
    agent = get(cfg.agent)
    if not agent.supported:
        raise ResearchError(f"{agent.label} can't be used by VEXT: {agent.unsupported_reason}")
    if not agent.installed():
        raise ResearchError(f'"{agent.binary}" CLI not found. Install it from Configuration > AI.')
    note = agent.billing()
    if note and not cfg.allow_billed:
        raise ResearchError(f"{note} VEXT only uses it after you allow that in Configuration > AI.")
    answer = agent.run(prompt, schema, cfg.model, cfg.effort or None)
    if not isinstance(answer, dict):
        raise ResearchError(f"{agent.label} returned no JSON object.")
    answer, problems = conform(answer, schema)
    if problems:
        raise ResearchError(f"{agent.label}'s answer didn't match the expected format: " + "; ".join(problems[:5]))
    return answer


# ---------- cached status and model lists ----------

_cache: dict = {}
_cache_lock = threading.Lock()


_refreshing: set = set()


def _cached(key, ttl, fn):
    """Fresh value within `ttl`. A stale value is returned at once and refreshed in the
    background (each check runs the agent's CLI, which takes seconds); only the first call,
    or one after forget(), waits for the CLI."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
        stale = hit is not None
        if stale and key not in _refreshing:
            _refreshing.add(key)
            threading.Thread(target=_refresh, args=(key, fn), daemon=True).start()
    if stale:
        return hit[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


def _refresh(key, fn) -> None:
    try:
        value = fn()
        with _cache_lock:
            _cache[key] = (time.time(), value)
    except Exception:
        pass
    finally:
        with _cache_lock:
            _refreshing.discard(key)


def warm() -> None:
    """Fills the status/model cache in the background (at startup), so the first
    'Which AI?' popup doesn't wait for every CLI."""
    def run():
        for agent_id in list(AGENTS):
            try:
                st = status_for(agent_id)
                if st["installed"] and get(agent_id).supported:
                    models_for(agent_id)
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


def forget(agent_id: str | None = None) -> None:
    """Drops cached status/models (after an install, a sign-in or a manual refresh)."""
    with _cache_lock:
        for key in list(_cache):
            if agent_id is None or key[1] == agent_id:
                del _cache[key]


def models_for(agent_id: str) -> dict:
    return _cached(("models", agent_id), MODELS_TTL_SECONDS, get(agent_id).models)


def status_for(agent_id: str) -> dict:
    agent = get(agent_id)

    def build():
        installed = agent.installed()
        version = agent.version() if installed else None
        signed, detail = agent.signed_in() if installed else (False, "")
        return {"installed": installed, "version": version, "signed_in": signed, "auth_detail": detail,
                "billing_note": agent.billing() if installed else None}

    return _cached(("status", agent_id), STATUS_TTL_SECONDS, build)


def config_for(conn, agent_id: str) -> AgentConfig:
    """The model and effort the owner picked for this agent (or its defaults)."""
    from app import db
    agent = get(agent_id)
    model = db.get_setting(conn, agent.model_key, agent.default_model)
    effort = db.get_setting(conn, agent.effort_key, agent.default_effort)
    if not agent.efforts:
        effort = ""
    allowed = db.get_setting(conn, agent.consent_key, "0") == "1"
    return AgentConfig(agent_id, model, effort or None, allowed)


def validate_choice(agent_id: str, model: str, effort: str | None) -> None:
    """Only a model the agent lists, and an effort that model supports, can be saved."""
    catalog = models_for(agent_id)["models"]
    match = next((m for m in catalog if m["id"] == model), None)
    if match is None:
        raise ResearchError("That model isn't in this agent's list.")
    if effort and effort not in match["efforts"]:
        raise ResearchError(f"{match['label']} doesn't support the effort level '{effort}'.")
