# TODO

Work that's been looked into but not done yet. Remove an entry when it lands.

## Workspace templates

Important, and next for apps once the list template has been used for a while. Today every app template is the repo's (`packages/sandbox/src/sandbox/apps/`), reviewed with the runner and the same in every workspace. Users will want looks and kinds of app of their own (a habit tracker, a reading log, a family's chores board) without a repo change, and a list duct-taped together per request is what templates replaced, so the way to add one has to keep that consistency.

- **Where they'd live:** a workspace's `/shared/<workspace>/apps/<template>/`, beside its Zola themes, so other workspaces can read and copy one.
- **What has to hold:** a workspace template's page runs under the same pages CSP sandbox as any page; its ops and card can't run on the host as the repo's do (they're Python the runner imports), so either the ops become a declarative schema the runner applies (add/remove/toggle/set on typed fields) and the card a declarative layout `chatimage` draws, or both run in a sandbox container per change, which costs a container start (a second or two) on every tick.
- **Opt-in per workspace,** turned on the way web access is (the user approves it), and a template pinned by its hash when an app is made, so the workspace changing its template doesn't silently change apps already made.
- **The declarative route first:** it keeps a tick instant and the host free of workspace code; the repo's list template would be rewritten in it to prove it.

## Checks left from 2026-10-08's changes

Everything that could be done without a person is live: the runners, gateway, egress proxy and static server run the new code, the installed units match the templates, the skills, the default prompt and its version are deployed, and the apps' routes answer. Left:

- **The machine's route for the article writer.** `tailscale serve --https=8445 --set-path=/news/write off` (nothing listens on :8448 now).
- **Apps, end to end, in a chat:** "put oat milk on the groceries list" makes the app and shows its card; ticking on its page moves the card in the chat; an older tab of the page says to reload.
- **Each workspace's prompt.** career, cloud, algorithms, education and parity-scratch have their own copy of the block (`uv run hostctl health` lists them); `update-prompt` in each, the user's call.
- **Checks that need a person in a chat:** `sandbox-access` turning web on shows AnythingLLM's approval prompt, and a "no" or an always-allowed skill leaves it off; a deep research run from a UI chat lands in the workspace's documents with a notice quoting its findings; `show-image` puts a chart in the chat.
- **The Nilson app** no longer gets a report link (ntfy's `Click`) when a research run ends, since there's no report page; check what it shows instead.

## Make the test suite faster

`uv run hostctl test` takes about 55 s: `pytest` for 785 tests, plus 0.4 s for the skill tests. Measured on 2026-10-07, by package:

| Package | Time | Where it goes |
| --- | --- | --- |
| egress | ~4 s | the tunnel idle tests wait out a 0.3 s limit, 0.8 s each |
| others | \<4 s each | |

- **Parallel runs with pytest-xdist** (`-n auto`, 4 cores here): this might bring the suite to roughly 15 s. First look for timing-sensitive tests that flake under load, and check that no two tests share a fixed port or path.

Tests are a small part of how long sessions take. Run one package's tests while iterating (1--3 s), and the full suite before committing.

## Finish the service-container rollout

The relay and research-runner went live in their containers on 2026-10-07 (see the proposal's status and the README's "Service containers"). The checks a script couldn't make, and the cleanup once they've held:

- **Checks that need a person.**
  - Run one Nilson chat to the end through the relay, with its ntfy notice.
- **Once the containers have run for a week:** delete the old host venvs `~/.local/share/everythingllm/venvs/{relay,research}`, and the leftover `browser-net` network (`podman network rm browser-net`).

## Typing in the take-over view, for SSO and other handoffs

Decided 2026-10-08: SSO ("Continue with Google"), logins the vault doesn't have and 2FA prompts stay with the user through `browser-handoff`. No saved identity-provider logins, and no agent in an SSO popup or on a consent screen. That makes the handoff the path that has to work, and its weak point is typing: on 2026-10-08 the keyboard was hard to use in the view.

- **What failed.** On a phone the keyboard never opened: noVNC's bare RFB core (`static/app.js`) draws a canvas and gives the page no field to focus, so Android shows no keyboard and nothing can be typed at all. The screen being a canvas also means a password manager can't fill it, and nothing carries the clipboard into the browser. Not yet tried: a desktop keyboard. Xvfb runs with its default US keymap and x11vnc without `-xkb` (`host/containers/browser/entrypoint.sh`), so on a Swedish keyboard what's behind AltGr (`@`, `{`, `\`) and perhaps å, ä and ö may arrive wrong.
- **A text field in the view.** A real `<input>` under the screen ("Type into the browser") whose text goes to the page's focused field through the runner and the driver (`page.keyboard.insert_text`), not as key events. Then any layout, a phone's keyboard, paste and a password manager all work. A second field with `type=password` and `autocomplete=current-password`, so a manager offers the login, and buttons for Enter, Tab and Backspace (`page.keyboard.press`).
- **What it must keep.** Only while the user has the browser (`control == "user"`), like VNC input. The text goes from the view to the driver and nowhere else: no reply, log, card or offer line holds it. After the hand-back, what was typed into a password field stays hidden from the agent's reads, as today (`driver.hide`), and a form sent with it is still offered for saving (`capture.js`).
- **On a phone, the field is the fix.** Show it, focused, as soon as the user takes over on a narrow screen, so the keyboard opens without a hunt, with a Keyboard button to bring it back.
- **On a desktop, check before changing.** Type `@`, `{` and å, ä, ö on a Swedish layout; if they arrive wrong, start x11vnc with `-xkb` (and check Xvfb's keymap) so keys map by symbol.
- **A smoother handoff for SSO.** When the chat's page is an identity provider's sign-in (`accounts.google.com`, `github.com/login`, `login.microsoftonline.com`, `appleid.apple.com`), the handoff's reason, the card's strip and the view name the provider: "Sign in to Google, then hand the browser back". The provider's session stays in the workspace's profile until `browser-reset`, so it's once per workspace. One Google session opens every "Sign in with Google" in that workspace to the agent, so the README's "Mind what it's logged into" should say so.
- **Check by hand.** A Swedish keyboard on Linux, Gboard on an Android phone, and a password manager's fill, at `https://the-internet.herokuapp.com/login` in a scratch workspace.

## Gaps the Muse probes showed

From the Muse parity notes (2026-10-07, now in the private notes), after page scripts, chat attachments in `/work`, and the scheduled-job and memories skills. Most of these combine tools the agent already has rather than adding services.

- **Post long jobs back to their chat: what's left.** A chat in AnythingLLM's UI now gets a notice when its research run or delegation ends (`agents.postback`), and a Nilson chat's research run an ntfy notice. Still untold: a delegation from a Nilson chat or Telegram (their invocations carry no thread), and an open tab, which shows the notice only after a reload since AnythingLLM's UI doesn't refresh a thread.
- **One `notify` call.** The relay already posts to ntfy. A skill (or a job tool) that sends a short notice would carry reminders, job results and check-ins to the phone.
- **Pages that keep state.** Sandboxed pages have an opaque origin, so no `localStorage`: a flashcard deck forgets its place. Persistence needs an origin per workspace (a port or subdomain each) or a write-back op through the sandbox runner.
- **Activity feed.** A scheduled job that writes a page from the run logs (research, agents, scheduled jobs), linked by its card.
- **Model choice for agent turns.** Several probe failures (a "one-off" stored as yearly, not knowing the home city that the prompt states) look like `glm-5.3-flash`'s limits. Try a stronger model for agent turns and compare on the probes.
- **The probes as scenario runs.** Run the 11 probes through agents-runner on demand and keep their tool calls and results, so each change is measured against the 2026-10-07 baseline.

## Push notifications for Nilson through UnifiedPush

The relay can post to ntfy (`NTFY_URL`, `NTFY_TOKEN` in `relay.env`) when an answer is ready or fails. Push to Nilson itself would make those two settings unnecessary.

- **Why not AnythingLLM's push.** AnythingLLM's `storage/push-notifications/vapid-keys.json` is browser Web Push. It reaches only a browser that subscribed through AnythingLLM's web UI and its service worker. In single-user mode that subscription would be `primary-subscription.json`, and no browser has subscribed. Nilson is a native Flutter app, so it can't receive this.
- **What would.** UnifiedPush. Nilson registers with a distributor (on Android, the ntfy app or an FCM-backed one; on Linux, a D-Bus distributor such as KUnifiedPush) and sends the relay its endpoint. The relay then sends encrypted Web Push (RFC 8291) there, signed with VAPID.
- **Relay side.**
  - A VAPID key pair of the relay's own, made on first start in `~/.local/share/everythingllm/relay/`, not AnythingLLM's.
  - A `POST /push` route (bearer token, as for the other routes) that keeps the endpoint in `relay.db`.
  - `pywebpush`.
  - The same "Answer ready" and "Answer failed" payloads as now, never the answer.
  - Remove `NTFY_*` from `relay.env`, `relay_env.py` and the README.
- **Nilson side.** The `unifiedpush` Flutter package, plus a distributor installed on each device.
