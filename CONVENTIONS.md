# ShimShim conventions

Rules earned by incidents. Each one has a scar attached — that's why it's here.

## Card quality

### Stale departure club in the summary is a recycled deal
`brief_problems` rejects any `kind="deal"` card whose summary says the
player is leaving club X (`"from Napoli"`, …) while `from_club` is a
different club Y. Suitor phrasing (`"proposal from Arsenal"`) is ignored.

*Incident (2026-09-07):* an Aug 2024 "medical booked / Al-Ahli" Osimhen piece
(Napoli era) was resurfaced by an SEO mirror with a fresh pubDate. Lean mode
filed it as Here we go with `from_club=Galatasaray` (current club) but kept
"from Napoli" in the summary — a fake **Galatasaray → Al-Ahli** here-we-go.
Osimhen never left Galatasaray. Pinned by `test_rumour_gates.py`.

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

### A move the infobox dates to an earlier year is a recycled story
`oracle_sanity_check`: when the oracles already place the player at the
destination, `wikipedia_tenure` (raw infobox `years`/`clubs` rows) decides
whether that's a fresh completion or old news — `is_recycled_move` fires only
when the origin spell **closed before this year** and the destination spell
**opened before this year**. Loan returns (origin spell ends this year) and
re-signings (new open spell this year) stay clear. `verify_all.py` runs the
same check on cards ≤3 days old as a backstop.

*Incident (2026-09-13):* an SEO mirror re-dated a **8 Jul 2023** "Milan and
Chelsea agree deal for Pulisic" piece; newsdata's pubDate was fresh, so the
3-day age gate passed, the summary matched `from_club` (both stale together),
and the oracles showing him at Milan looked like confirmation. Carded
**Chelsea → AC Milan, Completed** three years late. Pinned by
`test_search_upgrade.py`.

### A deal's destination must be named in the source text
`brief_problems(brief, article)` rejects any `kind="deal"` card whose
`to_club` is nowhere in the article's title/description/source
(`club_mentioned`: canonical alias or a distinctive token — "Spurs", "Barça",
"Depor", "Ipswich" — but never a nickname alone).

*Incident (2026-08-27):* an Ipswich Town fan site wrote "his move to **the
Blues** from Bayer Leverkusen" and lean mode carded **Palacios → Chelsea**. The
club came from the model's memory of a nickname, not from the story. He signed
for Ipswich the same day; the wrong card lived 17 days until the sweep saw him
at Ipswich. *Accepted trade-off:* a story that only names a city ("travels to
Florence") drops this poll; the official announcement names the club.
Pinned by `test_rumour_gates.py`.

### A completed move retires the player's other open deals
`append_feed` → `retire_superseded_deals`: when a **Completed** deal lands,
any open (here-we-go) deal for the same player, same origin, *different*
destination is removed — the losing bid collapsed. Onward moves (different
origin), completed cards, and competing here-we-go's are untouched.

*Incident (2026-09-13):* "Ndiaye Everton → Spurs here we go" (Aug 30) was still
live two weeks after "Everton → Man City Completed" (Sep 1) — the journey
upsert only upgraded same-destination cards. Casadó's journey split the same
way via a club alias ("Depor" vs "Deportivo A Coruna", now one canon).
Pinned by `test_journey.py`.

### "Completed but still at origin" has a benign twin: the loan-back
`verify_all.py` → `origin_disposition`: a Completed card whose player 2+
oracles still place at the origin is a **flag** (over-staged, or collapsed
after being carded Completed) *unless the card itself says he was loaned back*
(`is_loan_back`: "loan him back", "remain with", "return to X on loan") — then
it's a note. The card carries the tell; the oracles can't.

*Incident (2026-09-13):* Brughmans → Liverpool (signed, loaned straight back to
Genk) was flagged in the same breath as two real collapses that had been carded
Completed from "done deal" prose — Camara → Chelsea (Monaco pulled out after
the deadline) and Oosterwolde → Roma (failed medical). Pinned by
`test_verify_sweep.py`. *Still open:* a news article can upgrade a journey to
Completed on "agreed terms" wording in lean mode; the daily sweep's 10-day
origin flag is the backstop that caught both.

### The solidity sweep must not cry wolf on onward loans
`verify_all.py` flags a card when 2+ oracles place the player at a club that's
neither the card's origin nor destination. That reading has two causes, and the
sweep must tell them apart (`elsewhere_disposition`):

- **Completed deal** + player now elsewhere = the "signed, then loaned out"
  chain — the destination genuinely happened; the oracle just shows the onward
  loan. **Downgraded to a note, not alerted.** Silent when the feed already
  records the onward move (`has_onward_move`); printed otherwise. Only a
  completed arrival *can* be followed by an onward loan — that's the tell.
- **Unfinished move** (rumour / "here we go") + player elsewhere = the move
  likely collapsed or redirected. **Still a real flag.**

*Incident (2026-08-30):* the sweep flagged 6 deals; web-checking all six showed
only one wrong (Suzuki's "Parma → PSG here we go", collapsed — he signed for
Aston Villa). The other five were benign — four completed-deal onward loans
(Detourbet→Monaco, P.Charles→QPR, Openda→Lyon, Cuenca→Gijón) and a João Mário
name collision. Alerting on all six equally trained the eye to ignore the alert.
*Accepted trade-off:* a genuinely-wrong **completed** deal now downgrades to a
note too — irreducible without loan metadata the free oracles don't expose. The
loud flag is reserved for the unfinished-move class, which is where the real
error (Suzuki) actually lived. Pinned by `test_verify_sweep.py`.

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
