---
name: cfo-tester
description: Boots this CFO budgeting agent locally and hands it to a non-technical CFO persona subagent, which uses the app through the browser only and returns a concise, critical bullet-point rating of how relevant it actually is to a CFO's job. Use when asked to user-test, CFO-test, dogfood, or "have a CFO try" the app — or to sanity-check whether a feature is useful rather than merely working. Not a code review and not a test-suite run.
metadata:
  domain: evaluation
  role: orchestrator
  scope: this repo only
---

# CFO tester

You boot the app, then get out of the way. A subagent playing a **non-technical CFO** does the
actual evaluation, because the judgement being asked for — *is this relevant to my job?* — is
exactly the one an engineer who knows the codebase cannot make. Your two jobs are (1) hand that
subagent a working app, and (2) separate its complaints into "irrelevant" and "broken", which are
different findings with different owners.

Do not review code. Do not run `pytest`. Do not fix anything mid-run — a fix invalidates the
session the CFO is describing. Collect findings; act on them after, if asked.

## 1. Preflight

Three preconditions, in the order they bite. Fix, don't report, unless a fix needs a decision:

```bash
test -d .venv || python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
grep -q 'ANTHROPIC_API_KEY=.\+' .env || echo "MISSING API KEY"
ls data/*.parquet >/dev/null 2>&1 || .venv/bin/python -m app.generate_data
```

**No `ANTHROPIC_API_KEY` is a stop.** The server still starts, but every agent turn fails, and a CFO
staring at error bubbles rates the error handling, not the product. Say so and stop rather than
running a test whose result you already know.

Then record what the run is allowed to dirty:

```bash
git status --short data/
```

Keep that output. The CFO will lock assumptions, build scenarios and possibly approve a version —
all of which write to `data/`. `data/locked_assumptions.json` is **tracked**; the rest of what they
touch is gitignored runtime state. Diff this again at the end and report any tracked-file change so
the user can revert deliberately.

## 2. Boot

One process serves API and SPA. Use the preview tooling, never `Bash` for the server:

```
preview_start {name: "cfo-agent-3"}
```

That reads `.claude/launch.json` and opens a tab at `http://127.0.0.1:8323/`. Keep the returned
`tabId` (for browser calls) and `serverId` (for `preview_logs` / `preview_stop`). Ready when the log
says `Application startup complete.`

Confirm the two gates are open before handing off, because each one dead-ends the CFO on a wizard
instead of the product:

```bash
curl -s http://127.0.0.1:8323/api/profile | head -c 200
curl -s http://127.0.0.1:8323/api/budgetplan | head -c 200
```

- `setup_complete: false` → the SPA lands on `#/setup` and every gated route 409s. Either seed a
  profile from `data/demo_profile.json` or tell the CFO their first task **is** the setup wizard and
  that this is expected. Decide and say which; do not let them discover it as a bug.
- `configured: false` on the budget plan → Budget outlook opens on its own config form. Same call.

If the app is already running from an earlier session, `preview_list` first and reuse it.

## 3. Hand off to the CFO

Read `references/cfo-brief.md` and pass it to **one** subagent **verbatim**, appending the live base
URL and the `tabId`:

```
Agent {
  subagent_type: "general-purpose",
  run_in_background: false,
  description: "CFO user test",
  prompt: <contents of references/cfo-brief.md>
          + "\n\nThe app is at http://127.0.0.1:8323/ (browser tabId: <tabId>)."
          + "\nYour walkthrough is at skills/cfo-tester/references/tour.md — read that one file, and no others."
}
```

`run_in_background: false`: you need the verdict before you can triage it, and there is nothing
useful to do while a browser session runs.

Do not paraphrase, summarise or "improve" the brief. Its constraints are the experiment — a CFO who
reads `app/tools.py` stops being a CFO, and their rating becomes worthless.

While it runs, do nothing. Do not drive the same browser tab; you will steal the CFO's focus
mid-form.

## 4. Triage and report

Split the subagent's bullets into two lists, and label them plainly:

- **Relevance findings** — the answer to the question that was asked. Relay these close to verbatim,
  including the score and the harsh ones. Do not soften, re-rank by how hard they'd be to fix, or
  drop a bullet because you disagree with it; disagree out loud in your own line instead.
- **Defects** — anything that errored, hung, rendered wrong or 409'd. Cross-check each against
  `preview_logs {serverId, level: "error"}` before repeating it, and say which ones the log
  confirms. A CFO calling a 45-second research turn "broken" is a UX finding, not a bug; a traceback
  is a bug.

Then:

```bash
git status --short data/
```

Report tracked-file changes against the preflight snapshot. Leave the server running — the user will
usually want to look at what was described — and give them the stop command rather than stopping it
yourself:

```
preview_stop {serverId}
```

Close with one line of your own: does the CFO's verdict point at a missing feature, a mis-framed
existing one, or nothing actionable? That judgement is yours to make, and it is the only place in
this skill where your knowledge of the codebase belongs.

## Running it again

A second CFO on the same build is worth it only with a different starting point — a fresh profile
via the setup wizard, or a specific brief ("you have 40 minutes before a board call"). Two identical
runs mostly re-measure the subagent's variance. If you want breadth in one pass, give each CFO a
different section of `references/tour.md` and say so in their brief; do not spawn several with the
same instructions and average them.
