# Azure Deployment — History & Reference

Context for deploying additional agent apps to the same Azure account. Written after
deploying the first app (CFO Finance Agent demo). Read this before touching Azure —
it records what was tried, what failed, why, and the exact commands that worked.

## Live resource (do not disturb without asking)

| Resource | Name | Notes |
|---|---|---|
| Subscription | `Azure subscription 1` (`61e234e2-8cc8-462f-a120-2d9b0e0c4123`) | **Free Trial** (`FreeTrial_2014-09-01`), spending limit ON |
| Resource group | `cfo-demo-rg2` | West Europe |
| Container registry | `cfoagentdemoacr8105` (`cfoagentdemoacr8105.azurecr.io`) | Basic SKU, admin enabled — **shared by both apps** |
| App Service plan | `cfo-demo-plan` | **B1** (1 core / 1.75 GB), Linux — Agent 1 only |
| Web app | `cfo-agent-demo-8105` | Agent 1. Public URL: `https://cfo-agent-demo-8105.azurewebsites.net` |
| App Service plan | `cfo-agent3-plan` | **B1** (1 core / 1.75 GB), Linux — Agent 3 only |
| Web app | `cfo-budget-agent-8105` | Agent 3 (this repo). Public URL: `https://cfo-budget-agent-8105.azurewebsites.net` |
| App Service plan | `cfo-agent2-plan` | **B1** (1 core / 1.75 GB), Linux — Agent 2 only |
| Web app | `cfo-anomaly-agent-8105` | Agent 2 (`../CFO_Agent_2`). Public URL: `https://cfo-anomaly-agent-8105.azurewebsites.net` |

Live app settings on `cfo-agent-demo-8105`: `WEBSITES_PORT=8000`, `ANTHROPIC_API_KEY=<set>`,
`alwaysOn=true`, image `cfoagentdemoacr8105.azurecr.io/cfo-agent-demo:v2` (`:latest` points at the
same digest; `:v1` is retained for rollback).

Live app settings on `cfo-budget-agent-8105`: `WEBSITES_PORT=8000`, `ANTHROPIC_API_KEY=<set>`,
`alwaysOn=true`, `numberOfWorkers=1`, image `cfoagentdemoacr8105.azurecr.io/cfo-budget-agent:v3`
(`:latest` points at the same digest).

Live app settings on `cfo-anomaly-agent-8105`: `WEBSITES_PORT=8000`, `ANTHROPIC_API_KEY=<set>`,
`alwaysOn=true`, `numberOfWorkers=1`, image `cfoagentdemoacr8105.azurecr.io/cfo-anomaly-agent:v1`
(`:latest` points at the same digest). Full runbook:
[`../CFO_Agent_2/AZURE_DEPLOYMENT.md`](../CFO_Agent_2/AZURE_DEPLOYMENT.md).

Tag history for `cfo-anomaly-agent` — same warning as below, derive the next tag from the registry:

| Tag | Digest (short) | Pushed | Contents |
|---|---|---|---|
| `v1`, `latest` | `8cf58ae7` | 2026-08-13 | first Agent 2 image, through `a0c8d3f` |

**Agent 2 deliberately does NOT bake `data/` into its image** — the opposite of the choice below,
and not an oversight. Agent 3 bakes a snapshot for two reasons that both fail to apply to Agent 2:
it has no setup gate (every route serves unconditionally, so an empty `data/` is not a broken
demo), and it primes on a daemon thread rather than in the blocking lifespan hook (so the port
opens in ~1s regardless — measured: HTTP 200 within 12s of `docker run`). It seeds itself via
`generate_data.seed_if_empty()` on first boot instead. Don't "fix" its Dockerfile by copying this
one's `COPY data/` line.

Tag history for `cfo-budget-agent` — **read this before choosing a tag for a new push**:

| Tag | Digest (short) | Pushed | Contents |
|---|---|---|---|
| `v1` | `2ef691f0` | 2026-08-11 | first Agent 3 image |
| `v2` | `c3aded4b` | 2026-08-11 | through `ca5b856` (data ingestion) |
| `v3`, `latest` | `e90c3266` | 2026-08-13 | through `1a29f09` (token-spend cuts), assets at `?v=42` |

**This section said `:v1` was live until 2026-08-13, and it was wrong — `:v2` had been deployed
without the runbook being updated.** Pushing the "next" tag by reading this file alone would have
overwritten the image actually serving the demo and destroyed a rollback point. Always derive the
next tag from the registry and the app, never from this table:

```bash
az acr manifest list-metadata --registry cfoagentdemoacr8105 --name cfo-budget-agent \
  --orderby time_desc --query "[?tags!=null].{tags:tags, digest:digest, created:createdTime}" -o json
az webapp config show --name cfo-budget-agent-8105 --resource-group cfo-demo-rg2 \
  --query linuxFxVersion -o tsv
```

Continuous deployment is currently **off** on both — a new image requires a manual restart to pick up.

**Each app has its own plan on purpose.** B1 is one core, and the runbook's own sizing says B1 → 1
app. Giving Agent 3 a separate B1 rather than resizing `cfo-demo-plan` to B2 costs the same and means
neither demo can restart or starve the other — a plan SKU change restarts every app on the plan, and
these are demoed live. Agent 2 followed the same precedent in 2026-08 (`cfo-agent2-plan`), so the
account now runs three B1 plans with one app each. Free Trial had no quota objection to the third.

**Before running anything destructive or anything that restarts/resizes the plan, confirm
the demo isn't in use.** A plan SKU change (`az appservice plan update --sku ...`) restarts
every app on the plan, including this one.

## Why App Service, not Container Apps

Container Apps was the first choice and is normally the better fit (per-second billing,
easy scale-to-zero). It failed on this subscription for reasons specific to the **Free
Trial** tier, not misconfiguration:

1. **ACR Tasks are blocked** (`TasksOperationsNotAllowed`) — Microsoft disables remote
   image builds on trial/credit subscriptions as an anti-crypto-mining measure. This
   breaks `az containerapp up --source .`, which builds remotely by default.
   **Fix used:** build the image locally with Docker and push the finished image — plain
   `docker push` is unaffected, only remote *building* is blocked.
2. **Hard quota: 1 Container Apps environment per subscription, subscription-wide** —
   not per region. Deleting the old environment and recreating it under a new name in the
   *same* region hits `MaxNumberOfRegionalEnvironmentsInSubExceeded`; retrying in a
   *different* region hits `MaxNumberOfGlobalEnvironmentsInSubExceeded`. There is no
   bypass except waiting for the existing environment to be deleted, or upgrading off
   the trial.

Given that, **Azure App Service for Containers** was used instead: same Docker image, no
ACR Tasks dependency (you always push a locally-built image), no environment quota, and
it comfortably hosts multiple apps on one plan (verified empirically — see below).

## Multi-app capacity (verified, not assumed)

Tested directly: created a second web app on `cfo-demo-plan`, confirmed both apps served
HTTP 200 simultaneously, then deleted the test app. Conclusions:

- **App count is not the limit** on a Basic-tier plan — you can put 2, 3, or more web apps
  on one plan without hitting a quota.
- **CPU/RAM is the real limit.** B1 = 1 core / 1.75 GB. Each app here is a Python +
  FastAPI + pandas + pyarrow container. Two idle apps coexist fine; two apps under real
  concurrent load on 1 core will contend.
- **Recommended sizing:** B1 → 1 app comfortably. B2 (2 core / 3.5 GB) → 2 apps. B3
  (4 core / 7 GB) → 3 apps. Resize with:
  ```bash
  az appservice plan update --name cfo-demo-plan --resource-group cfo-demo-rg2 --sku B2
  ```
  This restarts every app on the plan — do it between demos, not during one.

**What was actually done for Agent 3: a second B1 plan, not a B2 resize.** Same cost, and it
removes both failure modes at once — no restart of the live Agent 1 demo, and no shared core for
two pandas apps to contend over. Prefer this whenever the apps are demoed independently; only
consolidate onto one larger plan if you need the headroom for a single app.

## The Free Trial ceiling (read this before relying on this account long-term)

The subscription has `spendingLimit: On`. When the $200 credit is exhausted (or the trial
window ends), **the whole subscription is disabled and every app stops serving** — no
warning, just downtime. This is separate from and in addition to the Anthropic API billing
(which is metered by Anthropic, not Azure). If this is going to be depended on, upgrade to
Pay-As-You-Go first — it also removes the ACR Tasks block and the 1-environment cap above.

## Deploying a second (or third) agent app — recommended steps

Reuse the existing registry and plan; only the app itself is new.

```bash
RG=cfo-demo-rg2
ACR=cfoagentdemoacr8105
PLAN=cfo-demo-plan
NEWAPP=<choose-a-unique-name>          # becomes <NEWAPP>.azurewebsites.net
REPO=<agent2-repo-name>                # e.g. "agent2", distinct from "cfo-agent-demo"

# 1. Build locally — MUST target amd64 even on Apple Silicon; Azure runs amd64
cd /path/to/agent2
docker build --platform linux/amd64 -t $ACR.azurecr.io/$REPO:v1 .

# 2. Push (login first; ACR Tasks restriction does not apply to a plain push)
az acr login --name $ACR
docker push $ACR.azurecr.io/$REPO:v1

# 3. Create the web app on the SAME plan
ACR_USER=$(az acr credential show --name $ACR --query username --output tsv)
ACR_PASS=$(az acr credential show --name $ACR --query "passwords[0].value" --output tsv)
az webapp create --name $NEWAPP --resource-group $RG --plan $PLAN \
  --container-image-name $ACR.azurecr.io/$REPO:v1 \
  --container-registry-url https://$ACR.azurecr.io \
  --container-registry-user $ACR_USER \
  --container-registry-password "$ACR_PASS"

# NOTE: az webapp create has a bug where --container-image-name including the registry
# host gets the host prepended AGAIN (resulting image path becomes doubled and wrong).
# It fires every time — it has now fired on all THREE deploys to this account (Agent 1,
# Agent 3, Agent 2) — so treat this as a step, not a contingency. ALWAYS verify:
az webapp config show --name $NEWAPP --resource-group $RG --query linuxFxVersion --output tsv

# If it shows "<acr>.azurecr.io/<acr>.azurecr.io/<repo>:v1", fix with `container set`.
# IMPORTANT: `container set` does NOT prepend the host, unlike `create`. Pass the
# FULLY-QUALIFIED name here. Passing a bare "<repo>:v1" is the tempting fix and it is
# wrong — it yields "DOCKER|<repo>:v1", which silently points the app at Docker Hub and
# fails to pull with a registry error that never mentions your ACR.
az webapp config container set --name $NEWAPP --resource-group $RG \
  --container-image-name $ACR.azurecr.io/$REPO:v1 \
  --container-registry-url https://$ACR.azurecr.io \
  --container-registry-user $ACR_USER --container-registry-password "$ACR_PASS"

# Correct end state — exactly one registry host:
#   DOCKER|<acr>.azurecr.io/<repo>:v1

# 4. App settings — port must match the Dockerfile's EXPOSE/CMD port, and inject
#    secrets as runtime settings, NEVER as Dockerfile ENV (that bakes them into the
#    image layers, leaking them to anyone who pulls the image)
az webapp config appsettings set --name $NEWAPP --resource-group $RG \
  --settings WEBSITES_PORT=<port> ANTHROPIC_API_KEY=<key>

# 5. Keep any background thread / scheduler alive between requests
az webapp config set --name $NEWAPP --resource-group $RG --always-on true

# 6. Confirm
az webapp show --name $NEWAPP --resource-group $RG --query defaultHostName --output tsv
curl -s -o /dev/null -w "%{http_code}\n" https://$NEWAPP.azurewebsites.net/
```

Before step 3, check the plan has headroom (see capacity section) and resize if needed.

## Local development loop (don't rebuild Docker for every code change)

Docker rebuild-and-push is only for *publishing*, not for day-to-day development:

```bash
# local iteration — instant reload, no container involved
.venv/bin/python -m uvicorn app.main:app --reload --port 8321
```

Rebuild and push only when you're ready to ship the current code to Azure:

```bash
docker build --platform linux/amd64 -t $ACR.azurecr.io/$REPO:latest .
docker push $ACR.azurecr.io/$REPO:latest
```

With continuous deployment **off** (the default here), pushing `:latest` does not restart
the app automatically — you must restart it manually (`az webapp restart`) to pick up the
new image. To automate this, enable CD:

```bash
az webapp deployment container config --enable-cd true --name $NEWAPP --resource-group $RG
```

Use versioned tags (`:v2`, `:v3`, ...) alongside `:latest` so a bad deploy can be rolled
back by repointing the app at a previous tag instead of rebuilding under pressure.

## The Dockerfile now lives in this repo

Agent 1's image was built from a Dockerfile that was never committed, so Agent 3 had to author
one from scratch. It is now at [Dockerfile](Dockerfile) with [.dockerignore](.dockerignore)
beside it — **keep them committed**; that was the single biggest time sink in this deployment.
Four choices in it are load-bearing:

- **`python:3.12-slim`**, not the 3.14 the local `.venv` runs. The app only needs 3.9+ at runtime,
  and 3.12 has the widest wheel coverage for pandas/numpy/pyarrow — which matters because the build
  runs under amd64 emulation on Apple Silicon. `requirements.txt` is lower-bound-only with no
  lockfile, so the image resolves whatever is newest at build time; the v1 build landed on
  pandas 3.0.5 / numpy 2.5.2 / pyarrow 25.0.1, matching the dev venv. **This is why the local
  smoke-run below is not optional** — it is the only thing standing between an unpinned resolve
  and a broken demo.
- **Explicit `COPY app/ static/ data/`**, never `COPY . .` — `.env` (a real key), `.venv/` (312 MB)
  and `tests/` then cannot reach a layer even if `.dockerignore` is wrong. Verified after building:
  `docker history --no-trunc <img> | grep -c sk-ant` → 0.
- **`data/` ships inside the image.** Every path constant in the app (`tools.DATA_DIR`,
  `store.HISTORY_DIR`, `scenarios.SCENARIOS_FILE`, …) derives from the source location and none is
  env-configurable, so the container must be writable at `/app/data`. Baking a populated snapshot
  makes `profile.setup_complete()` true on boot — otherwise the SPA lands on `#/setup` and ~55
  gated routes 409 — and makes the lifespan hook skip `generate_data.generate()`, which is the
  slowest part of cold start and can otherwise collide with App Service's container-start ping
  timeout. The trade is that a restart **resets the demo** to the baked state.
- **No `--workers` flag** → one uvicorn worker, for the single-instance reason below.

Smoke-run the image locally before pushing — this catches an unpinned-dependency break, a missing
`data/`, and the setup gate, all before Azure is involved:

```bash
docker run -d --name smoke -p 8324:8000 -e ANTHROPIC_API_KEY=<key> $ACR.azurecr.io/$REPO:v1
curl -s http://127.0.0.1:8324/api/profile     # want setup_complete: true, has_api_key: true
curl -s http://127.0.0.1:8324/api/scenarios   # want a populated list, not []
docker rm -f smoke
```

`has_api_key` only reports that the env var is *set*, not that it works. To check the key itself:

```bash
curl -s -o /dev/null -w "%{http_code}\n" https://api.anthropic.com/v1/messages \
  -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d '{"model":"claude-haiku-4-5-20251001","max_tokens":1,"messages":[{"role":"user","content":"hi"}]}'
```

There is **no `/health` endpoint** — `/` returns 200 with the SPA shell whatever the app's state,
so `/api/profile` is the real readiness signal for both a smoke test and any health-check probe.

## Gotchas worth remembering

- **`--platform linux/amd64`** is required on any build machine that isn't already amd64
  (e.g. Apple Silicon Macs default to arm64, which won't run on Azure). On a Windows amd64 box with
  Docker Desktop's WSL2 backend the flag is a no-op, but leave it in — it costs nothing and the
  command is then correct on every machine.
- **A 200 right after `az webapp restart` does not mean your new image is live.** App Service keeps
  the old container serving while the new one warms, so `/api/profile` answered 200 five seconds
  into a 758 MB pull — from the *previous* build. Verify the running image by fetching something
  that differs between builds, not by status code. The `?v=N` cache-busting string on the five
  asset links in `static/index.html` is ideal, since it changes on every frontend edit anyway:
  ```bash
  curl -s https://cfo-budget-agent-8105.azurewebsites.net/ | grep -o '?v=[0-9]*' | sort -u
  ```
  The real swap shows up as one failed request followed by the new version — expect ~15-30s.
- **State is ephemeral.** Anything written to the container filesystem (JSON files,
  SQLite, etc.) is lost on every restart and every redeploy. If a future app needs
  persistent state, mount an Azure File share into the container rather than relying on
  local disk. Agent 3 takes the other route deliberately — it bakes a good `data/` snapshot
  into the image, so a restart is a **reset to a known-good demo** rather than data loss.
  Anything a viewer creates live (scenarios, approved versions, chat history) does not survive.
- **A scheduler on `alwaysOn` spends tokens with nobody watching.** These apps run an in-process
  daemon thread; on Agent 3 the built-in hourly task runs a drift scan that calls the Anthropic
  API per finding. `alwaysOn=true` is required to keep the scheduler alive, so the spend is the
  price of the autonomy demo. Disable the task in the baked `data/tasks.json` if an app is going
  to sit idle for a long stretch.
- **API keys go in as runtime app settings, never as Dockerfile `ENV`.** `.env` files and
  any local secrets should stay in `.dockerignore` so they're never baked into an image
  layer.
- **No auth is enabled on purpose** for these public demo apps (Easy Auth is off). That
  means: (a) anyone with the link can call every endpoint, and (b) if the app calls a
  metered API (e.g. Anthropic) with no rate limiting, cost exposure is unbounded. Use a
  spend-capped API key for anything public.
- **Single instance / single replica.** Apps here rely on in-process background threads
  (schedulers) and local file state, which does not work correctly if the app scales to
  more than one instance. Don't enable autoscale on these plans without redesigning state
  storage first.
