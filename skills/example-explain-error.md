---
name: explain-error
description: Explains a pasted error message or traceback in plain language and suggests likely fixes.
triggers: [explain this error, what does this error mean, why am i getting this error, decode this traceback]
mode: keyword
---

# explain-error

When the user pastes an error message, traceback, or stack trace and asks what it means:

1. Identify the actual exception type and the real root line - not just the last line of the traceback, which is often just where the failure surfaced, not where it started.
2. Explain in plain language what went wrong. Assume the user knows how to code, but not this specific error.
3. Suggest 1-2 concrete, likely fixes based on the error type - not a generic "check your syntax" checklist.
4. If the error message alone isn't enough to diagnose (e.g. a bare `KeyError` with no context), say so honestly rather than guessing - ask for the surrounding code instead.
