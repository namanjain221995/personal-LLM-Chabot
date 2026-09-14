"""Helpers for the TechSara OpenAI-SDK conformance suite.

Nothing in here talks to a server on import. `config` reads the target,
`features` decides which planned features the target has built, `pacing`
keeps a run inside a deployment's advertised request limit, `media` makes the
tiny PNG/WAV fixtures in memory, and `sse` reads a raw event stream with
timings.
"""
