#!/usr/bin/env python3
"""Regression: one journey, one card — even when the destination changes.

Incident (2026-09-13 sweep): two stale open deals survived their player's real
move for two weeks:
  - Ndiaye "Everton -> Tottenham here we go" (Aug 30) stayed live after
    "Everton -> Manchester City Completed" (Sep 1). The journey upsert only
    upgrades a lower-stage card with the SAME destination; a different
    destination appended a second card and left the losing bid standing.
  - Casadó "Barcelona -> Depor here we go" (Romano) and "Barcelona -> Deportivo
    A Coruna Completed" (news) were the same journey split by a club alias.

Fix: a Completed deal retires the player's other open deals from the same
origin (retire_superseded_deals); "Depor"/"Deportivo" canonicalise to A Coruña.
"""
import os
os.environ.setdefault("NEWS_API_KEY", "x")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "x")
os.environ.setdefault("TELEGRAM_CHAT_ID", "x")
os.environ.setdefault("ANTHROPIC_API_KEY", "x")

import shimshim_bot as s


def brief(**k):
    d = dict(kind="deal", stage="Here we go", player="Iliman Ndiaye",
             position="FW", age="26", from_club="Everton", to_club="Tottenham",
             fee="—", style="—", fit="—", source="Fabrizio Romano", summary="x")
    d.update(k)
    return s.TransferBrief(**d)


def article(i):
    return {"id": f"a{i}", "source": "test", "url": f"u{i}", "title": f"t{i}"}


# --- Depor alias: Romano's short name and the news outlet's long name agree --
assert s._norm_club("Depor") == s._norm_club("Deportivo A Coruna") \
    == s._norm_club("RC Deportivo de La Coruña") == "deportivo la coruna"
assert s.same_club("Depor", "Deportivo")
assert s._norm_club("Deportivo Alavés") != "deportivo la coruna"

# --- Redirected move: Completed to B retires the open deal to A --------------
feed = []
assert s.append_feed(article(1), brief(), None, feed) == "new"          # HWG Spurs
assert s.append_feed(article(2), brief(stage="Completed", to_club="Manchester City",
                                       fee="£65m"), None, feed) == "new"
assert [(c["to_club"], c["stage"]) for c in feed] == [("Manchester City", "Completed")]

# --- Same destination still upgrades in place (unchanged journey behaviour) --
feed = []
s.append_feed(article(1), brief(player="Marc Casadó", from_club="Barcelona",
                                to_club="Depor"), None, feed)
r = s.append_feed(article(2), brief(player="Marc Casado", stage="Completed",
                                    from_club="FC Barcelona",
                                    to_club="Deportivo A Coruna"), None, feed)
assert r == "upgraded" and len(feed) == 1 and feed[0]["stage"] == "Completed"

# --- An onward move is a different journey and must NOT be retired ----------
# "City -> QPR here we go" (onward loan) is open when the "Sheffield Wednesday
# -> City Completed" card arrives late: different origin, different journey.
feed = [
    {"id": "x", "kind": "deal", "stage": "Here we go", "player": "Pierce Charles",
     "from_club": "Manchester City", "to_club": "QPR"},
]
retired = s.retire_superseded_deals(
    feed, brief(player="Pierce Charles", stage="Completed",
                from_club="Sheffield Wednesday", to_club="Manchester City"))
assert retired == [] and len(feed) == 1

# --- Two competing here-we-go's coexist until one completes -----------------
feed = []
s.append_feed(article(1), brief(), None, feed)
s.append_feed(article(2), brief(to_club="Manchester City"), None, feed)
assert len(feed) == 2

# --- Completed cards are never retired (re-transfer = new journey) -----------
feed = [
    {"id": "x", "kind": "deal", "stage": "Completed", "player": "Iliman Ndiaye",
     "from_club": "Sheffield United", "to_club": "Marseille"},
]
assert s.retire_superseded_deals(
    feed, brief(stage="Completed", to_club="Manchester City")) == []

print("test_journey: OK")
