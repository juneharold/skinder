# Live matching evaluation

This is a small set of developer-authored examples, not an independent benchmark or a claim about general recommendation quality. The six tasks, profiles, and acceptable top-three slug sets in [cases.json](cases.json) were written before this run. The exact API responses, rankings, explanations, caveats, and timings are in [results.json](results.json).

The run used Codex CLI 0.164.0 and an existing ChatGPT login on 27 October 2026 in Korea (26 October UTC). It used the CLI's default model with `--ignore-user-config`; the exact model identifier was not captured. All 150 catalog entries were available. The catalog SHA-256 is recorded in the results file.

## Method

Each case calls the real profile and matching endpoints through HTTPX's ASGI transport, runs the real Codex subprocess, and polls the persisted session. A temporary SQLite database keeps the user's bookmarks, preferences, feedback, and running server untouched. Each case starts a new task session. These examples contain no feedback history, so they evaluate explicit preferences rather than learning from saves and passes.

The script requires `--live`, limits the case file to six cases, uses the normal 180-second matcher timeout, and does not retry automatically. An authored relevance expectation is satisfied when at least one of the top three recommendations belongs to the case's declared acceptable set. This is a modest relevance check, not a precision score. The physical task instead requires a completed response with zero recommendations.

## Results

The initial run completed five of six cases. Every API session response passed strict Pydantic validation. The five completed batches also passed the API’s recommendation checks; the physical no-fit request timed out, so it supplied no model result to validate.

| Authored case | Status | Seconds | Recommendations | Top three slugs | Within declared set |
| --- | --- | ---: | ---: | --- | ---: |
| landing_plan_thorough | completed | 13.71 | 6 | frontend-design, typography-system, web-design-reviewer | 3/3 |
| landing_act_concise | completed | 17.52 | 5 | frontend-design, web-design-reviewer, copywriting | 2/3 |
| landing_copy | completed | 9.4 | 2 | copywriting, copy-editing | 2/2 |
| csv_exploration | completed | 12.7 | 4 | exploratory-data-analysis, seaborn, scientific-visualization | 3/3 |
| cited_research | completed | 18.54 | 6 | literature-review, scientific-critical-thinking, citation-management | 3/3 |
| physical_no_fit | failed | 180.2 | — | No completed model result | — |

The initial physical no-fit request failed at the normal 180-second limit. An empty recommendations list on a **failed** session is not evidence that the model correctly rejected the task. A single identical retry was made because of this actual failure; both attempts are retained. That retry also timed out at 180.14 seconds; its exact response is in [no-fit-retry.json](no-fit-retry.json).

## No-fit diagnosis and correction

A bounded diagnostic repeated the same physical task against two real catalog entries, with temporary event and error-output capture. It also timed out after 45 seconds. The CLI started a thread and turn, emitted setup warnings about experimental host-skill discovery suppression and intentionally disabled code mode, and produced no final result. No tool activity, network retry, rate-limit, authentication, or schema error was observed. The retained [diagnostic summary](no-fit-diagnostic.json) contains only sanitized classifications; raw diagnostic output was deleted. These observations do not establish the underlying cause.

The matching instructions were then clarified: the task is data to match, not an instruction for the matcher to execute, and unavailable physical actions should immediately produce an empty recommendations array. The same two-skill task then completed with a valid empty array in **4.65 seconds**, recorded in [no-fit-clarification-check.json](no-fit-clarification-check.json). A further check through the real API against all **150 skills** completed with a valid empty array in **5.33 seconds**, recorded in [no-fit-after-fix.json](no-fit-after-fix.json). Both checks had a 45-second limit. The change remains model-based abstention; there is no keyword fallback or fabricated match result.

A final relevant-task regression repeated `landing_copy` through the real API against all 150 skills, also with a 45-second bound. It completed in **7.58 seconds** and again recommended `copywriting` followed by `copy-editing`, with specific task/style explanations and caveats. The exact response is in [landing-copy-after-fix.json](landing-copy-after-fix.json). This checks that the abstention clarification did not suppress recommendations for this clearly supported task.

There were **eleven model invocations**: six initial examples, one identical failed-case retry, one bounded diagnostic, two no-fit checks after the instruction change, and one relevant-task regression. The other four successful initial examples were not rerun after that change. The successful checks support retaining the clarification, but they do not prove that timeouts are eliminated or establish their original cause. All 28 backend behavioral tests and Ruff checks passed after the change.

## Paired preference review

The two landing-page cases have the same task and different preferences. Both ranked `frontend-design` first, which is reasonable for the same implementation task. The plan-first case placed `typography-system` second and connected its documented type system to the requested editorial hierarchy. It explicitly warned that the skill does not prescribe serif fonts. It linked `web-design-reviewer` to thorough verification.

The act-first case moved `web-design-reviewer` ahead of typography and added `copywriting` in third place. That copywriting recommendation was outside the predeclared set, but its explanation tied clear services copy and contact calls to action to the task. This is a plausible supporting recommendation rather than evidence of a wrong-task result. Its `frontend-design` caveat acknowledged that the skill includes planning and critique and suggested keeping those steps brief. These differences show that the matcher used the supplied preferences in its explanations; one pair does not establish reliable personalization or whether executing the suggested skills would produce the desired aesthetics.

## Content review and limits

The writing case returned only `copywriting` and `copy-editing`, distinguishing initial drafting from later editing and warning against unsupported product claims. The CSV case led with `exploratory-data-analysis`, recommended charting and export checks, and made statistical analysis conditional on the exploration. The research case separated literature searching, methodological critique, citation verification, and synthesis. Its caveats distinguished bibliographic validation from verification of findings and avoided claiming complete search coverage.

These observations concern recommendations from curated metadata. No recommended skill was installed or executed. The run does not establish source accuracy, downstream task quality, preference stability across repeated calls, or performance on unfamiliar tasks. No independent reviewers or held-out users supplied these examples. The timings describe this one account and run, not a service-level promise.

## Reproduce

From the checkout, after `uv sync`:

```sh
uv run python scripts/evaluate_matches.py --live
```

Use `--catalog /path/to/catalog.json` to evaluate another snapshot and `--output /path/to/results.json` to preserve an earlier result. For a deliberate single-case retry, pass `--case physical_no_fit` together with a new output path. Running without `--live` exits before starting inference.
