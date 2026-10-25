#!/usr/bin/env python3
"""context-guard-hook.py v2 — UserPromptSubmit guard against MARATHON SESSIONS.

v1 watched resident context only (250K/400K thresholds). Obsolete: with
CLAUDE_CODE_AUTO_COMPACT_WINDOW=200000 the context is capped — sessions no longer
die fat, they die LONG. Measured 2026-06-10 (7d): two ~14,000-turn, ~3-day sessions
= 91% of all weekly cost at a modest ~88K median resident context.

v2.1 (2026-08-06) RETUNED against measured data. Audit of 37 prospector sessions
(`token-audit.py -Users-chidionyema`): $374.64 / 4,183 requests = $0.0896 per request,
near-constant; cost drivers cache_read 55.6% + cache_write 23.3% = 79% context
transport, output only 21.1%. Peaks cluster at 160-167K (the 200K auto-compact knee)
and only 1 of 37 sessions ever reached 170K — so the old RESIDENT_HARD=170_000 fired
about ONCE in 37 sessions and the resident "strong" path was effectively dead. Warn
130_000 -> 85_000 (the measured mean-of-medians), hard 170_000 -> 140_000 (under the
knee, so it can actually fire). Beware: peak CAN exceed the window (max seen 277,374)
when one turn dumps a lot before compaction triggers.

v2 watches session SHAPE — user-prompt count, transcript size, session age, and
resident context — and when the shape goes marathon it injects an instruction that
tells Claude to (a) write a handoff to checkpoints/LATEST.md and (b) hand the user a
one-keystroke /compact. Loss-proof either way: memory-loop.py (SessionStart hook) re-injects
checkpoints/LATEST.md into the next fresh session automatically.

Per-session state lives next to the transcript: <session>.jsonl.guard.json
Cost when it fires: ~70 tokens of injected context. Silent otherwise. Never blocks.
"""
from __future__ import annotations
import json, os, re, sys, time

#: Above this, the PreToolUse half refuses ONE context-growing call per session. Same
#: number as RESIDENT_HARD on purpose: the nudge and the block must not disagree about
#: when a session is too fat, or the nudge trains you to ignore the block.
#:
#: It is one refusal, not a wall. Founder directive 2026-08-19: "have ne type sonethig is
#: friction, the goal is autonony / i should not have to nanually be involced". A guard
#: whose only escape is the founder typing `touch .../OFF` has moved the work onto the
#: founder, which is the opposite of what a guard is for -- and on 2026-08-19 it is what
#: stopped the tooling research the founder had asked for in the same session. The refusal
#: exists to make Claude stop and write the handoff. Once that is done,
#: CLAUDE_CODE_AUTO_COMPACT_WINDOW caps the context by itself and there is nothing left
#: for this half to protect.
#: DERIVED FROM THE WINDOW, NEVER HARDCODED. This number went stale twice for the same
#: reason: it was tuned against one value of CLAUDE_CODE_AUTO_COMPACT_WINDOW and the window
#: then moved. v1 set 170_000 against a 200K window and fired once in 37 sessions; v2.1 set
#: 140_000, the window later moved to 150_000, and 140_000 sits ABOVE the ~118K autocompact
#: trigger -- so autocompact always won the race and the resident path was dead AGAIN.
#: Measured 2026-08-19: a 150_000 window compacted at ~118K, and the 200_000 window's knee was
#: 160-167K. Both are ~0.79-0.80 of the window, so the trigger is a FRACTION of the window and
#: the guard reads the same env var Claude Code does.
_WINDOW = int(os.environ.get("CLAUDE_CODE_AUTO_COMPACT_WINDOW") or 200_000)
_TRIGGER = int(_WINDOW * 0.79)          # where autocompact fires, measured
RESIDENT_BLOCK = int(_TRIGGER * 0.90)   # ~10% of the window of room to write the handoff first
BLOCK_OFF = os.path.expanduser("~/.claude/state/contextguard/OFF")

#: Tools that GROW resident context. Everything not named here is allowed at any size,
#: which is the whole design: the way out of a fat session is to write the handoff and
#: commit, so Write, Edit, TodoWrite and the git half of Bash must never be refused.
_GROWING_TOOLS = {"Read", "Grep", "Glob", "WebFetch", "WebSearch", "Agent", "Task",
                  "NotebookRead"}

#: In auto mode reading happens through Bash (`cat`, `sed -n`, `rg`), so refusing the
#: Read tool alone would be a hole big enough to drive the session through. Only pure
#: readers are refused; git, tests, builds, redirects and the handoff write all pass.
_BASH_READER_RE = re.compile(
    r"^\s*(cat|bat|less|more|head|tail|rg|ag|ack|find|jq)\b"
    r"|^\s*grep\b|^\s*sed\s+-n\b|^\s*ls\s+-[a-zA-Z]*R")

# The nudge and the block must not disagree about when a session is too fat, so HARD is the
# same derived number as RESIDENT_BLOCK. WARN stays the measured mean-of-medians (85K) but is
# clamped under HARD, so a small window can never invert the two.
RESIDENT_HARD = RESIDENT_BLOCK
RESIDENT_WARN = min(85_000, RESIDENT_HARD - 10_000)
PROMPTS_WARN  = 25                 # user prompts in one session ≈ a task boundary passed
SIZE_WARN     = 20 * 1024 * 1024   # transcript bytes ≈ proxy for turn count
AGE_WARN      = 8 * 3600           # seconds; marathons ran ~3 days
RENUDGE_EVERY = 10                 # min prompts between nudges (no spam)
TAIL          = 200_000            # bytes of transcript scanned for resident ctx


def tail_text(path, n=TAIL):
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > n:
                fh.seek(size - n)
                fh.readline()
            return fh.read().decode("utf-8", "replace")
    except Exception:
        return ""


def is_compaction_boundary(rec):
    """True for the pair of records a compaction writes.

    Measured on transcript 5a5eafd3 (2026-08-19): a `system` record carrying
    `compactMetadata`, immediately followed by a `user` record with `isCompactSummary`.
    Either one is enough to know that every usage figure ABOVE it describes a context that
    no longer exists.
    """
    return bool(rec.get("compactMetadata") or rec.get("isCompactSummary"))


def resident(path):
    r = 0
    for line in tail_text(path).splitlines():
        if '"usage"' not in line and "ompact" not in line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        # A compaction throws the context away. Anything measured before it is history, and
        # quoting it makes the guard fire hardest at the moment the session got CHEAP -- the
        # exact mismatch the founder caught on 2026-08-19 (guard said 165K, statusline 73K).
        if is_compaction_boundary(rec):
            r = 0
            continue
        if rec.get("type") != "assistant":
            continue
        u = (rec.get("message") or {}).get("usage") or {}
        if u:
            r = (u.get("input_tokens", 0) or 0) + (u.get("cache_read_input_tokens", 0) or 0) \
                + (u.get("cache_creation_input_tokens", 0) or 0)
    return r


def load_state(path):
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return {"first_seen": time.time(), "prompts": 0, "last_nudge_prompt": 0}


PEV_DIRECTIVE = (
    "[pev] Standing workflow for implementation work this session: YOU plan, the "
    "executor implements, YOU verify.\n"
    "- Plan yourself: exact file paths, the specific edit per file, and the exact "
    "verification commands. The executor sees ONLY your plan text.\n"
    "- Dispatch mechanical/bulk implementation with the `pi_execute` MCP tool "
    "(MiniMax, cheap). Keep design, judgement and diagnosis in this session.\n"
    "- Then ALWAYS `pi_gate` (free, deterministic: typecheck/lint/tests) BEFORE any "
    "reasoning about correctness, and read the real diff — the executor's own report "
    "is a claim, not evidence.\n"
    "- Money rail / identity / contract / migrations never leave Claude Code; "
    "`pi_execute` refuses them by design. Small edits and one-liners: just do them "
    "here, a dispatch round-trip costs more than the edit."
)


def pi_bridge_registered():
    """Only advertise the workflow if the MCP server is actually configured."""
    try:
        with open(os.path.expanduser("~/.claude.json")) as fh:
            cfg = json.load(fh)
    except Exception:
        return False
    if "pi-bridge" in (cfg.get("mcpServers") or {}):
        return True
    for proj in (cfg.get("projects") or {}).values():
        if isinstance(proj, dict) and "pi-bridge" in (proj.get("mcpServers") or {}):
            return True
    return False


def save_state(path, st):
    try:
        with open(path, "w") as fh:
            json.dump(st, fh)
    except Exception:
        try: (__import__("sys").path.append(__import__("os").path.expanduser("~/.claude/scripts")), __import__("guard_report").broken(__file__, 170))
        except Exception: pass


def assess(r, prompts, size, age):
    """(signals, strong, fires) for one session shape. Pure, so it can be tested.

    Lifted out of main() on 2026-08-19 for exactly that reason: this is the whole hook -- when
    it nudges and how hard -- and nothing checked it. The hook fails OPEN, so a decision rule
    broken by a threshold edit looks identical to one that works, from inside a session.

    ONE signal is not enough on its own unless the shape is `strong`; a long session with
    nothing else wrong should not be nagged.
    """
    signals = []
    if r >= RESIDENT_WARN:
        signals.append(f"~{r/1000:.0f}K resident context re-billed every turn")
    if prompts >= PROMPTS_WARN:
        signals.append(f"{prompts} prompts this session")
    if size >= SIZE_WARN:
        signals.append(f"{size/1024/1024:.0f}MB transcript (high turn count)")
    if age >= AGE_WARN:
        signals.append(f"session is {age/3600:.0f}h old")

    strong = (r >= RESIDENT_HARD or prompts >= 2 * PROMPTS_WARN
              or size >= 2 * SIZE_WARN or age >= 24 * 3600)
    fires = bool(signals) and (len(signals) >= 2 or strong)
    return signals, strong, fires


def selftest():
    """Check the decision rule and the resident-context reader. Graded by process_audit.py."""
    import tempfile

    hour = 3600
    cases = [
        # (resident, prompts, bytes, age_s) -> (fires, strong)
        ((0, 1, 0, 0), (False, False)),                                  # fresh session
        ((RESIDENT_WARN, 1, 0, 0), (False, False)),                      # one weak signal only
        ((RESIDENT_WARN, PROMPTS_WARN, 0, 0), (True, False)),            # two weak signals
        ((RESIDENT_HARD, 1, 0, 0), (True, True)),                        # one STRONG signal
        ((0, 2 * PROMPTS_WARN, 0, 0), (True, True)),
        ((0, 0, 2 * SIZE_WARN, 0), (True, True)),
        ((0, 0, 0, 24 * hour), (True, True)),
        ((0, 0, 0, AGE_WARN), (False, False)),                           # age alone is weak
        ((RESIDENT_WARN - 1, PROMPTS_WARN - 1, 0, 0), (False, False)),   # just under both
        # The measured median session on 2026-08-19: 165K resident. Must fire STRONG.
        ((165_553, 30, 0, 2 * hour), (True, True)),
    ]
    failures = []
    for args, (want_fires, want_strong) in cases:
        _, strong, fires = assess(*args)
        if (fires, strong) != (want_fires, want_strong):
            failures.append(f"  assess{args}: want fires={want_fires} strong={want_strong}, "
                            f"got fires={fires} strong={strong}")

    # A COMPACTION throws the context away. Everything measured above the boundary describes
    # a context that no longer exists, so quoting it makes the guard fire hardest at the moment
    # the session got cheap. Caught by the founder on 2026-08-19: the guard said 165K while the
    # statusline said 73K, and both were reading the same file with the same formula.
    def _fat(n):
        return json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 0, "cache_read_input_tokens": n,
            "cache_creation_input_tokens": 0}}}) + "\n"

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(_fat(166_070))
        fh.write(json.dumps({"type": "system", "subtype": "compact_boundary",
                             "compactMetadata": {"trigger": "auto"}}) + "\n")
        fh.write(json.dumps({"type": "user", "isCompactSummary": True,
                             "message": {"content": "summary"}}) + "\n")
        compacted = fh.name
    # No assistant turn after the boundary yet -- the exact window in which PreToolUse fires
    # first in a continued session, and the only window where last-record-wins reads stale.
    got = resident(compacted)
    if got != 0:
        failures.append(f"  resident(compacted, no new turn yet) = {got}, want 0 "
                        "(the pre-compaction figure must not survive the boundary)")
    os.unlink(compacted)

    # ...and once a new turn lands, that is the figure.
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(_fat(166_070))
        fh.write(json.dumps({"type": "user", "isCompactSummary": True,
                             "message": {"content": "summary"}}) + "\n")
        fh.write(_fat(69_181))
        resumed = fh.name
    got = resident(resumed)
    if got != 69_181:
        failures.append(f"  resident(resumed after compaction) = {got}, want 69181")
    os.unlink(resumed)

    # ...and the same shape WITHOUT a boundary must still report the last figure. Without this
    # case, a reset that fired on every record would read as a pass above and disarm the guard.
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(_fat(166_070))
        fh.write(_fat(169_000))
        plain = fh.name
    got = resident(plain)
    if got != 169_000:
        failures.append(f"  resident(no compaction) = {got}, want 169000")
    os.unlink(plain)

    # `resident` must read the LAST assistant usage block, and must sum all three input
    # counters -- reading only input_tokens under-reports a cached turn by an order of
    # magnitude, which would silence the hook exactly when it matters most.
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 1, "cache_read_input_tokens": 2,
            "cache_creation_input_tokens": 3}}}) + "\n")
        fh.write(json.dumps({"type": "assistant", "message": {"usage": {
            "input_tokens": 10, "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 1000}}}) + "\n")
        tmp = fh.name
    got = resident(tmp)
    os.unlink(tmp)
    if got != 1110:
        failures.append(f"  resident(): want 1110 (last record, all three counters), got {got}")

    # The BLOCKING half. This is the first guard here that can stop work, so both
    # directions are pinned: what it refuses, and -- more important -- what it must never
    # refuse, because those are the calls that write the handoff and get the work saved.
    under, over = RESIDENT_BLOCK - 1, RESIDENT_BLOCK
    block_cases = [
        ("Read", {}, under, False),                 # under the ceiling nothing is refused
        ("Read", {}, over, True),
        ("Grep", {}, over, True),
        ("Agent", {}, over, True),
        ("WebFetch", {}, over, True),
        ("Write", {}, over, False),                 # the handoff must always be writable
        ("Edit", {}, over, False),
        ("TodoWrite", {}, over, False),
        ("Bash", {"command": "cat foo.py"}, over, True),
        ("Bash", {"command": "rg pattern src/"}, over, True),
        ("Bash", {"command": "sed -n '1,50p' a.py"}, over, True),
        ("Bash", {"command": "grep x a.py"}, over, True),
        # Everything needed to finish and ship. If any of these ever blocks, the guard has
        # trapped the session instead of ending it.
        ("Bash", {"command": "git commit -m 'x'"}, over, False),
        ("Bash", {"command": "git push"}, over, False),
        ("Bash", {"command": "gh pr create"}, over, False),
        ("Bash", {"command": "pytest -q"}, over, False),
        ("Bash", {"command": "cat > handoff.md <<EOF"}, over, False),
        # A redirect sends the bytes to a file, not into the transcript. Not this guard\'s harm.
        ("Bash", {"command": "rg pattern src/ > /tmp/hits.txt"}, over, False),
    ]
    for tool, ti, r_in, want_block in block_cases:
        got = block_reason(tool, ti, r_in) is not None
        if got != want_block:
            failures.append(f"  block_reason({tool}, {ti}, {r_in}): want block={want_block}, "
                            f"got {got}")

    # The one-shot. Refusing twice would leave the founder typing `touch .../OFF` as the only
    # way out, which is the manual step this hook is not allowed to create. Both directions are
    # pinned: the first call must refuse (or the handoff never gets written) and the second must
    # not (or the session is trapped, which is the failure the founder reported).
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        fh.write(_fat(RESIDENT_BLOCK + 1))
        fat_path = fh.name
    call = {"hook_event_name": "PreToolUse", "transcript_path": fat_path,
            "tool_name": "Read", "tool_input": {}}
    first, second = pretooluse(dict(call)), pretooluse(dict(call))
    if (first, second) != (2, 0):
        failures.append(f"  pretooluse one-shot: want (2, 0), got ({first}, {second})")
    for junk in (fat_path, fat_path + ".guard.json"):
        try:
            os.unlink(junk)
        except OSError:
            pass

    total = len(cases) + 2 + len(block_cases)
    if failures:
        print(f"context-guard selftest: {len(failures)}/{total} FAILED")
        print("\n".join(failures))
        return 1
    print(f"context-guard selftest: {total}/{total} passed")
    return 0


def block_reason(tool: str, tool_input: dict, r: int) -> str | None:
    """Should this tool call be refused at `r` tokens resident? None means allow.

    Pure, and tested below, because a guard that fails open looks exactly like a guard with
    nothing to say. This one can also fail CLOSED -- it can stop work -- so it is the first
    hook in this estate that has to be right in both directions.
    """
    if r < RESIDENT_BLOCK:
        return None
    if tool in _GROWING_TOOLS:
        return f"{tool} call"
    if tool == "Bash":
        cmd = str((tool_input or {}).get("command") or "")
        # A redirect means the bytes land in a FILE, not in the transcript, so it is not
        # the harm this guard exists for. `cat > handoff.md <<EOF` is how the handoff gets
        # written -- refusing it would trap the session in exactly the state it is trying
        # to escape.
        if re.search(r">>?\s*\S", cmd):
            return None
        if _BASH_READER_RE.search(cmd):
            return "read-only shell command"
    return None


BLOCK_MSG = (
    "context-guard: this session is at ~{k}K resident context, above the {lim}K ceiling.\n"
    "Every turn now re-bills that whole context. Measured 2026-08-06 across 37 sessions: "
    "cache_read is 55.6% of spend, so a turn at 165K costs roughly 5x the same turn at the "
    "35K floor. Reading MORE makes every remaining turn worse.\n"
    "\n"
    "THIS IS THE ONLY REFUSAL YOU WILL GET IN THIS SESSION. The next {what} is allowed, and "
    "so is every one after it. Nothing is blocked from here on, so do not stop, do not hand "
    "back, and do not ask the founder to type anything -- auto-compact caps the context by "
    "itself at CLAUDE_CODE_AUTO_COMPACT_WINDOW.\n"
    "\n"
    "Do these two things now, then carry straight on with the work:\n"
    "  1. Write the handoff to {ckpt}\n"
    "     Its FIRST section must be `## RESUME HERE` naming the one next action, so a\n"
    "     compaction at any moment loses nothing.\n"
    "  2. Commit and push what is already done.\n"
    "Then continue. Prefer narrow reads over whole files for the rest of the session.\n"
)


def pretooluse(data: dict) -> int:
    """The blocking half. Exit 2 refuses the call and shows stderr to the model.

    It refuses ONCE per session and then never again. A guard that keeps refusing has only
    one exit -- the founder typing `touch ~/.claude/state/contextguard/OFF` -- and that is a
    manual step in a workflow whose whole point is that there are none. Refusing once still
    lands the message that gets the handoff written; refusing forever just moves the work
    onto the founder.
    """
    if os.path.exists(BLOCK_OFF):
        return 0
    path = data.get("transcript_path") or ""
    if not path:
        return 0
    state_path = path + ".guard.json"
    st = load_state(state_path)
    if st.get("blocked_at"):
        return 0
    r = resident(path)
    what = block_reason(data.get("tool_name") or "", data.get("tool_input") or {}, r)
    if not what:
        return 0
    st["blocked_at"] = r
    save_state(state_path, st)
    ckpt = os.path.join(os.path.dirname(path), "checkpoints", "LATEST.md")
    sys.stderr.write(BLOCK_MSG.format(k=round(r / 1000), lim=RESIDENT_BLOCK // 1000,
                                      what=what, ckpt=ckpt))
    return 2


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    # One file, two hook events. They share the resident-context reader and the ceiling on
    # purpose -- a nudge and a block that disagreed about when a session is too fat would
    # teach you to ignore whichever fired first.
    if data.get("hook_event_name") == "PreToolUse":
        sys.exit(pretooluse(data))
    path = data.get("transcript_path") or ""
    if not path:
        sys.exit(0)

    state_path = path + ".guard.json"
    st = load_state(state_path)
    st["prompts"] = st.get("prompts", 0) + 1
    save_state(state_path, st)

    # ONCE per session: the plan/execute/verify standing directive. Injected on the
    # first prompt only — a per-prompt injection would be re-billed every turn, which
    # is the exact cost shape this hook exists to prevent.
    if not st.get("pev_sent") and pi_bridge_registered():
        st["pev_sent"] = True
        save_state(state_path, st)
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": PEV_DIRECTIVE,
        }}))
        sys.exit(0)

    prompts = st["prompts"]
    if prompts - st.get("last_nudge_prompt", 0) < RENUDGE_EVERY and st.get("last_nudge_prompt", 0) > 0:
        sys.exit(0)  # rate-limit: already nudged recently

    r = resident(path)
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    age = time.time() - st.get("first_seen", time.time())

    signals, strong, fires = assess(r, prompts, size, age)
    if not fires:
        sys.exit(0)  # healthy shape — stay silent

    st["last_nudge_prompt"] = prompts
    save_state(state_path, st)

    ckpt = os.path.join(os.path.dirname(path), "checkpoints", "LATEST.md")
    if strong:
        msg = (f"[session-guard] MARATHON SHAPE: {'; '.join(signals)}. "
               f"Claude: write a concise handoff (task+goal, decisions, files touched, exact "
               f"next steps) to {ckpt} now, commit and push what is done, then KEEP WORKING. "
               f"Auto-compact caps the context by itself and the memory-loop hook restores "
               f"that handoff, so do NOT ask the founder to type /compact or /clear, and do "
               f"NOT stop to hand back. Save silently, then carry on.")
    else:
        msg = (f"[session-guard] Session going long ({'; '.join(signals)}). Claude: at the "
               f"next task boundary, write a handoff to {ckpt}, then carry on. Do not ask "
               "the founder to type anything -- auto-compact fires on its own and the "
               "memory-loop SessionStart hook re-injects the handoff.")
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": msg,
        }
    }))
    sys.exit(0)


if __name__ == "__main__":
    main()
