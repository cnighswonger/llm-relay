# Design memo: per-repo Codex GH App token routing

**Author:** AITL
**Date:** 2026-07-12
**Status:** design proposal, R0
**Amends:** `src/llm_relay/orch/executor.py` Codex GH App token injection (2026-06 or thereabouts)
**Load-bearing:** yes — every Codex CLI invocation that touches GitHub goes through this token injection path.

## Why

Today `llm_relay.orch.executor` injects a single GH App installation token into every Codex subprocess via `GH_TOKEN`. The App identity is fixed via `LLM_RELAY_CODEX_GH_AGENT` (default `codex-reviewer`) or `LLM_RELAY_CODEX_GH_TOKEN_SCRIPT` env var. Whichever bot the process is configured for is what Codex gets, regardless of which repo the invocation is targeting.

**This breaks when the same host commissions reviews across multiple GitHub org/user accounts.** Today (2026-07-12 21:15Z) AITL commissioned Codex on:
- `vsits/agent-chat` PR #40 — worked. The `codex-reviewer-vsits` GH App is installed on `vsits/*` and has write there.
- `cnighswonger/kanfei-adsb` PR #9 — Codex ran the review (verdict APPROVED, 61 tests pass) but couldn't post: "GH_TOKEN cannot resolve cnighswonger/kanfei-adsb." The `codex-reviewer-vsits` App has no perms on `cnighswonger/*`.

A separate GH App `codex-reviewer` (no `-vsits` suffix) exists for Chris's personal account. `generate-token.sh` supports both. But the executor doesn't know to pick.

## What ships

A small config-driven per-repo token selector in `executor.py`:

1. New optional config file at `~/.llm-relay/codex-gh-agents.json`:
   ```json
   {
     "match_precedence": "longest-prefix",
     "mappings": {
       "vsits/*": "codex-reviewer-vsits",
       "cnighswonger/*": "codex-reviewer"
     },
     "default_agent": "codex-reviewer"
   }
   ```
2. When Codex is spawned with a `working_dir`, executor:
   - Reads the git remote origin URL of `working_dir` (best-effort via `git -C working_dir remote get-url origin`).
   - Extracts `owner/name` from the URL.
   - Matches against `mappings` keys (longest-prefix-wins if multiple match).
   - Uses the matched agent name to call `generate-token.sh <agent>`.
3. Falls back to the current env-var behavior (`LLM_RELAY_CODEX_GH_AGENT`) if:
   - `~/.llm-relay/codex-gh-agents.json` does not exist.
   - `working_dir` is None.
   - Remote URL can't be inferred.
   - No mapping matches AND no `default_agent` is set.
4. Cache invalidation: the token cache key becomes `(agent_name, ttl)` rather than just `ttl`, so switching between agents mid-session doesn't return a stale token from the wrong bot.

## Match precedence

`longest-prefix` wins. Example config:

```json
{
  "mappings": {
    "vsits/*": "codex-reviewer-vsits",
    "vsits/legacy-cartographer": "codex-reviewer-vsits-legacy"
  }
}
```

For repo `vsits/agent-chat`: matches `vsits/*` (10 chars). Uses `codex-reviewer-vsits`.
For repo `vsits/legacy-cartographer`: matches both `vsits/*` (10 chars) and `vsits/legacy-cartographer` (25 chars). Longer wins. Uses `codex-reviewer-vsits-legacy`.

This handles the common case of "org-wide default with specific overrides" cleanly.

## Glob syntax

Only two shapes supported (keep it simple):
- Exact match: `owner/repo` (matches only that exact repo).
- Trailing wildcard: `owner/*` (matches all repos under that owner).

No other glob metachars, no regex. Anything else in the mapping key is treated as an exact match against the full `owner/name` string.

## Failure modes

- **Config file missing:** silently fall back to env-var behavior. This is the current behavior; existing installs keep working without config.
- **Config file invalid JSON:** log warning at INFO, fall back to env-var. Don't crash the subprocess spawn.
- **Config file valid but no `mappings` match AND no `default_agent`:** log info ("no codex GH token routing for {owner/name}, no default"), inject nothing (fall back to CURRENT env-var, which may or may not work — that's the existing behavior).
- **Token generator script fails for the picked agent:** already handled — `_get_codex_gh_token` returns None on script failure, and executor spawns Codex with inherited env.
- **`git -C working_dir remote get-url origin` fails:** log debug, fall back to env-var. Common cause: `working_dir` isn't a git tree, or origin isn't set.

## Cache implications

Current cache: `(token, expiry_monotonic)` singleton. Under per-repo routing, two different agents could be needed within a single 50-min window. Fix: cache keyed by agent name, `{agent: (token, expiry)}`.

Test helper `_reset_codex_gh_token_cache_for_test` clears the full dict.

## Security considerations

- **Token isolation.** Each App's token has scope only for that App's installations. Injecting the wrong token into a subprocess is a functional failure (as demonstrated by today's cnighswonger/kanfei-adsb attempt), not a security breach. The token is scoped, not privileged beyond the App's installations.
- **Config file location.** `~/.llm-relay/codex-gh-agents.json` is the operator's home; only the operator writes it. Same trust model as the token generator scripts.
- **No secrets in config.** The config maps `owner/glob → agent_name`, not `owner → token`. Tokens are still minted per-call by `generate-token.sh <agent>`. No long-lived tokens in the config.

## Test plan

1. Unit: `_infer_repo_from_working_dir` extracts owner/name correctly from SSH, HTTPS, and github: URLs.
2. Unit: `_pick_agent` returns the correct agent for various mapping/config combinations, honoring longest-prefix.
3. Unit: cache dict handles two agents in the same test.
4. Integration: env-var-only setup (no config file) preserves current behavior.
5. Integration: config file with matching mapping selects the right agent.
6. Integration: config file with no match falls back to env-var-agent OR default_agent (both cases).
7. Live: commission Codex on `cnighswonger/kanfei-adsb` PR #9 to verify the fix works end-to-end. That's the loop-closing smoke.

## Rollout

1. This memo lands. Chris + MCA sign off.
2. Implementation PR against llm-relay main. AITL peer-reviews, Codex R1 via v2 tool.
3. Operator (Chris) writes the config file at `~/.llm-relay/codex-gh-agents.json` with the two mappings.
4. Restart `llm-relay` daemon (systemd user unit) to pick up executor changes.
5. Re-commission Codex on adsb PR #9 — verify labels + review post correctly under `codex-reviewer` (no `-vsits`) bot identity.
6. Update `agent-chat/docs/how-to-commission-codex-review.md` §Substrate context to mention per-repo routing (small doc PR).

## Not in scope

- Adding new GH Apps or expanding token generator's supported agents. This memo assumes the operator already has the App credentials.
- Auto-detecting which GH App is installed on a target repo. That would require a live API call per token mint; too expensive. Config maintenance is the operator's job.
- Multi-token concurrent access from a single Codex invocation. If Codex needs to reach TWO different orgs in one review (rare), it uses whatever token it was spawned with. Not addressed here.
- Non-Codex CLI token routing (Claude, Gemini). If those need it later, same shape generalizes.

## Bottom line

Small config-file addition + executor path split. Preserves current behavior for existing installs (no config = env-var fallback). Enables cross-org Codex reviews on single-host multi-account setups. Closes the loop on adsb_agent's stuck PR #9 review.

— AITL, 2026-07-12
