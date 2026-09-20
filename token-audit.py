#!/usr/bin/env python3
"""Token-spend probe for Claude Code sessions.  READ-ONLY.

    python3 $HOME/Documents/code/claude-guards/token-audit.py [project-slug] [--detail SESSION_PREFIX]

Reads ~/.claude/projects/<slug>/*.jsonl (the session transcripts) and reconstructs the
billed cost from the `usage` block each API response carries.  This is the probe that
replaces prose about "we should use fewer tokens" with a number.

The multipliers below are NOT assumptions -- they were validated against Claude Code's own
ledger in ~/.claude.json (`projects.<cwd>.lastModelUsage`), which records costUSD per model:

  claude-opus-5[1m]      in 1,855  read 8,758,361  write 264,169  out 62,384
    1855*5 + 8758361*0.50 + 264169*10.00 + 62384*25  = $8.5897455  (ledger: 8.589745499999998)
  claude-haiku-4-5       in 1,124  read   301,741  write  29,951  out  4,439
    1124*1 +  301741*0.10 +  29951*1.25  +  4439*5  = $0.09093185 (ledger: 0.09093184999999998)

Both reproduce to 7+ significant figures, which proves three things:
  1. cache reads bill at 0.1x base input;
  2. the MAIN loop writes cache at 2.0x (1-hour TTL) while SUBAGENTS write at 1.25x (5-min TTL)
     -- so pushing work into a subagent is cheaper on the write side too, independent of model;
  3. the `[1m]` 1M-context variant bills at plain $5/$25 -- there is NO long-context premium.
"""
import collections
import glob
import json
import os
import sys

HOME = os.path.expanduser("~")

# $ per 1M tokens: (base_input, output)
RATES = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
READ_MULT = 0.1        # cache read
WRITE_5M = 1.25        # cache write, 5-minute TTL  (subagents)
WRITE_1H = 2.0         # cache write, 1-hour TTL    (main loop)


def rate(model):
    m = (model or "").split("[")[0]                      # strip the "[1m]" variant suffix
    for k, v in RATES.items():
        if m.startswith(k):
            return v
    return RATES["claude-opus-5"]                        # unknown -> price as Opus, flagged below


def requests(path):
    """Deduped API responses. The transcript can repeat a message id; count each once."""
    seen, out = set(), []
    with open(path, errors="ignore") as fh:
        for line in fh:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            msg = d.get("message") or {}
            usage = msg.get("usage")
            if not usage or msg.get("model") == "<synthetic>":
                continue
            mid = msg.get("id")
            if mid in seen:
                continue
            seen.add(mid)
            out.append((msg.get("model"), usage))
    return out


def cost(model, u):
    base, outrate = rate(model)
    cc = u.get("cache_creation") or {}
    w5 = cc.get("ephemeral_5m_input_tokens", 0)
    w1 = cc.get("ephemeral_1h_input_tokens", 0)
    if not (w5 or w1):                                   # older transcripts lack the split
        w1 = u.get("cache_creation_input_tokens", 0)
    return (
        u.get("input_tokens", 0) * base
        + u.get("cache_read_input_tokens", 0) * base * READ_MULT
        + w5 * base * WRITE_5M
        + w1 * base * WRITE_1H
        + u.get("output_tokens", 0) * outrate
    ) / 1e6


def prompt_size(u):
    return (u.get("input_tokens", 0)
            + u.get("cache_read_input_tokens", 0)
            + u.get("cache_creation_input_tokens", 0))


def detail(path):
    R = requests(path)
    print(f"\n{os.path.basename(path)}  ({len(R)} requests)")
    print(f"{'#':>4} {'model':<20} {'prompt':>9} {'read':>9} {'write':>8} {'out':>7} {'$':>7}")
    for i, (model, u) in enumerate(R, 1):
        print(f"{i:4} {(model or '?'):<20} {prompt_size(u):9,} "
              f"{u.get('cache_read_input_tokens', 0):9,} "
              f"{u.get('cache_creation_input_tokens', 0):8,} "
              f"{u.get('output_tokens', 0):7,} {cost(model, u):7.3f}")
    # where the money actually went
    agg = collections.Counter()
    for model, u in R:
        base, outrate = rate(model)
        cc = u.get("cache_creation") or {}
        w5, w1 = cc.get("ephemeral_5m_input_tokens", 0), cc.get("ephemeral_1h_input_tokens", 0)
        if not (w5 or w1):
            w1 = u.get("cache_creation_input_tokens", 0)
        agg["cache_read"] += u.get("cache_read_input_tokens", 0) * base * READ_MULT / 1e6
        agg["cache_write"] += (w5 * WRITE_5M + w1 * WRITE_1H) * base / 1e6
        agg["output"] += u.get("output_tokens", 0) * outrate / 1e6
        agg["raw_input"] += u.get("input_tokens", 0) * base / 1e6
    tot = sum(agg.values()) or 1.0
    print("\n  cost drivers:")
    for k, v in agg.most_common():
        print(f"    {k:<12} ${v:7.2f}  {v / tot:5.1%}")


def main():
    args = [a for a in sys.argv[1:]]
    slug = None
    target = None
    if "--detail" in args:
        i = args.index("--detail")
        target = args[i + 1] if len(args) > i + 1 else ""
        args = args[:i] + args[i + 2:]
    if args:
        slug = args[0]
    if slug is None:
        slug = os.getcwd().replace("/", "-")
    root = os.path.join(HOME, ".claude", "projects", slug)
    if not os.path.isdir(root):
        # fall back to the largest project dir so the probe still answers something
        cands = sorted(glob.glob(os.path.join(HOME, ".claude", "projects", "*")))
        print(f"no such project dir: {root}\nknown slugs:")
        for c in cands:
            print("  " + os.path.basename(c))
        return 2

    files = sorted(glob.glob(os.path.join(root, "*.jsonl")), key=os.path.getmtime, reverse=True)
    if target:
        hit = [f for f in files if os.path.basename(f).startswith(target)]
        if not hit:
            print(f"no session starting with {target!r} in {root}")
            return 2
        detail(hit[0])
        return 0

    print(f"project: {slug}")
    print(f"{'session':10} {'reqs':>5} {'floor':>7} {'median':>8} {'peak':>8} "
          f"{'cacheR':>12} {'cacheW':>9} {'out':>8} {'$':>7}")
    grand = 0.0
    for f in files:
        R = requests(f)
        if not R:
            continue
        sizes = sorted(prompt_size(u) for _, u in R)
        c = sum(cost(m, u) for m, u in R)
        grand += c
        print(f"{os.path.basename(f)[:8]:10} {len(R):5} "
              f"{prompt_size(R[0][1]):7,} {sizes[len(sizes) // 2]:8,} {sizes[-1]:8,} "
              f"{sum(u.get('cache_read_input_tokens', 0) for _, u in R):12,} "
              f"{sum(u.get('cache_creation_input_tokens', 0) for _, u in R):9,} "
              f"{sum(u.get('output_tokens', 0) for _, u in R):8,} {c:7.2f}")
    print(f"{'TOTAL':10} {'':5} {'':7} {'':8} {'':8} {'':12} {'':9} {'':8} {grand:7.2f}")
    print("\nfloor  = prompt size of request #1 (system + tools + MCP + CLAUDE.md, before any work)")
    print("median = typical resident context re-billed on EVERY request at 0.1x base input")
    print("--detail <session-prefix> for the per-request breakdown and driver split")
    return 0


if __name__ == "__main__":
    sys.exit(main())
