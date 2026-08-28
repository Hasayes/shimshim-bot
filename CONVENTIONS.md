# ShimShim conventions

Rules earned by incidents. Each one has a scar attached — that's why it's here.

## Card quality

### A rumour must know where the player currently plays
`brief_problems` rejects any `kind="interest"` card whose `from_club` is
unknown (`""`/`"—"`) — **"rumour without origin club."**

*Incident (2026-08-28):* under lean mode (no web-verify) two misparsed
rumours reached the live feed and had to be pulled by hand — a lone `"Enzo"`
(really Enzo Fernández of Chelsea) and `"Franck Kessie"` carrying a stale
Juventus link (he plays for Al-Ahli). Both had a blank `from_club`. The gate
only demanded an origin club for *deals*, so blank-origin rumours passed.
A genuine rumour always names the player's current club; a blank origin is
the tell for the single-name / stale-link misparse class.
Pinned by `test_rumour_gates.py`.

## Lean mode's accepted gap — and its backstops

`VERIFY_NEWS` / `VERIFY_INTEREST` are **off** by default (cost / "free of
charge"). That means cheap structural gates can't catch two classes of wrong
card, and we accept that in exchange for near-zero spend:

- **Stale summary prose** — the structured fields are right but the summary
  text is out of date (e.g. Matheus Cunha's card called him "Wolves' Cunha"
  though he's been at Man Utd since 2025). `from_club` was correct, so no
  gate or oracle fires.
- **A done deal filed as a rumour** — an already-agreed move ("here we go")
  summarised as "in talks" (e.g. Honest Ahanor, deal agreed). Structurally a
  valid-looking interest card.

Backstops, cheapest first:
1. `brief_problems` structural gates (free, at publish time).
2. `oracle_sanity_check` — free FotMob/TheSportsDB/Wikipedia squad check
   (`ORACLE_SANITY=1`): drops interest in a club the player already plays for,
   fixes inverted `from_club`, upgrades stale "Completed" deals.
3. `verify_all.py` — free daily solidity sweep over the live feed.
4. **The only fix for stale-prose / done-deal-as-rumour is a paid verify
   pass.** Set repo Variable `VERIFY_INTEREST=1` (Sonnet + web search) to
   close it for a busy window — a deliberate cost decision, not a default.

## Working rules

- **Branch → dry-run → merge.** `main` is stable. Develop on a feature
  branch; validate a bot-behaviour change with `gh workflow run poll.yml
  -f dry_run=true` before merging.
- **Match the bot's own JSON writer** when hand-editing `docs/feed.json`:
  `json.dumps(cards, indent=1)` (default ASCII). Anything else produces a
  huge cosmetic diff.
- **Every mistake runs the self-learning loop:** fix the instance *and* build
  the prevention (a guard, a red-green regression test, a rule here) — never
  fix alone.
