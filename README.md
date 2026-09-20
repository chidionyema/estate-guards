# estate-guards

The code behind the estate. This repository is the `scripts/` submodule of
[estate-core](https://github.com/chidionyema/estate-core). Clone the parent
with `--recurse-submodules`; on its own this repository restores nothing.

Private, and it stays private. It carries this machine's paths and the hostnames
of running services. A guard checks the visibility of both repositories every
hour and names the repository on the board if either goes public.

## The four things in here

**Guards refuse a mistake rather than describe one.** `hooks/pre-commit` reads
the staged blob and refuses a commit whose content looks like a credential.
`hooks/pre-push` refuses a push whose new feature carries no demo and no
onboarding page. Install them per repository with
`git config core.hooksPath scripts/hooks`.

**Jobs are declared once and rendered per platform.** `jobs/jobs.json` holds all
29 scheduled jobs with no platform in them: `{HOME}` where a home directory
would be, `{PYTHON3_SYSTEM}` where an interpreter would be, and a list of
directories where a search path would be. `jobs/platforms.json` is the only file
that holds a platform's own names. `jobs/render.py` writes the macOS plists,
`jobs/render_windows.py` writes Task Scheduler XML and reports what Windows does
not keep. Adding a third platform is a block in `platforms.json` plus a
renderer, not an edit to 29 job declarations.

**Drills prove a recovery path instead of assuming one.** `drills/run.py --list`
shows every path, whether it has been proved, and when. A path with no drill
written says so rather than showing green.

**Sweeps run themselves and report to a place with readers.** `estate/in-git.py`
checks every hour that the load-bearing files are in version control, that no
checkout is parked off main, and that the private repositories are private. It
writes to `ESTATE_BOARD.jsonl`, which every session is handed at startup.

## The one trap worth knowing before you touch anything

The parent repository records a pointer to one commit of this one. A commit made
here and not recorded in the parent is invisible to a clone, and nothing on this
machine notices, because this machine already has the files. Only a clone finds
out. The commit here and the pointer move in the parent are one action, in the
same turn, every time:

```
git -C ~/Documents/code/claude-estate/scripts commit -m "..." && git -C ~/Documents/code/claude-estate/scripts push
git -C ~/Documents/code/claude-estate add scripts && git -C ~/Documents/code/claude-estate commit -m "point at <sha>" && git -C ~/Documents/code/claude-estate push
```

`tracked.py --check` fails on a stale pointer and runs every 30 minutes, so the
trap is caught rather than remembered.

The other one: six sessions and a scheduled job share this checkout, so
`index.lock` collisions are normal. A lock is stale only when it is 0 bytes,
`lsof` on it is empty, and its mtime is not advancing. Retry before you remove.

## Where the rest is written

`docs/onboarding/` has one page per feature: what it is for, what it costs,
where it lives, and the one command that turns it off. `docs/demo/` has real
output from a real run of each. `rebuild/PREREQUISITES.md` lists what a new
machine needs that git must never hold.
