"""The services the audit and `make health` look at. Nothing is imported here, so the MCP
server in the container can name them without loading the checks."""

# Services whose logs we read, by the journal field that names them: our containers
# and our host user units.
WATCHED = {
    "systemd-anythingllm": ("CONTAINER_NAME", "AnythingLLM"),
    "systemd-searxng": ("CONTAINER_NAME", "SearXNG"),
    "systemd-static_agent": ("CONTAINER_NAME", "pages site (Caddy)"),
    "podcasts-web.service": ("_SYSTEMD_USER_UNIT", "podcasts-web"),
    "podcasts-sync@_all.service": ("_SYSTEMD_USER_UNIT", "podcast sync"),
    "podcasts-transcribe.service": ("_SYSTEMD_USER_UNIT", "podcast transcripts"),
    "news-audio.service": ("_SYSTEMD_USER_UNIT", "Daily News read aloud"),
    "sandbox-runner.service": ("_SYSTEMD_USER_UNIT", "code sandbox runner"),
    "sandbox-proxy.service": ("_SYSTEMD_USER_UNIT", "code sandbox PyPI proxy"),
    "research-runner.service": ("_SYSTEMD_USER_UNIT", "deep-research runner"),
    "podcasts-runner.service": ("_SYSTEMD_USER_UNIT", "podcasts runner"),
    "sites-runner.service": ("_SYSTEMD_USER_UNIT", "sites runner"),
    "audit-runner.service": ("_SYSTEMD_USER_UNIT", "audit runner"),
    "relay.service": ("_SYSTEMD_USER_UNIT", "Nilson relay"),
}

# Host services the MCP servers and skills hand work to (<folder>-runner.service), by the
# folder their socket is in under storage/everythingllm/.
RUNNERS = {
    u.removesuffix(".service"): u.removesuffix("-runner.service")
    for u in WATCHED
    if u.endswith("-runner.service")
}
