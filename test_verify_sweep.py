#!/usr/bin/env python3
"""Regression: the solidity sweep must tell an onward loan from a real error.

Incident (2026-08-30): the daily free sweep flagged 6 deals where 2+ oracles
placed the player at a club not on the card. Web-checking all six showed only
ONE was wrong (Suzuki's "Parma -> PSG here we go", which collapsed — he signed
for Aston Villa). The other five were benign: four completed deals whose player
had since been loaned on (Detourbet->Monaco, P.Charles->QPR, Openda->Lyon,
Cuenca->Gijon) and one name collision. The sweep alerted on all six equally.

Root cause: the "elsewhere" rule couldn't distinguish the "signed, then loaned
out" chain (a completed arrival followed by an onward loan — the oracle just
shows the loan club) from a move that never happened. Only a *completed* arrival
can be followed by an onward loan, so stage is the discriminator; and if the
feed already records the onward step, it is certain, not merely likely.

Fix: elsewhere_disposition() downgrades completed-deal elsewhere readings to a
non-alerting note (silent when the onward move is already in the feed) and keeps
a real flag only for unfinished moves. These assertions pin that decision.
"""
import verify_all as v


# --- _same_player: surname-based, accent- and first-name-tolerant -----------
assert v._same_player("Loïs Openda", "Lois Openda")        # accents folded
assert v._same_player("Suzuki", "Zion Suzuki")             # missing first name ok
assert not v._same_player("João Mário", "Mario Gila")      # different surname
assert not v._same_player("Andrés Cuenca", "David Cuenca")  # surname same, first differs

# --- has_onward_move: is the onward step already documented? -----------------
feed = [
    {"player": "Loïs Openda", "from_club": "RB Leipzig", "to_club": "Juventus"},
    {"player": "Lois Openda", "from_club": "Juventus", "to_club": "Olympique Lyon"},
    {"player": "Andrés Cuenca", "from_club": "FC Barcelona", "to_club": "Como"},
]
assert v.has_onward_move(feed, "Loïs Openda", "Juventus", "Lyon")           # chain present
assert not v.has_onward_move(feed, "Andrés Cuenca", "Como", "Sporting Gijón")  # onward absent
assert not v.has_onward_move(feed, "Loïs Openda", "Juventus", "Roma")       # wrong later club

# --- elsewhere_disposition: the noise-vs-signal decision --------------------
# Completed deal + onward already recorded -> silent, certain chain (Openda).
assert v.elsewhere_disposition(True, True) == "ok"
# Completed deal, onward not yet recorded -> soft note, no alert (CFG loan-out:
# Detourbet, P.Charles, Cuenca).
assert v.elsewhere_disposition(True, False) == "note"
# An unfinished move whose player is already elsewhere -> real flag (Suzuki).
assert v.elsewhere_disposition(False, False) == "flag"
assert v.elsewhere_disposition(False, True) == "flag"

# --- origin_disposition: "completed but still at origin" has a benign twin --
# Incident (2026-09-13): Brughmans -> Liverpool was flagged alongside two real
# collapses (Camara -> Chelsea, Oosterwolde -> Roma). Liverpool DID sign him and
# loaned him straight back to Genk — the oracles were right to show Genk. The
# card text says so; the oracles can't. Read the card, downgrade to a note.
assert v.is_loan_back({"summary": "Liverpool have completed a €35m transfer "
                       "for Lucca Brughmans and plan to loan him back to Genk "
                       "for the season.", "fee": "€35m"})
assert v.is_loan_back({"summary": "Signed on a six-year deal; he will remain "
                       "with Genk until June.", "fee": "—"})
assert v.is_loan_back({"summary": "Deal done.", "fee": "£30m, loaned back"})
assert not v.is_loan_back({"summary": "Chelsea signed Monaco midfielder Lamine "
                           "Camara on Deadline Day.", "fee": "—"})
assert not v.is_loan_back({"summary": "Roma have agreed terms with Fenerbahce "
                           "for Oosterwolde's transfer.", "fee": "€18m option"})
assert v.origin_disposition(True, True) == "note"    # Brughmans: expected
assert v.origin_disposition(True, False) == "flag"   # Camara / Oosterwolde
assert v.origin_disposition(False, False) == "ok"    # rule is for completed only

print("test_verify_sweep: OK")
