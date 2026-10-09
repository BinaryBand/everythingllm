# TODO

Work that's been looked into but not done yet. Remove an entry when it lands.

## Apps after the starter

Apps are now pages the agent writes, with data the host keeps (2026-10-09; README, "Apps"). Left:

- **Starters for what users make often** (notes, a table, a tracker), once a few agent-written apps show what's worth keeping: each only a page in `apps/starters/`, never code in the runner.
- **Libraries without web access:** a page loads nothing from other hosts, so an editor or a grid library has to be in the workspace's `/public`, which needs web access to fetch; a read-only folder of vetted ones beside the themes would spare that.
- **Sharing an app between workspaces:** copying a page from `/shared/<workspace>/apps/` works today by hand.

## Checks left from 2026-10-08's changes

Everything that could be done without a person is live: the runners, gateway, egress proxy and static server run the new code, the installed units match the templates, the skills, the default prompt and its version are deployed, and the apps' routes answer. Left:

- **The machine's route for the article writer.** `tailscale serve --https=8445 --set-path=/news/write off` (nothing listens on :8448 now).
- **Apps in AnythingLLM's own chat and in Nilson:** the agent's side was tested on 2026-10-09 through the developer API (a list change, and a notes app and a reading log it wrote, driven in Chromium: saves, a second tab's sync, reloads, the card's `where`). Left: the card showing and moving in the web UI's chat and in Nilson.
- **Each workspace's prompt.** career, cloud, algorithms, education and parity-scratch have their own copy of the block (`uv run hostctl health` lists them); `update-prompt` in each, the user's call.
- **Checks that need a person in a chat:** `sandbox-access` turning web on shows AnythingLLM's approval prompt, and a "no" or an always-allowed skill leaves it off; a deep research run from a UI chat lands in the workspace's documents with a notice quoting its findings; `show-image` puts a chart in the chat.
- **The Nilson app** no longer gets a report link (ntfy's `Click`) when a research run ends, since there's no report page; check what it shows instead.

## Finish the service-container rollout

The relay and research-runner went live in their containers on 2026-10-07 (see the proposal's status and the README's "Service containers"). The checks a script couldn't make, and the cleanup once they've held:

- **Checks that need a person.**
  - Run one Nilson chat to the end through the relay, with its ntfy notice.
- **Once the containers have run for a week:** delete the old host venvs `~/.local/share/everythingllm/venvs/{relay,research}`, and the leftover `browser-net` network (`podman network rm browser-net`).

## Typing in the take-over view: what's left

Built 2026-10-08: the view's "Type into the browser" field (text and password, sent as text through the driver, with Enter, Tab and Backspace), focused on a phone at take-over, and the handoff naming Google, GitHub, Microsoft or Apple on their sign-in pages. Left:

- **Check by hand.** Gboard on an Android phone (the keyboard opens at take-over, and text lands in the field), a password manager's fill of the view's password field, and a Swedish keyboard on Linux, at `https://the-internet.herokuapp.com/login` in a scratch workspace.
- **The desktop keymap.** Type `@`, `{` and å, ä, ö over VNC on a Swedish layout; if they arrive wrong, start x11vnc with `-xkb` (and check Xvfb's keymap) so keys map by symbol (`host/containers/browser/entrypoint.sh`). The field sidesteps it meanwhile.
- **VNC input while the agent has the browser.** Only noVNC's `viewOnly` in the page stops it: the take-over view's websocket bridge passes keys and clicks whoever has the browser. The new typing routes refuse on the runner; VNC would need the bridge to drop RFB key and pointer messages unless `control == "user"`.

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
