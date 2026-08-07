#!/usr/bin/env python3
"""Free full-feed solidity check — zero API spend.

Three free oracles (FotMob, TheSportsDB, Wikipedia) publish each player's
current club. Rules:
  - UPGRADE (safe, automatic): a non-Completed card whose player already
    shows at the destination club -> Completed. Oracles only reflect moves
    after they really happen.
  - FLAG (never auto-downgrade): a Completed card older than 3 days whose
    player still shows at the origin on 2+ oracles — could be an
    over-staged card OR oracle lag; a human (or Claude session) decides.
  - FLAG: oracles agree the player is at a club that appears nowhere on
    the card (wrong destination / move went elsewhere).
Flags are sent to Telegram in one summary message (only when any exist).
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from shimshim_bot import (FEED_FILE, STATE_FILE, _get_json, _norm, _norm_club,
                          STAGE_RANK, send_plain_telegram)

PACE = float(os.environ.get("VERIFY_PACE", "2.5"))


def oracle_fotmob(player):
    try:
        d = _get_json("https://apigw.fotmob.com/searchapi/suggest?lang=en&term="
                      + urllib.parse.quote(player))
        name = _norm(player)
        for g in d.get("squadMemberSuggest") or []:
            for o in g.get("options") or []:
                text = _norm((o.get("text") or "").split("|")[0])
                if name.split()[-1] in text and (len(name.split()) < 2 or name.split()[0] in text):
                    team = (o.get("payload") or {}).get("teamName") or ""
                    if team:
                        return re.sub(r"\s+u\d{2}$", "", team, flags=re.I)
    except Exception:  # noqa: BLE001
        pass
    return ""


def oracle_sportsdb(player):
    try:
        d = _get_json("https://www.thesportsdb.com/api/v1/json/3/searchplayers.php?p="
                      + urllib.parse.quote(player))
        for p in d.get("player") or []:
            if (p.get("strSport") or "") != "Soccer":
                continue
            name = _norm(player)
            if name.split()[-1] in _norm(p.get("strPlayer") or ""):
                return p.get("strTeam") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def oracle_wikipedia(player):
    try:
        hits = _get_json("https://en.wikipedia.org/w/api.php?action=query&list=search"
                         "&format=json&srlimit=3&srsearch="
                         + urllib.parse.quote(f"{player} footballer"))["query"]["search"]
        name = _norm(player)
        titles = [h["title"] for h in hits
                  if name.split()[-1] in _norm(h.get("title", ""))
                  and (len(name.split()) < 2 or name.split()[0] in _norm(h.get("title", "")))]
        if not titles:
            return ""
        d = _get_json("https://en.wikipedia.org/w/api.php?action=query&format=json"
                      "&prop=extracts&exintro=1&explaintext=1&exlimit=1&titles="
                      + urllib.parse.quote(titles[0]))
        pages = (d.get("query") or {}).get("pages") or {}
        extract = next(iter(pages.values()), {}).get("extract") or ""
        m = re.search(r"plays? (?:as [^.]{3,40}? )?for (?:[A-Za-z1]+ )?club ([A-Z][^.,]{2,40})",
                      extract)
        return m.group(1).strip() if m else ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def same_club(a, b):
    if not a or not b:
        return False
    ka, kb = _norm_club(a), _norm_club(b)
    if ka == kb:
        return True
    ta = {w for w in ka.split() if len(w) > 3}
    tb = {w for w in kb.split() if len(w) > 3}
    return bool(ta and tb and (ta <= tb or tb <= ta))


def main():
    feed = json.loads(FEED_FILE.read_text())
    state = json.loads(STATE_FILE.read_text())
    now = datetime.now(timezone.utc)
    upgrades, flags = [], []

    for c in feed:
        player = c.get("player", "")
        if not player or player == "—":
            continue
        if len(_norm(player).split()) < 2:
            continue  # single-name players collide with famous namesakes
        dests = [s.strip() for s in c["to_club"].split(",") if s.strip() not in ("", "—")]
        origin = c.get("from_club", "")
        teams = []
        for oracle in (oracle_fotmob, oracle_sportsdb, oracle_wikipedia):
            t = oracle(player)
            if t:
                teams.append(t)
            time.sleep(PACE)
            if len(teams) >= 2:
                break  # two agreeing oracles is plenty; save calls

        if not teams:
            continue
        at_dest = [d for d in dests if any(same_club(t, d) for t in teams)]
        at_origin = sum(1 for t in teams if same_club(t, origin))
        elsewhere = [t for t in teams
                     if not any(same_club(t, x) for x in dests + [origin])]

        is_completed = c["kind"] == "deal" and c.get("stage") == "Completed"
        age_days = (now - datetime.fromisoformat(c["ts"])).days

        if at_dest and not is_completed:
            dest = at_dest[0]
            c["kind"] = "deal"
            c["stage"] = "Completed"
            c["to_club"] = dest
            key = f"{_norm(player).split()[-1]} -> {_norm_club(dest)}"
            state["deals"][key] = STAGE_RANK["completed"]
            upgrades.append(f"{player} -> {dest}")
            print(f"[UPGRADE] {player} -> {dest} (oracle-confirmed arrival)")
        elif is_completed and not at_dest and at_origin >= 2 and age_days > 10:
            flags.append(f"• {player}: card says Completed -> {c['to_club']}, "
                         f"but oracles still show {teams[0]}")
            print(f"[FLAG] {player}: completed but oracles show {teams}")
        elif len(elsewhere) >= 2 and same_club(elsewhere[0], elsewhere[1] if len(elsewhere) > 1 else elsewhere[0]):
            flags.append(f"• {player}: oracles show {elsewhere[0]}, "
                         f"not on the card ({origin} -> {c['to_club']})")
            print(f"[FLAG] {player}: oracles at {elsewhere[0]}, card {origin}->{c['to_club']}")

    FEED_FILE.write_text(json.dumps(feed, indent=1))
    STATE_FILE.write_text(json.dumps(state, indent=2))
    print(f"done: {len(upgrades)} upgraded, {len(flags)} flagged")
    if flags and os.environ.get("TELEGRAM_BOT_TOKEN"):
        try:
            send_plain_telegram("🧾 ShimShim daily solidity check flagged:\n"
                                + "\n".join(flags[:12])
                                + "\nAsk Claude to investigate.")
        except Exception as e:  # noqa: BLE001
            print(f"flag alert failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
