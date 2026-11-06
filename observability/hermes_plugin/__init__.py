"""NOT VERIFIED — DO NOT INSTALL.

Staged documentation example only (Milestone 1). The Hermes plugin
contract (manifest syntax, register(ctx) signature, hook names, payload
fields, concurrency/retry behavior, installed version) is UNVERIFIED
against any installed Hermes build. Milestone 3 validates it against the
exact tagged release and the locally installed version before anything
is shipped. Fallback until then: external agent.handoff.

This stub intentionally does nothing: importing it must never register
hooks, mutate tool args, or touch conversation text.
"""


def register(ctx):
    """Unverified stub. Raises instead of registering anything."""
    raise NotImplementedError(
        "fleet-trace-observer is an UNVERIFIED stub (Milestone 1). "
        "Do not install; Hermes validation happens in Milestone 3."
    )
