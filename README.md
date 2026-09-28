# Urdu Banking Translator

A zero-cost, local-first, bidirectional English ⇄ Urdu translator for Pakistani banking
communication (SMS alerts, app messages, support chats, complaints). It is built with
**FastAPI**, **SQLite** and **OpenRouter's free models**, trying Qwen first.

- **Exactly one model call per translation.** No automatic retries. If the local integrity
  check fails, the result is flagged in the response — the model is never called a second
  time for the same translation. A cache hit costs zero calls.
- **Sensitive values never leave the server.** IBANs, CNICs, account and card numbers, phone
  numbers, amounts, transaction IDs, OTPs/PINs, dates and times are masked locally as
  `[[IBAN_1]]`, `[[AMOUNT_1]]`, and so on. Before the model call, the server checks that
  none of the original values is in the prompt.
- **Integrity check after the single call.** Each placeholder must come back exactly once,
  with no invented numbers. If this fails, it's surfaced as `integrity.ok: false` with the
  specific problems — never silently retried.
- **Masked-only storage.** History, feedback and the response cache hold placeholders, never
  real values.
- **Curated example bank ("cold samples").** For each input, the 3 most relevant approved
  Pakistani banking examples are included in the prompt.
- **Conservative, review-gated self-improvement.** 👍/👎 feedback (with an optional category,
  comment or correction) is stored masked. The prompt and example bank never change from one
  piece of feedback — a pattern only becomes a *proposal* once it has recurred across
  `min_independent_examples` (default 3) independent sessions/messages, and even then it only
  takes effect once an admin approves it at `/admin`.
- **In-line correction.** The translated output can be edited directly (an "Edit" button next
  to Copy) instead of using the separate 👎 form — both paths feed the same review-gated
  learning loop, tagged by `source: "inline"` vs `"form"`.
- **Traceable context.** Every history row records which `model` and `knowledge_version`
  (a hash of `knowledge/prompt.json` + `cold_samples.json`'s mtimes) actually served it, so
  context stays auditable across a model swap or fallback. `POST /context/preview` shows the
  exact prompt + examples a translation would get, without calling the model.
- **Telemetry.** Prometheus metrics at `/metrics` (volume, latency, quality, cache hits,
  integrity failures, security flags, errors, fallbacks) with a ready-made Grafana dashboard.
- **Editable prompt.** `knowledge/prompt.json` holds the prompt in sections: natural banking
  language, code-switching, terminology, protected tokens, formatting and security.
- **Also:** language detection without AI (English, Urdu, Roman Urdu), streaming output,
  per-session history, correct RTL/LTR display of values, and automatic fallback to other
  free models (still a single HTTP request — OpenRouter picks one provider from the list).

## How a request flows

```
input ─► detect language (regex, no AI)
      ─► shield: mask sensitive values locally           ← real values stay in memory
      ─► pick ≤3 relevant approved examples + build prompt from knowledge/prompt.json
      ─► leak guard: refuse if any original value is in the outgoing prompt
      ─► ONE streamed call to OpenRouter (masked text only) — no retry, ever
      ─► local integrity check (placeholders exactly once, no invented values, Urdu script)
            └─ fails? flag it in the response; the model is not called again
      ─► restore values locally (times/dates localized, bidi-isolated for RTL)
      ─► heuristic checks ─► save MASKED source + MASKED output + model + knowledge_version
         to session history, and record Prometheus metrics
```

## Privacy model

| Data | Where it lives |
|---|---|
| Original sensitive values | Request memory only, discarded when the request ends |
| Text sent to the model | Masked text only, verified by `assert_no_leak` before sending |
| History (`masked_history` table) | Masked source and masked output, placeholder types, score. Per session, deleted after 24 h |
| Feedback (`feedback` table) | Masked source and output. Comment and correction are masked by the same shield before saving |
| Response cache | In memory, keyed and valued by masked text |
| Error messages | Never include source text or values |

SQLite runs with `secure_delete`, so deleted rows are overwritten. On startup, tables from
older versions that stored unmasked text (`history`, `translations`, `terminology`) are
dropped and the database file is vacuumed.

The protected types are `IBAN`, `CNIC`, `CARD`, `ACCOUNT`, `PHONE`, `AMOUNT`, `TXN`,
`SECRET` (OTP/PIN/CVV digits), `EMAIL`, `LINK`, `DATE`, `TIME`, and `NUMBER` (a safety net
for any other run of 4+ digits). Urdu digits (۰–۹) are handled everywhere. `POST /shield`
previews the masked text without calling the model.

## Knowledge files (edit these to improve translations)

### `knowledge/prompt.json`

| Key | Purpose |
|---|---|
| `role` | Opening line per target language |
| `sections[]` | Titled rule groups; each has `all`, `ur` and `en` rule lists |
| `keep_in_english` | Acronyms kept in Latin letters (ATM, OTP, IBAN…) |
| `loanwords` | Preferred Urdu wording; only entries that occur in the input are sent |
| `issue_rules` | Feedback categories, each with a pre-written rule the server can enable automatically |
| `enabled_issue_rules` | Rules the server has already enabled after independent evidence; only these are sent to the model |
| `learning` | `min_independent_examples` (default 3) and `window_days` (default 30) |

### `knowledge/cold_samples.json`

Each example has `id`, `tags`, `en`, `ur`, an optional `roman` (Roman Urdu) and `approved`.
Examples must use placeholders instead of real values, and every language version must
contain the same placeholders — enforced automatically whenever the server adds one.

How examples are chosen for a prompt:
- Each approved example is scored by word overlap weighted by IDF, shared placeholder types,
  and a Roman Urdu bonus. Votes play no part. The top 3 are used. When nothing matches, the
  examples tagged `core` are used.
- Examples tagged `security` are included only when the input looks like a prompt injection.

Both files reload automatically when they change, with no restart needed. You can hand-edit
either file at any time — the server picks up changes on the next request.

## Conservative, review-gated self-improvement

**Rule: a single feedback item never changes future behavior, and neither does an unreviewed
one.** Example selection and the prompt depend only on the knowledge files; feedback never
feeds into them directly, and nothing is ever applied without an admin decision.

1. **Monitor.** Every 👍/👎 or in-line edit is stored with masked text only (category, comment,
   correction, and whether it came from the in-line editor or the form — see `source`).
2. **Detect recurring patterns.** `compute_proposals()` groups feedback from the last
   `window_days` and looks for patterns that have at least `min_independent_examples`
   independent reports. "Independent" means different sessions, and for cross-message
   patterns also different messages, so one person repeating a complaint counts once.

   | Pattern | Proposed action |
   |---|---|
   | The same 👎 category across independent messages | Enable its pre-written rule from `issue_rules` |
   | An example keeps appearing in badly rated translations (👎 > 2 × 👍) | Disable that example |
   | The same message is independently corrected | Add the best-supported correction as a new example |
   | The same message and output are independently approved | Add it as a new example |

3. **Review, then apply.** A pattern that crosses the threshold shows up at `GET
   /admin/proposals` and waits there:
   - `POST /admin/proposals/{key}/approve` applies it — a new example still must pass local
     validation (no real values leaked, matching placeholders across languages) first; if
     every candidate fails, the endpoint returns an error and nothing is added.
   - `POST /admin/proposals/{key}/reject` dismisses it without applying it.
   - Either decision is recorded **once**: only evidence created after that decision counts
     toward proposing the same key again, so the same votes can't reapply or re-reject
     themselves.

`POST /feedback` returns `pending_review_count` — how many proposals currently await a
decision — purely for visibility; the submission itself never changes anything.

## Setup and run

```bash
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env              # then set OPENROUTER_API_KEY
uvicorn main:app --reload
```

Open `http://localhost:8000/`. API docs are at `/docs`.

| Variable | Default | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | — | Required |
| `OPENROUTER_MODEL` | `qwen/qwen3.8-27b:free` | Primary model |
| `OPENROUTER_FALLBACK_MODELS` | `nvidia/nemotron-3-ultra-550b-a55b:free,google/gemma-4-26b-a4b-it:free` | Free fallbacks when the primary is rate-limited (still one HTTP request) |
| `TRANSLATOR_DB_PATH` | `translator.db` | SQLite file |
| `KNOWLEDGE_DIR` | `./knowledge` | Location of `prompt.json` and `cold_samples.json` |
| `ADMIN_TOKEN` | — | Gates `/admin/*` (header `X-Admin-Token`). Unset disables the admin surface entirely (fails closed) |
| `WEB_CONCURRENCY` | `1` | uvicorn worker count (Docker/Procfile/render.yaml); see Scalability below |

## API

| Endpoint | Purpose |
|---|---|
| `POST /translate/stream` | Server-sent events: `meta`, `delta`s, then `done` or `error`. Exactly one model call happens here (zero on a cache hit) |
| `POST /translate` | The final `done` object as JSON |
| `POST /shield` | Masked preview, with no model call |
| `POST /context/preview` | The exact system prompt + examples a translation would get, with no model call |
| `GET /history`, `DELETE /history` | This session's masked history (now includes `model`, `knowledge_version`, `latency_ms`, `cached`, `human_feedback`) |
| `GET /feedback/categories` | Issue categories from `prompt.json` |
| `POST /feedback` | `{history_id, rating: 1\|-1, category?, comment?, correction?, source?: "form"\|"inline"}` for this session's items only. Never applies anything itself — see `pending_review_count` in the response |
| `GET /admin/proposals` *(admin)* | Proposals currently past the independent-evidence threshold, awaiting a decision |
| `POST /admin/proposals/{key}/approve` *(admin)* | Applies one proposal (validated first) |
| `POST /admin/proposals/{key}/reject` *(admin)* | Dismisses one proposal without applying it |
| `GET /admin/history` *(admin)* | Masked history across every session, each with any human feedback joined in |
| `GET /admin/health` *(admin)* | The same numbers `/metrics` exposes, as plain JSON |
| `GET /metrics` | Prometheus scrape endpoint |

*(admin)* endpoints require header `X-Admin-Token: <ADMIN_TOKEN>`. The admin review page is
at `/admin` (prompts for the token once, keeps it in `localStorage`).

A `done` object includes:
- `translated_text` (display-ready; strip `U+2066`–`U+2069` for SMS)
- `sent_to_model`, `history_id`, `samples_used`, `model`, `latency_ms`, `cached`
- `integrity {ok, problems}`
- `context {model, knowledge_version, samples_used}`
- `privacy {protected_count, protected_types, originals_sent_to_model, history_masked}`
- `security_flag`, `feedback {score, warnings}`

## Telemetry

`telemetry.py` defines Prometheus metrics (translation volume by language direction,
latency, quality score, cache hits, integrity failures, security flags, translation errors,
model-fallback count), recorded once per translation in `run_translation()`. `GET /metrics`
exposes them; `GET /admin/health` reads the same values back as plain JSON.

`docker-compose.yml` runs Prometheus + Grafana (the app itself runs on the host via
`uvicorn`, scraped at `host.docker.internal:8000/metrics`):

```bash
docker compose up -d
# Prometheus:  http://localhost:9090
# Grafana:     http://localhost:3000  (dashboard auto-provisioned from grafana/dashboards/)
```

The dashboard includes a "Requests per Minute (24h)" panel for spotting peak usage periods.

## Deploy (free)

`Dockerfile`, `Procfile` and `render.yaml` are included. Set `OPENROUTER_API_KEY` (and, if you
want the admin view in production, `ADMIN_TOKEN`) as secrets. `knowledge/` ships with the app
and is read/written on the server's own disk — on hosts with ephemeral disks, anything an
approved proposal has changed is lost on redeploy, so periodically copy
`knowledge/cold_samples.json` and `knowledge/prompt.json` back into the repo if you want to
keep it.

### Scalability

- SQLite runs in **WAL mode** (`PRAGMA journal_mode=WAL` in `db()`) with a `busy_timeout`, so
  concurrent readers and a writer don't produce "database is locked" errors — the one real
  correctness bottleneck the single-connection-per-request design had under load.
- `WEB_CONCURRENCY` (default `1`) sets the uvicorn worker count. Raising it is safe: the
  source of truth (SQLite + `knowledge/*.json`) is shared across processes. Each worker does
  keep its own in-memory response cache and model-fallback cooldown, so with N workers the
  effective cache hit rate is somewhat lower and fallback cooldowns are tracked independently
  — not a correctness issue, just slightly less globally coordinated. Deliberately not adding
  a shared cache (Redis, etc.) for this — not needed at this scale.

## Limitations

- **The shield is rule-based.** Names and street addresses are not masked. A 3-digit
  CVV/OTP is masked only when it appears shortly after its keyword; longer digit runs are
  always masked.
- **Integrity checks are deterministic but not semantic.** They guarantee values are neither
  lost nor invented, not that the sentence is translated well. A failed check is flagged for
  whoever reads the response, not retried.
- **Free-model rate limits.** When Qwen is rate-limited, the fallback models answer in the
  same single request; the `model` field shows which one served it.
- **Sessions have no login.** Anyone with the session cookie sees that session's masked
  history. For the same reason, "independent" reports are only as independent as sessions
  are: someone clearing their cookies repeatedly could in principle simulate several
  sessions. Local validation (no real values, matching placeholders) is the safeguard against
  a bad proposal passing validation, not a guarantee against gaming the vote count — the admin
  approval step is the actual safeguard against a gamed proposal being applied.
- **Admin auth is a single shared token**, not per-reviewer accounts — fine for one or a
  small trusted team, not for attributing which specific admin approved what.
