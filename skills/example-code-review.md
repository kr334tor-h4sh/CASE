---
name: code-review
description: Reviews a piece of code the user shares for bugs, unclear naming, and missed edge cases - a quick second pair of eyes before committing. Matched by meaning, not fixed phrases, so "can you check this over", "does this look right", or "sanity check this function" all work.
mode: semantic
---

# code-review

When asked to look over, review, or sanity-check a piece of code:

1. Read the whole snippet before commenting - don't react to the first line in isolation.
2. Prioritize real correctness issues (bugs, edge cases, security) over style preferences.
3. Call out anything genuinely confusing (unclear naming, a function doing too much) but don't nitpick formatting a linter would already catch.
4. If the code looks fine, say so plainly - don't invent issues just to seem thorough.
5. Keep it to the 2-3 things that actually matter, not an exhaustive line-by-line audit, unless a full audit is specifically requested.
