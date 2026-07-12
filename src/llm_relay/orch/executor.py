"""Subprocess execution wrapper for CLI tools -- stdlib only."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

from llm_relay.orch.models import CLIStatus, DelegationResult

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = int(os.environ.get("LLM_RELAY_ORCH_EXEC_TIMEOUT", "120"))

# Codex GitHub App token injection — when llm-relay's cli_delegate spawns
# Codex (cli.cli_id == "openai-codex"), we generate a fresh GitHub App
# installation token via a user-provided script and inject it as GH_TOKEN
# in the subprocess env. This lets Codex use a dedicated bot identity for
# `gh` operations (PR comments, label changes, branch pushes) instead of
# falling back to the operator's personal token.
#
# Tokens are GitHub-installation tokens that expire after 60 minutes; we
# cache for 50 minutes to leave a comfortable margin.
#
# Per-repo routing (docs/memos/2026-07-12-codex-per-repo-token-routing.md):
# When a config file at ~/.llm-relay/codex-gh-agents.json is present, the
# executor infers the target repo from `working_dir`'s git origin and picks
# a per-repo agent name from the mappings. Without the config, falls back
# to the LLM_RELAY_CODEX_GH_AGENT env var (default `codex-reviewer`).
#
# Disable by unsetting LLM_RELAY_CODEX_GH_TOKEN_SCRIPT (or pointing it at a
# non-existent path). Disabled by default — feature only activates when the
# script exists and is executable.
_CODEX_GH_TOKEN_SCRIPT = os.environ.get(
    "LLM_RELAY_CODEX_GH_TOKEN_SCRIPT",
    os.path.expanduser("~/.llm-relay/github-apps/generate-token.sh"),
)
_CODEX_GH_TOKEN_AGENT_DEFAULT = os.environ.get("LLM_RELAY_CODEX_GH_AGENT", "codex-reviewer")
_CODEX_GH_TOKEN_TTL_S = 3000  # 50 min cache; tokens themselves expire at 60
_codex_gh_token_cache: Dict[str, Tuple[str, float]] = {}  # {agent: (token, expiry)}

_CODEX_GH_AGENTS_CONFIG_PATH = os.path.expanduser(
    os.environ.get("LLM_RELAY_CODEX_GH_AGENTS_CONFIG", "~/.llm-relay/codex-gh-agents.json")
)


def _infer_repo_from_working_dir(working_dir: Optional[str]) -> Optional[str]:
    """Return `owner/name` inferred from working_dir's git origin, or None.

    Handles SSH (git@github.com:owner/name.git), HTTPS
    (https://github.com/owner/name.git), and github: (github:owner/name)
    URL shapes. Best-effort — returns None on any failure without raising.
    """
    if not working_dir or not os.path.isdir(working_dir):
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", working_dir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode != 0:
            return None
        url = proc.stdout.strip()
    except Exception:
        return None
    if not url:
        return None

    # Match owner/name from any of: git@host:owner/name, https://host/owner/name,
    # ssh://git@host/owner/name, github:owner/name.
    m = re.search(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$", url)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def _load_codex_gh_agents_config() -> Optional[dict]:
    """Load ~/.llm-relay/codex-gh-agents.json or return None on any failure.

    None means "no per-repo routing configured; use the env-var agent." That's
    the pre-existing behavior and the default for installs that don't opt in.
    """
    path = _CODEX_GH_AGENTS_CONFIG_PATH
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            logger.info("codex-gh-agents.json is not a JSON object; ignoring")
            return None
        return cfg
    except Exception:
        logger.info("codex-gh-agents.json unreadable or invalid JSON; ignoring")
        return None


def _pick_codex_gh_agent(working_dir: Optional[str]) -> str:
    """Pick the codex GH App agent name for a Codex invocation targeting
    `working_dir`. Returns the env-var default if no config or no match.

    Longest-prefix wins when multiple mappings match. Supports two mapping
    shapes:
      - exact: "owner/repo" matches only that repo
      - trailing-wildcard: "owner/*" matches all repos under owner
    Any other value is treated as an exact match.
    """
    cfg = _load_codex_gh_agents_config()
    if not cfg:
        return _CODEX_GH_TOKEN_AGENT_DEFAULT

    repo = _infer_repo_from_working_dir(working_dir)
    mappings = cfg.get("mappings") if isinstance(cfg.get("mappings"), dict) else {}
    default_agent = cfg.get("default_agent") or _CODEX_GH_TOKEN_AGENT_DEFAULT

    if not repo:
        return default_agent

    # Match repo against every mapping key; keep the longest that matches.
    best: Optional[Tuple[int, str]] = None  # (match_length, agent_name)
    for key, agent in mappings.items():
        if not isinstance(key, str) or not isinstance(agent, str):
            continue
        if key.endswith("/*"):
            prefix = key[:-1]  # keep trailing slash
            if repo.startswith(prefix):
                match_len = len(prefix)
                if best is None or match_len > best[0]:
                    best = (match_len, agent)
        else:
            # Exact match
            if repo == key:
                match_len = len(key) + 1000  # exact always beats wildcard
                if best is None or match_len > best[0]:
                    best = (match_len, agent)
    if best:
        return best[1]
    return default_agent


def _get_codex_gh_token(working_dir: Optional[str] = None) -> Optional[str]:
    """Generate (or return cached) GitHub App installation token for Codex.

    Picks the agent per _pick_codex_gh_agent(working_dir). Returns None
    silently if the script doesn't exist, isn't executable, or fails —
    callers must tolerate that and continue without the env injection.
    """
    global _codex_gh_token_cache
    agent = _pick_codex_gh_agent(working_dir)

    cached = _codex_gh_token_cache.get(agent)
    if cached:
        token, expiry = cached
        if time.monotonic() < expiry:
            return token

    script = _CODEX_GH_TOKEN_SCRIPT
    if not script or not os.path.isfile(script) or not os.access(script, os.X_OK):
        return None

    try:
        proc = subprocess.run(
            [script, agent],
            capture_output=True,
            text=True,
            timeout=15,
            stdin=subprocess.DEVNULL,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            token = proc.stdout.strip()
            _codex_gh_token_cache[agent] = (token, time.monotonic() + _CODEX_GH_TOKEN_TTL_S)
            logger.debug("Codex GH token generated for %s (cached %ds)", agent, _CODEX_GH_TOKEN_TTL_S)
            return token
        logger.debug("Codex GH token script returned %d: %s", proc.returncode, proc.stderr.strip()[:200])
    except Exception:
        logger.debug("Codex GH token generation failed", exc_info=True)
    return None


def _reset_codex_gh_token_cache_for_test() -> None:
    """Test helper — clear the in-memory cache."""
    global _codex_gh_token_cache
    _codex_gh_token_cache = {}


def execute_cli(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> DelegationResult:
    """Execute a headless CLI command and return parsed result.

    Builds the command based on cli.cli_id:
      claude:  claude -p "{prompt}" --output-format=json [--model X]
      codex:   codex exec "{prompt}" --json --full-auto [--model X]
      gemini:  gemini -p "{prompt}" --output-format=json -y [--model X]
    """
    if not cli.binary_path:
        return DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=False,
            output="",
            error="CLI binary not found",
            exit_code=-1,
        )

    builders = {
        "claude-code": _build_claude_cmd,
        "openai-codex": _build_codex_cmd,
        "gemini-cli": _build_gemini_cmd,
    }

    builder = builders.get(cli.cli_id)
    if builder is None:
        return DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=False,
            output="",
            error="Unknown CLI: {}".format(cli.cli_id),
            exit_code=-1,
        )

    cmd = builder(cli, prompt, model=model, working_dir=working_dir, max_budget_usd=max_budget_usd)
    logger.info("Executing %s: %s", cli.cli_id, " ".join(cmd[:4]) + " ...")

    # Inject Codex bot-account GitHub App token when invoking Codex. None
    # = inherit current env (the default subprocess behavior); falling back
    # to operator's gh credentials. See _get_codex_gh_token above for the
    # opt-in / disable contract.
    env = None
    if cli.cli_id == "openai-codex":
        gh_token = _get_codex_gh_token(working_dir=working_dir)
        if gh_token:
            env = {**os.environ, "GH_TOKEN": gh_token}

    start = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=working_dir,
            stdin=subprocess.DEVNULL,
            env=env,
        )
        duration_ms = (time.monotonic() - start) * 1000

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        success = proc.returncode == 0

        # Parse output based on CLI type
        output = _extract_output(cli.cli_id, stdout, stderr)
        error = stderr.strip() if not success and stderr.strip() else None

        result = DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=success,
            output=output,
            error=error,
            duration_ms=duration_ms,
            exit_code=proc.returncode,
        )
        logger.info(
            "Completed %s: success=%s, duration=%.0fms, output=%d chars",
            cli.cli_id, success, duration_ms, len(output),
        )
        return result

    except subprocess.TimeoutExpired:
        duration_ms = (time.monotonic() - start) * 1000
        logger.warning("Timeout after %.0fms for %s", duration_ms, cli.cli_id)
        return DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=False,
            output="",
            error="Execution timed out after {}s".format(timeout),
            duration_ms=duration_ms,
            exit_code=-1,
        )
    except OSError as e:
        duration_ms = (time.monotonic() - start) * 1000
        logger.error("OS error executing %s: %s", cli.cli_id, e)
        return DelegationResult(
            cli_id=cli.cli_id,
            auth_method=cli.preferred_auth,
            success=False,
            output="",
            error=str(e),
            duration_ms=duration_ms,
            exit_code=-1,
        )


def _build_claude_cmd(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
) -> List[str]:
    """Build Claude Code headless command."""
    cmd = [cli.binary_path, "-p", prompt, "--output-format", "json"]
    if model:
        cmd.extend(["--model", model])
    if max_budget_usd is not None and max_budget_usd > 0:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    return cmd


def _build_codex_cmd(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
) -> List[str]:
    """Build Codex CLI headless command.

    Sandbox mode is controlled by LLM_RELAY_CODEX_SANDBOX env var, read on
    every invocation so operators can change the setting without restarting
    the orchestrator process (e.g. when cli_delegate runs inside a long-lived
    MCP subprocess and the env is updated mid-session):
      - "workspace-write" (default): sandboxed, no shell access beyond workspace
      - "danger-full-access": full filesystem access, shell commands work
      - "none": --dangerously-bypass-approvals-and-sandbox (no sandbox at all)

    Users who need Codex to run gh, git, or read files outside the workspace
    should set LLM_RELAY_CODEX_SANDBOX=none.
    """
    sandbox = os.environ.get("LLM_RELAY_CODEX_SANDBOX", "workspace-write")
    cmd = [cli.binary_path, "exec", prompt, "--json", "--skip-git-repo-check"]
    if sandbox == "none":
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        cmd.extend(["--full-auto", "--sandbox", sandbox])
    if model:
        cmd.extend(["--model", model])
    if working_dir:
        cmd.extend(["-C", working_dir])
    return cmd


def _build_gemini_cmd(
    cli: CLIStatus,
    prompt: str,
    *,
    model: Optional[str] = None,
    working_dir: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
) -> List[str]:
    """Build Gemini CLI headless command."""
    cmd = [cli.binary_path, "-p", prompt, "--output-format", "json", "-y"]
    if model:
        cmd.extend(["-m", model])
    return cmd


def _extract_output(cli_id: str, stdout: str, stderr: str) -> str:
    """Extract meaningful output from CLI response."""
    if cli_id == "openai-codex":
        return _parse_codex_jsonl(stdout)
    # For claude and gemini, try to parse JSON and extract the result text
    return _parse_json_output(stdout)


def _parse_json_output(stdout: str) -> str:
    """Parse JSON output and extract the result text."""
    if not stdout.strip():
        return ""
    try:
        data = json.loads(stdout)
        # Claude Code JSON output has a "result" field
        if isinstance(data, dict):
            if "result" in data:
                return str(data["result"])
            if "content" in data:
                return str(data["content"])
            if "text" in data:
                return str(data["text"])
        return stdout.strip()
    except (json.JSONDecodeError, ValueError):
        return stdout.strip()


def _parse_codex_jsonl(stdout: str) -> str:
    """Parse Codex JSONL event stream and extract the final message."""
    if not stdout.strip():
        return ""
    last_message = ""
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                # Codex events have "type" field; look for message/response events
                event_type = event.get("type", "")
                if event_type in ("message", "response", "assistant"):
                    content = event.get("content", event.get("text", event.get("message", "")))
                    if content:
                        last_message = str(content)
                elif "content" in event or "text" in event or "message" in event:
                    content = event.get("content", event.get("text", event.get("message", "")))
                    if content:
                        last_message = str(content)
        except (json.JSONDecodeError, ValueError):
            continue
    return last_message or stdout.strip()


def prompt_hash(prompt: str) -> str:
    """SHA-256 hash of a prompt for dedup/tracking (no full prompt stored)."""
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def prompt_preview(prompt: str, max_len: int = 200) -> str:
    """Truncated preview of a prompt for logging."""
    if len(prompt) <= max_len:
        return prompt
    return prompt[:max_len] + "..."
