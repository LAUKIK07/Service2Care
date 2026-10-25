# How we used Kiro to build Signal2Care

**Team:** Laukik Rahul Khade, Arya Mahadik
**Track:** Healthcare & Biotech
**Project:** Signal2Care — an iMessage health assistant with layered long-term memory and a safety-gated wearable alert monitor. All data is synthetic; it is non-diagnostic and gives no medical advice.

## Kiro tool used

We built entirely in the **Kiro IDE** (Autopilot and Supervised modes). We deliberately kept the surface small and went deep on one capability rather than spreading thin: **steering docs**, which we treated as the project's constitution. Everything Kiro produced ran through the rules in those files on every single turn.

## Steering: our highest-leverage strategy

Healthcare is the wrong domain to "vibe code." A model that improvises a medication dose or invents a number is not a bug, it's a safety failure. So before writing feature code, we encoded the project's hard constraints as steering so Kiro could not drift away from them.

We used three files with different inclusion modes:

- **`orchestration.md`** (`inclusion: always`) — the working agreement loaded on every request. It is deliberately short so it costs little context each turn.
- **`signal2care-analysis.md`** (`inclusion: manual`) — a long evidence ledger and track record, pulled in only when needed so it doesn't bloat every prompt.
- **`architecture.md`** (`inclusion: manual`) — the normalized target architecture and build order.

Three strategies in that steering made the biggest difference:

**1. A claim-tagging discipline.** We required every factual claim Kiro made to carry a tag: `[VERIFIED]` (checked against a file, command, or primary source with a link), `[PDF]`, `[USER]`, `[INFERRED]`, or `[UNVERIFIED]`. This single rule changed Kiro's behavior the most. Instead of confidently guessing API shapes or rate limits, it went and checked — WHOOP's API (no continuous heart rate, no sandbox), Linq's sandbox limits, DeepInfra model capabilities — and marked anything it couldn't confirm as a spike to run, not a fact to build on.

**2. A model-orchestration routing policy.** We wrote a routing table that assigns work by *cost of being wrong and reversibility*, not task size: Opus-tier for locking contracts, safety, consent, and privacy design; Sonnet for implementation against locked contracts; Haiku for mechanical, diff-checkable work; and **no model at all** for the safety gate, STOP handling, and numeric computation. One rule we're proud of: on safety-path code, the reviewer is never a lower tier than the author. This kept credit spend sane without letting a cheap model near the dangerous paths.

**3. Deterministic-by-code safety as a standing rule.** Steering declared that the safety gate, consent/STOP, and output validation are always plain code, never an LLM or the Jev decision model, and that numbers reach the user only from typed evidence records. Because this lived in always-on steering, every feature Kiro built respected it by default — the guardrails in `safety.py`, `guard.py`, and `alerts.py` all enforce limits in code so a prompt can't be talked around.

## Steering vs. vibe coding

We used both, intentionally. Early exploration (trying a Linq call, probing which DeepInfra models accept images) was vibe-coded to learn fast. But anything that touched safety, the decision stack, or the evidence contracts went through steering-first development: the rule was written down, then the code was built against it, then reviewed at a tier no lower than the author. The steering approach caught drift that vibe coding would have shipped — for example, Kiro initially wanted to let the LLM phrase a question with its own numbers; the steering rule forced numbers to come from code and the model to only word the question, which is now how `alerts.py` works.

## What we did not use, honestly

To keep this write-up trustworthy: we did not set up agent hooks, spec workflows, MCP servers, property-based testing, or Kiro powers on this project. Our bet was that deep, disciplined use of steering in a safety-critical domain would show more understanding than a shallow tour of every feature. The `.kiro/` folder in the repo contains exactly what we used, nothing staged for show.

## Result

Steering turned Kiro from a fast code generator into a collaborator that refused to invent health claims, cited its sources, routed expensive reasoning only where mistakes were costly, and kept every safety rule enforced in code. For a healthcare project judged on both implementation and responsible design, that discipline was the whole game.
