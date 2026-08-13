#!/usr/bin/env python3
"""Offline checks for the search-accuracy upgrade (no network)."""
import os
os.environ["ORACLE_SANITY"] = "1"
os.environ["VERIFY_NEWS"] = "1"
os.environ["VERIFY_INTEREST"] = "1"
os.environ["WEB_SEARCH_MAX_USES"] = "3"

import importlib
import shimshim_bot as s
importlib.reload(s)

assert s.VERIFY_NEWS and s.VERIFY_INTEREST
assert s.WEB_SEARCH_MAX_USES == 3 and s.ORACLE_SANITY
assert s.same_club("Manchester City", "Man City")
assert not s.same_club("Arsenal", "Chelsea")

s.oracle_current_clubs = lambda player, max_hits=2: {
    "Tijjani Reijnders": ["Manchester City", "Man City"],
    "Filip Jorgensen": ["Chelsea", "Chelsea"],
    "Fake Player": ["AC Milan", "Milan"],
}.get(player, [])

b = s.TransferBrief(
    kind="interest", stage="—", player="Tijjani Reijnders", position="MF",
    age="26", from_club="—", to_club="Manchester City", fee="—",
    style="—", fit="—", source="—", summary="x")
assert s.oracle_sanity_check(b).kind == "none"

b = s.TransferBrief(
    kind="deal", stage="Here we go", player="Filip Jorgensen", position="GK",
    age="23", from_club="Coventry", to_club="RC Strasbourg", fee="Loan",
    style="—", fit="—", source="—", summary="x")
out = s.oracle_sanity_check(b)
assert out.from_club == "Chelsea"

b = s.TransferBrief(
    kind="deal", stage="Completed", player="Fake Player", position="ST",
    age="25", from_club="Bologna", to_club="Manchester United", fee="£1",
    style="—", fit="—", source="—", summary="x")
assert s.oracle_sanity_check(b).kind == "none"

art_news = {"id": "https://x", "source": "Yardbarker"}
art_tg = {"id": "tg:fabrizioromanotg/1", "source": "Telegram · Fabrizio Romano"}
brief = s.TransferBrief(
    kind="deal", stage="Here we go", player="X", position="—", age="—",
    from_club="A", to_club="Arsenal", fee="—", style="—", fit="—",
    source="—", summary="—")
assert s.needs_web_verify(art_news, brief) is True
assert s.needs_web_verify(art_tg, brief) is False
assert "2026" in s._research_system() and "July 2026" not in s._research_system()
print("test_search_upgrade: OK")
