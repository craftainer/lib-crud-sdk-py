# tests/unit/concurrency/

A deterministic interleaving harness for the cache layer and the SSE subscriber.

- `world.py` — the fakes (a transport whose responses are released step by step, an event
  source that emits frames on demand, a store whose writes can be held, a manual clock) and the
  invariants checked after every step.
- `test_interleavings.py` — a table of scripted interleavings (one per race fixed in review
  rounds) and a seeded exploration of random ones.

Nothing sleeps and no thread races: everything runs on one event loop, and a step ends only when
the loop has settled. A failing run prints its step trace, so it can be pasted into the table.
