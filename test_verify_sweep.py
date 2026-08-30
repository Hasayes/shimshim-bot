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

print("test_verify_sweep: OK")
