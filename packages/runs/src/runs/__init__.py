"""Long runs a host service holds for its callers (deep research, delegations): a run's
state with long-poll waiting and slots (runs.service), its record when it ends (runs.runlog)
and its live progress card (runs.live). A service configures them; nothing here knows what
a run does."""
