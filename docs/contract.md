# Skinder implementation contract

This contract is shared by the independent backend, catalog, and frontend tasks. Keep all JSON field names snake_case. The app is single-user, bound to loopback, and does not install or run discovered skills.

## Catalog

Ship `src/skinder/data/catalog.json` as `{ "version": 1, "generated_at": "ISO timestamp", "skills": [Skill...] }`. Ship `src/skinder/data/sources.json` with pinned source revisions and selected paths. The backend loads this snapshot, with no network requirement at startup.

`Skill`:
- `id`: stable opaque URL-safe string derived from repository + skill directory (not revision); unique across all sources.
- `name`: readable display name; `slug`: original frontmatter skill name.
- `description`: short original summary of purpose (at most 300 characters).
- `category`: one of `Development`, `Design`, `Writing`, `Research`, `Documents`, `Data`, `Productivity`, `Marketing`.
- `author`: source owner; `repository`: `owner/repo`; `source_url`: commit-pinned GitHub SKILL.md URL; `revision`: commit SHA; `license`: source-supported license label or `See source license`.
- `compatibility`: list of `codex` and/or `claude-code`; only include a target after checking portability.
- `requirements`: string[] describing needed tools/accounts/packages; use [] for none specifically required by the instructions.
- `tags`: string[] of 2–5 concise capability tags.
- `workflow`: string[] of 1–3 evidence-backed descriptions of the working method.
- `output_style`: string[] of 1–3 evidence-backed descriptions of the intended output; [] when unspecified.
- `evidence`: array of `{trait: string, excerpt: string}` for short literal source evidence supporting workflow/output traits. These traits are Skinder-inferred, not measured quality.
- `install_commands`: object with optional `codex` and `claude-code` string values. Copy-only commands, no automatic execution. Prefer documented skills CLI for portable standalone skills; retain interactive confirmations; do not use --yes or --all.

Target 150 real skills from several maintainers across the categories. Deduplicate exact copied instructions but retain distinct approaches to similar tasks. Do not mirror full source bodies into the catalog. Record attribution/license and only short evidence excerpts. Source retrieval and refresh are explicit Python maintenance commands.

## API

Errors: appropriate HTTP status and `{ "detail": "human-readable message" }`.

`Profile`: `{agent: "codex" | "claude-code", planning: "balanced" | "plan-first" | "act-first", communication: "balanced" | "concise" | "detailed", verification: "balanced" | "pragmatic" | "thorough", output_style: string}`. All preferences default balanced, output_style empty, agent codex.

`Recommendation`: `{skill_id: string, task_fit: string, taste_fit: string, caveat: string | null}`.

`Feedback`: `{id: string, session_id: string, skill_id: string, action: "save" | "pass", reason: "wrong-task" | "wrong-style" | "too-much-setup" | null, created_at: string}`.

`Saved`: `{items:[{skill:Skill,saved_at:string}]}`. Items are ordered newest first.

`FeedbackResult`: `{feedback:Feedback,saved:Saved}`. Every bookmark or feedback mutation returns the current saved collection, so clients can update it immediately.

`MatchSession`: `{id: string, task: string, agent: string, status: "pending" | "running" | "completed" | "failed" | "cancelled", recommendations: Recommendation[], feedback: Feedback[], error: string | null, created_at: string, updated_at: string}`. Recommendations contains the current batch. Feedback spans the whole task session. At most one matching process is active.

Endpoints:
- GET `/api/health` → `{status:"ok", catalog_count:number, codex:{installed:boolean, authenticated:boolean, message:string}}`.
- GET `/api/catalog` → `{skills:Skill[], categories:string[]}`.
- GET `/api/skills/{id}` → Skill.
- GET `/api/profile` → Profile; PUT `/api/profile` accepts complete Profile → Profile.
- POST `/api/matches` accepts `{task:string, agent:"codex"|"claude-code", session_id?:string}` → MatchSession (HTTP 202). With session_id, create another batch for that same task and target, using new feedback and excluding reviewed skill IDs. Reject mismatched task/agent on continuation. Without session_id, create a fresh task session. Task length 3–4000; profile output style at most 2000.
- GET `/api/matches/current` → latest MatchSession or null; GET `/api/matches/{id}` → MatchSession. Frontend polls running sessions.
- POST `/api/matches/{id}/cancel` → MatchSession.
- POST `/api/feedback` accepts `{session_id, skill_id, action, reason?}` → FeedbackResult. Save creates a bookmark. A plain pass is task-specific, not global negative taste. Only recommendations in the session may receive feedback. Repeated identical requests return the existing feedback and current saved collection, without recreating a removed bookmark. Reject new feedback with HTTP 409 while that session is pending or running; identical retries remain allowed.
- DELETE `/api/feedback/{id}` → Saved. Undo removes feedback and reverses a save only if that feedback owns the bookmark. If another session saved the skill since the bookmark was created, transfer ownership to that feedback. Explicit independent saves survive undo. Older feedback cannot restore an explicitly removed bookmark. Reject undo with HTTP 409 while its session is pending or running. A missing feedback ID is a no-op.
- GET `/api/saved` → Saved.
- PUT `/api/saved/{skill_id}` → Saved. Independently bookmark a catalog skill without recording feedback or a preference event; unknown skills return HTTP 404. Repeated saves preserve the original saved_at timestamp and make an existing deck bookmark independent of undo. Saving after removal creates a new timestamp. Allowed during matching.
- DELETE `/api/saved/{skill_id}` → Saved. Idempotently remove a bookmark; feedback history remains. Allowed during matching.

Pydantic models are the contract source of truth. Backend provides a command to export the OpenAPI schema; frontend generates TypeScript types via openapi-typescript after integration.

## Matching and persistence

Use codex exec with an existing local login, no separate API key. A compact request includes only the supplied task, profile, eligible catalog metadata, and up to 30 recent feedback events with their task context. Positive saves are weak contextual preference signals; only explicit wrong-style/too-much-setup negatives affect future taste. Explicit preferences override learned hints. A wrong-task or plain pass only excludes the skill from that task session.

Return up to 12 relevant recommendations, possibly fewer or none. Explain task fit and taste fit separately; no arbitrary percentages. Validate IDs, compatibility, duplicates, seen IDs, and JSON shape before committing results. Keep existing completed results if a subsequent batch fails. No hidden keyword fallback masquerading as AI matching. New feedback affects the next batch, not the order of cards already displayed.

SQLite persists profile, task sessions, feedback, and bookmarks. Restore latest session after refresh. On backend restart mark in-flight matching interrupted/failed rather than automatically repeating it. Missing Codex/login and model errors must leave catalog browsing and saved items usable. Cancel/timeout must terminate and reap the process, and late results must not overwrite cancelled state. Default timeout 180 seconds. Credentials never enter browser responses or application database. Bind 127.0.0.1:8742; reject cross-origin browser API calls. Vite dev proxy allows normal same-origin frontend requests.

Bookmarks have a nullable feedback_id: a non-null value identifies the deck feedback that owns the bookmark; null marks an independent save. On startup, legacy bookmarks with a required feedback_id are rebuilt transactionally with a nullable column, preserving all rows, timestamps, ownership, and other tables.

## Deliverable checks

Backend: pytest + HTTPX behavioral tests, real temporary SQLite DB, deterministic fake matcher only at the external process boundary. Test persistence, feedback/undo, invalid/unknown/repeated/incompatible recommendations, no-fit, cancellation and failures. Do not test exact prompt prose.
Frontend: typecheck/build plus meaningful interaction tests; browser QA desktop/mobile/keyboard/reduced motion. Live Codex checks on representative tasks and paired taste profiles after integration, reporting actual outcomes without overstating evaluation quality.
