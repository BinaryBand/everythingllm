"""The Nilson relay: a service, in its own container on the host, that owns every chat answer
the Nilson app asks for.

AnythingLLM stops an answer when the client of `stream-chat` disconnects, and saves it to
the thread only when the stream completes, so an answer whose app closes or loses its
network is lost. The relay makes that call itself, streams it to the end whatever its
followers do, keeps the events in SQLite, and lets the app rejoin with Last-Event-ID. It sits
at `/everythingllm/` beside AnythingLLM and takes the client's own AnythingLLM API key, so a
client that finds it there needs nothing else. See the README's "Nilson relay".

- `relay.store`: the runs and their events in SQLite.
- `relay.upstream`: one answer from AnythingLLM's stream-chat, as the relay's events.
- `relay.runs`: starting, following, cancelling and recovering runs.
- `relay.auth`: the key check, against AnythingLLM's own.
- `relay.app`: the HTTP API (Starlette under uvicorn) and its config.
"""
