# Muse parity

Findings from probing Meta's Muse (muse.ai, free plan) and this repo's AnythingLLM (workspace `algorithms`, model `glm-5.3-flash`) side by side on 2026-10-07. Every claim below comes from a probe run that day, the AnythingLLM logs, or a file in this repo.

## Probes

Each probe ran in a fresh Muse side chat and a fresh AnythingLLM thread with the same prompt.

| Probe | Muse | AnythingLLM |
| --- | --- | --- |
| List your tools | ~60 tools in 12 groups, accurate | 40 tools; says Gmail search/read and calendar writes are missing, though both are installed (see B2) |
| Tech news, last 48 h, cited | ~40 s, 3 stories, dated sources | ~2 min wall clock, 3 stories, links, "Sources +61" chip |
| Dice simulation, chart and table | \<60 s, inline chart and table | 26 s, table inline; chart published unasked to `/public` (see B4) |
| Interactive Pomodoro page, private | Background build with live status, then a working artifact in the Library | 1 m 31 s; wrote `/project/pomodoro-timer.html` and told the user to download it or run `python -m http.server`, neither of which the UI offers (see B13) |
| One-off reminder, then delete | ~15 s, inline "Scheduled task" card; deleted in chat | Created cron `0 7 8 10 *` (yearly) and called it one-off; could not delete it (see B3) |
| Image generation | ~30 s, good watercolor image inline | No image model; hand-drew an SVG and published it unasked (see B4) |
| Read-only browser task (HN #1 and top comment) | ~1 min, live preview card, correct | 21 s, correct; card rendered blank in the chat (see B5) |
| Describe your memory | Curated `MEMORY.md` (Facts, Preferences, Commitments), dated notes, people pages, forget flow | 2 m 41 s and 6+ tool calls; said it has no memory store (see B6) |
| Quick deep research | ~90 s, cited report in the chat | 6.5 min in research-runner, report published to the research site; the chat is not told when it lands |
| CSV upload, summarise | Correct totals | Correct totals; the CSV was pasted into the sandbox script inline (see B7) |
| Plan Saturday (forecast, calendar, 2 activities) | Calendar checked, timed itinerary, knew the home city | Calendar call returned 403 (see B1); itinerary otherwise good; did not know the home city |

## What Muse does well

| Capability | How it shows up |
| --- | --- |
| Asynchronous work | Long jobs run in the background with a status line ("Starting build", "Writing migration"), and the result posts back into the same chat. |
| Activity feed | A panel lists every task with a one-line outcome and time, across all chats. |
| Side chats | Auto-titled from the request ("Simulate dice rolls and plot distribution"). The agent can create, read, rename and message other chats. |
| Artifacts and widgets | Durable artifacts (documents, web apps with storage, decks) in a searchable Library; ephemeral inline widgets for in-chat UI. |
| Media | Image and video generation, text to speech, podcast episodes. |
| Scheduling | One-off reminders, recurring jobs, a 30-minute heartbeat, event hooks; an Upcoming tab lists them. |
| Goals and tracking | A Goals page with tracked commitments that the agent closes on evidence (fare watches, reminders, learning goals). |
| Memory | Curated `MEMORY.md` plus dated notes and people pages; a memory import from other assistants; refuses to store government IDs. |
| Proactivity | Feed tab, Ideas catalog (about 20 ready-made automations), onboarding tips, inbox watching. |
| Approvals | Per-connector and per-website defaults ("ask for some actions", "always ask"), an Approvals history, a secure credential store the agent never reads. |
| Connectors | Gmail (two accounts), Calendar, Contacts, Drive, GitHub, Plaid, health data, Spotify, Messenger, Instagram; WhatsApp as a chat channel. |
| Purchases | Wallet (Shop Pay, Stripe Link) with checkout behind explicit approval. |

Weak spots seen: pages take 10-15 s of skeleton loading, the free plan has a weekly usage cap, and artifacts render in opaque-origin iframes that browser automation cannot read. Its activity log said the CSV read was truncated, though its totals were right.

## Gaps and what to build

Status: **has** (comparable), **partial**, **missing**. Size: S (a skill or a fix), M (a new runner op or MCP tool set), L (a new service).

| Muse feature | AnythingLLM | Closest repo piece | Build | Size |
| --- | --- | --- | --- | --- |
| Result posts back into the chat when a long job ends | missing | research-runner and agents-runner live cards; gateway `POST /v1/runs` | On run completion, have the runner append a message to the originating thread through AnythingLLM's developer API (agents-runner already holds a key). | M |
| Activity feed across chats | missing | `runs.runlog` in research and agents; `scheduled_job_runs` | A page on the pages site listing runs, job runs and published pages with outcome and time; card in chat. | M |
| One-off reminders, list, edit, delete jobs | partial: create only, cron only | `create-scheduled-job` (built in) | Skills `list-scheduled-jobs`, `delete-scheduled-job`, and a one-off mode that disables the job after its first run. | S |
| Reminder delivery to the phone | missing | relay's ntfy (`NTFY_URL`); Telegram channel in AnythingLLM settings | Route job results to ntfy or Telegram. | S |
| Image generation | missing | `chatimage` draws cards only | MCP tool or skill calling an image model (OpenRouter), saving into `/project` and returning a card. | M |
| Interactive artifacts | missing: `/public` serves static pages, scripts do not run | sandbox `/public`, `publish`, Caddy on :8447 | A private artifacts origin that allows inline scripts under a strict CSP (no network), plus a Library index page. | L |
| Private file viewer for `/project` | missing | sandbox-runner, pages site | A read-only, tailnet-only view of `/project` and `/work`, so "it lives in /project" is reachable. | M |
| Memory the agent can see and manage | partial | AnythingLLM's native `memories` table (4 rows, global and workspace scope); `rag-memory` | Skills to list and forget native memories, and an instruction to check them; keep profile facts (home city) there. | S |
| Goals and tracked commitments | missing | sites (entries), scheduled jobs | A goals store with a skill to add, update and close goals, plus a weekly review job. | M |
| Feed and morning briefing | partial: daily news page only | `scheduled-jobs/daily-news-page` | A morning-brief job (calendar, inbox, news, weather) posting to a thread and ntfy. | S |
| Connectors: Calendar | broken (B1) | AnythingLLM's Google Calendar bridge | Fix the bridge deployment. | S |
| Connectors: Contacts, Drive | missing | MCP gateway | Add MCP servers behind the gateway's grants. | M |
| Approvals queue and history | partial: prompt rules, browser-login ask-first | `browser` take-over view approvals | Generalise the take-over view's approval list (`docs/TODO.md` already notes one approval per workspace) into one queue for every acting skill. | M |
| Auto-titled threads | missing: title is the truncated first message | AnythingLLM thread rename API | After the first reply, rename the thread from a short summary. | S |
| Text to speech, podcasts | partial: AnythingLLM reads replies aloud | podcasts were dropped and archived | Out of scope unless revived from the archive. | -- |
| Wallet and purchases | missing by design | browser-handoff for payments | Leave out. | -- |
| Subagents | has (read-only) | `delegate`, agents-runner | -- | -- |
| Web research, deep research | has | web-browsing, research-runner | Only the post-back above. | -- |
| Code sandbox | has | sandbox-runner | Attachments into the sandbox (B7). | S |
| Live browser with take-over | has | browser-runner | -- | -- |

## Bugs and rough edges

| ID | Finding | Evidence | Where to look |
| --- | --- | --- | --- |
| B1 | The calendar is unreadable: `gcal-get-events-for-day` returns 403 and the agent reports "blocked". Nothing is logged as an error. | Saturday-plan probe, 14:00; log shows the call and `GoogleCalendarBridge` base-URL lookup, then no error. | The Google Calendar bridge deployment and its OAuth grant. |
| B2 | The tool reranker drops tools the user needs, and the agent then tells the user they do not exist. | First probe listed Gmail as send-only and Calendar as read-only; the 13:58 log shows 35 tools attached, with `gcal-create-event` and Gmail search outside the cut. | `AGENT_SKILL_RERANKER_TOP_N=35` in `anythingllm/env.example`; turn off unused built-in skills, or raise N. |
| B3 | A "one-off" reminder is stored as yearly cron `0 7 8 10 *`, and the agent claimed it would not recur. No tool can list or delete jobs, so the agent cannot undo what it creates. | Job #9 in `scheduled_jobs`; deleted by hand from Settings > Scheduled Jobs. | `create-scheduled-job`; the system prompt's scheduling rule. |
| B4 | The agent publishes to the live `/public` site without asking, for a chart and an image. A folder without `index.html` is served as a Caddy directory listing, and its card shows no image. | `/public/algorithms/dice-sums/dice_sums.png` (listing page), `/public/algorithms/fox-cafe/`. Both removed. | `anythingllm/system-prompt.md` (the /public bullet), `publish` and `run-code` descriptions. |
| B5 | Cards sometimes render as the bare text `Card: https://...` (publish), or as an empty area (browser card). | Image and browser probes. The browser card is a `multipart/x-mixed-replace` stream that was still loading; unconfirmed. | AnythingLLM's card rendering; `browser.live`. |
| B6 | The agent does not know about AnythingLLM's native memories and says it has none; `rag-memory` searches documents and cannot delete. The probe took 2 m 41 s. | `memories` table has 4 rows; the agent searched documents, summarised a file, listed the sandbox and the sites. | System prompt "Saved memories may be stale" line; add a memory skill (see Gaps). |
| B7 | An uploaded CSV reaches the sandbox only by being pasted into the script. A large file would overflow the context. | CSV probe: the `run-code` call carried all 45 rows inline. | sandbox: copy chat attachments into `/work`. |
| B8 | Replies take 20 s to 2 m 41 s, and the footer shows only the last generation's time (48.0 s for a reply that took about 2 min). | News and memory probes. | Model choice; tool-call count; footer is AnythingLLM's. |
| B9 | A scheduled job's stored result keeps the model's `<think>` blocks. | `scheduled_job_runs` rows 44 and 45 (Daily News Page). | Strip thinking before saving, or the job's model setting. |
| B10 | The log shows "Client took too long to respond, chat thread is dead after 300000ms" twice during the session. | `journalctl --user -u anythingllm`, 13:54:04 and 13:55:21. | Unknown; likely threads left open in the browser while a long run streamed. |
| B11 | research-runner logs `WARNING discarding data: None` at the start of a run that finished ok. | `dr-b40f17e0`, 13:57:19. | research-runner. |
| B12 | A new thread's prompt is missing from the page text while the agent runs, then appears when it finishes. Screenshots show it, so this affects only text extraction and screen readers. | News and Pomodoro probes. | AnythingLLM UI. |
| B13 | The agent tells the user to download a file from `/project` or run a local server, and the UI offers neither. | Pomodoro probe. | Gaps: private file viewer, interactive artifacts. |

## Left behind

- **AnythingLLM:** 11 threads named `[Scratch test] ...` in the AnythingLLM workspace, and one research-site entry, "SearXNG vs Brave Search API vs Tavily for a personal AI agent (2026)". Delete the entry with `delete-entry` if it is not wanted.
- **Muse:** 11 side chats from the probes:
  - List of assistant tools and capabilities
  - Three biggest tech news stories
  - Simulate dice rolls and plot distribution
  - Create Pomodoro timer web page
  - Schedule scratch test reminder
  - Image of fox in Stockholm
  - Check Hacker News top story
  - How memory is organized
  - Compare SearXNG, Brave Search and Tavily
  - Summarize September spending and budget
  - Plan Saturday in Stockholm
- **Removed:** the test reminder and the Pomodoro artifact in Muse; scheduled job #9, `/public/algorithms/dice-sums`, `/public/algorithms/fox-cafe` and `/project/pomodoro-timer.html` in AnythingLLM.
