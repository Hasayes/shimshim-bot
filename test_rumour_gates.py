#!/usr/bin/env python3
"""Regression: structural gates on rumours and recycled deals.

Incident (2026-08-28): under lean mode (no web-verify) two misparsed rumour
cards reached the LIVE feed and had to be pulled by hand —
  * a lone "Enzo" (really Enzo Fernandez of Chelsea), and
  * "Franck Kessie" carrying a stale Juventus link (he plays for Al-Ahli),
both with a blank from_club ("—"). The completeness gate only demanded an
origin club for DEALS, so blank-origin rumours sailed through.

Root cause: missing guard — an invalid state (interest card with no known
origin) was representable. A genuine rumour always names the player's current
club; a blank origin is the reliable tell for the single-name / stale-link
misparse class that lean mode cannot web-verify after the fact.

Fix: brief_problems now flags "rumour without origin club".

Incident (2026-09-07): a recycled Aug 2024 "medical booked / Al-Ahli" Osimhen
story shipped as Galatasaray → Al-Ahli Here we go. from_club was current
(Galatasaray) but the summary still said "from Napoli". brief_problems now
flags "stale origin in summary".
"""
import shimshim_bot as s


def brief(**k):
    d = dict(kind="interest", stage="—", player="Enzo Fernandez", position="—",
             age="—", from_club="Chelsea", to_club="Arsenal", fee="—",
             style="—", fit="—", source="Romano", summary="x")
    d.update(k)
    return s.TransferBrief(**d)


# The two shipped misparses: interest + blank/empty origin -> rejected.
assert "rumour without origin club" in s.brief_problems(brief(player="Enzo", from_club="—"))
assert "rumour without origin club" in s.brief_problems(brief(player="Franck Kessie", from_club=""))

# A well-formed rumour (known origin) still publishes cleanly.
assert s.brief_problems(brief(player="Ismaila Sarr", from_club="Crystal Palace")) == []

# Deals are governed by their own origin rule, not the rumour one...
deal_probs = s.brief_problems(brief(kind="deal", stage="Completed", from_club="Ajax"))
assert "rumour without origin club" not in deal_probs and deal_probs == []
# ...and a blank-origin deal is still caught by the deal rule (unchanged).
assert "deal without origin club" in s.brief_problems(
    brief(kind="deal", stage="Completed", from_club="—"))

# Recycled medical/here-we-go: summary still says "from Napoli" after
# from_club was updated to the player's real current club (Osimhen → Al-Ahli
# Aug-2024 piece resurfacing in Sep 2026 as Galatasaray → Al-Ahli).
assert "stale origin in summary" in s.brief_problems(brief(
    kind="deal", stage="Here we go", player="Victor Osimhen",
    from_club="Galatasaray", to_club="Al-Ahli",
    summary="Victor Osimhen is set to join Saudi Arabian side Al-Ahli from "
            "Napoli, with a medical reportedly booked."))
# Matching departure club is fine.
assert "stale origin in summary" not in s.brief_problems(brief(
    kind="deal", stage="Completed", player="Hermann Malonga",
    from_club="Paris Saint-Germain", to_club="RC Strasbourg",
    summary="RC Strasbourg have agreed a deal to sign Hermann Malonga from "
            "Paris Saint-Germain on a five-year contract."))
# Suitor phrasing ("proposal from Arsenal") is not a departure club.
assert "stale origin in summary" not in s.brief_problems(brief(
    kind="deal", stage="Completed", player="Viktor Gyokeres",
    from_club="Sporting CP", to_club="Arsenal",
    summary="Galatasaray have rejected a proposal from Arsenal involving "
            "Viktor Gyokeres for Victor Osimhen."))

# Nickname collision (2026-08-27): an Ipswich Town site wrote "his move to the
# Blues" and the card said Chelsea — a club the article never named came from
# the model's memory. A deal's destination must be named in the source text.
ipswich = {"title": "Palacios Travelling For Medical",
           "desc": "Argentina international Exequiel Palacios is reportedly on "
                   "his way to complete his move to the Blues from Bayer "
                   "Leverkusen.", "source": "twtd"}
assert "destination club not named in source" in s.brief_problems(brief(
    kind="deal", stage="Here we go", player="Exequiel Palacios",
    from_club="Bayer Leverkusen", to_club="Chelsea"), ipswich)
# The same text with the club actually named passes.
named = dict(ipswich, desc=ipswich["desc"].replace("the Blues", "Ipswich Town"))
assert s.brief_problems(brief(
    kind="deal", stage="Here we go", player="Exequiel Palacios",
    from_club="Bayer Leverkusen", to_club="Ipswich Town"), named) == []
# Aliases count as naming the club: Spurs, Barça, Man Utd, Depor.
for club, text in (("Tottenham Hotspur", "Spurs agree deal"),
                   ("FC Barcelona", "Barça confirm"),
                   ("Manchester United", "Man Utd sign"),
                   ("Deportivo A Coruna", "Depor reach verbal agreement"),
                   ("PSV", "joins PSV Eindhoven")):
    assert s.club_mentioned(club, text), club
assert not s.club_mentioned("Chelsea", "his move to the Blues")
# Without the article the gate is inert (existing callers unchanged).
assert s.brief_problems(brief(kind="deal", stage="Completed", from_club="Ajax",
                              to_club="Chelsea")) == []
# Interest cards are not gated on suitors (different failure class).
assert "destination club not named in source" not in s.brief_problems(
    brief(kind="interest", from_club="Ajax", to_club="Chelsea"), ipswich)

print("test_rumour_gates: OK")
