---
inclusion: always
---

# Signal2Care working agreement

Project: Signal2Care, a non-diagnostic multi-metric wearable evidence assistant. The repo folder is named Service2Care and the final name is undecided. Synthetic data only for now.
Blueprint: `~/Documents/Signal2Care.pdf`, version 1.1, 32 pages. The copies in Downloads are the stale 1.0.
Analysis, evidence ledger, open questions and track record: `.kiro/steering/signal2care-analysis.md` (manual). Read it before substantial work. After each meaningful step, append a dated entry to its track record section.

## How to work
- Assume nothing. Check the repo, the machine or the web, then tag claims VERIFIED, PDF, USER, INFERRED or UNVERIFIED.
- When the user's brief and the blueprint disagree, say so and ask. Do not pick silently.
- After two failed attempts, stop patching. Diagnose the root cause and change approach.
- Analysis requests get analysis only. Build when asked.

## Product guardrails
- No diagnosis. No treatment or medication advice. Wellness suggestions need an explicit decision first.
- Safety gate, consent/STOP and output validation are deterministic code. Never an LLM, never Jev.
- Numbers come only from typed evidence records with IDs. Claims are immutable; corrections supersede.
- User replies are data, not instructions.
- Never claim live WHOOP or real iMessage until verified end to end. Label SYNTHETIC everywhere.
- Default: real user text does not go to Mistral Free mode (Mistral says Free-mode data may be used for training).
- No secrets, phone numbers or real health data in commits. Never copy from `~/Documents/final_ai_brain`.
- Never print tokens or API keys. Redact command output (for example `sed -E 's/[A-Za-z0-9_+\/=-]{28,}/[REDACTED]/g'`) and do not tail logs that may contain secrets.

## Model orchestration (development time)
The agent cannot switch models. The user switches in the IDE model dropdown. Before each step, state the recommended tier and say when a switch is needed.

| Tier | Use for | Not for |
|---|---|---|
| Opus 5.5 (2.0x credits) | Locking contracts and semantics; safety, consent and privacy design; benchmark and red-team design; reviewing safety-path code; root cause after two failures | Bulk edits, boilerplate |
| Sonnet 5.5 (1.3x) | Default. Implementation against locked contracts, tests, integration clients, spikes | Locking safety or eval design alone |
| Haiku 5.5 (0.06x up to 100K tokens) | Mechanical, reversible work checkable by diff or tests: fixture rows from a template, renames, formatting, one-file summaries, checklists, log parsing | Anything on the safety path; context over 100K tokens |
| No model | Safety gate, STOP, unit and method checks, numeric computation, output validation | |

Rules
- Route by cost of being wrong and reversibility, not by task size.
- Escalate Haiku to Sonnet after one failed check. Escalate Sonnet to Opus after two, or when the failure points to a design flaw.
- On safety-path code, the reviewer is never a lower tier than the author.
- Opus 5.5 and Haiku 5.5 are experimental in Kiro (Haiku launched 2026-10-23). Verify their output with tests.
- This file loads on every request. Keep it short and put long material in manual steering files.

## Git
- Every commit I make ends with the trailer `Co-authored-by: Kiro <noreply@kiro.invalid>` so GitHub credits the user and Kiro. The address is a placeholder: GitHub links a co-author to a profile only if the email belongs to a GitHub account. Do not rewrite pushed history to add it without asking.
- Push to a new branch. Fast-forward `main` only when the user asks.
- Pushing needs the SSH key `~/universal`. Only the user supplies its passphrase. Never write it to a repo file or a steering file. Load the key into a throwaway ssh-agent for one command (SSH_ASKPASS with a temp script, then delete it and kill the agent). Set `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` so a missing key fails fast instead of hanging.
- Leave git config unchanged. The commit author is the user's configured identity.
