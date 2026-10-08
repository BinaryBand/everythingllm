"""The patterns of the names the sandbox takes, shared by the runner and the apps server
(sandbox.appsweb), which can't import the runner."""

KEY = r"[a-z0-9_][a-z0-9_-]{0,99}"  # workspace slugs and thread ids
SLUG = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"  # a page's slug, and an app's name
