---
name: daily-standup
description: Turns recent conversation/memory context into a short standup-style summary - what got done, what's blocked, what's next. Manual only - run it explicitly (the 🎯 button in chat) rather than having it fire on its own, since "what's my status" is too easy to trigger by accident.
mode: manual
---

# daily-standup

When run, produce a short standup-style summary in three parts:

- **Done**: what was actually completed recently, based on real context (memory search or recent chat history) - not a guess.
- **Blocked**: anything genuinely stuck or waiting on a decision.
- **Next**: the most obvious next step or two - not a full roadmap.

Keep the whole thing to a few lines per section - this is a quick status check, not a report. If there isn't enough real context to fill a section honestly, say so rather than padding it out.
