# Conductor integration boundary

These JSON files are **reviewable application plans, not Netflix Conductor API
payloads**. They are explicitly marked `deployable: false`. No server version,
SDK contract, authentication mechanism, or organization deployment conventions
were supplied. There are no invented deployment commands.

Before deployment, obtain those details, implement the internal
`ConductorAdapter` port with the verified client, map these plans to that
version's schemas, and submit the actual definitions through the normal
organization deployment process. Configure scheduling separately. The real
client must acknowledge tasks, apply the configured timeouts and retry/backoff
policy, and continue polling after errors. Authentication/consent expiration
requires reauthorization, not automatic repeated retries.

The working demo uses an in-process fake queue. The persistent worker requests
one fake sync per ISO week, at its first poll, and safely replays a request after
restart using encrypted receipts. Transient failures retry with bounded
exponential backoff. The fake does not simulate a remote lease or server timeout.
`finance --demo worker --once` uses the same execution path for a foreground
permission check. It never connects to a server.

launchd keeps the worker alive; it is not the production weekly scheduler.
The user must be logged in and the Mac awake for a LaunchAgent to poll. On wake,
the demo runs on its next poll. Real server timeouts must accommodate sleep and
the maximum bootstrap duration. Operational outputs and rotating logs contain
only counts, timestamps, sanitized error codes and hashed execution IDs.
