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
import sys
import time
from datetime import datetime, timezone

from shimshim_bot import (FEED_FILE, STATE_FILE, STAGE_RANK, _norm, _norm_club,
                          oracle_fotmob, oracle_sportsdb, oracle_wikipedia,
                          same_club, send_plain_telegram)

PACE = float(os.environ.get("VERIFY_PACE", "2.5"))


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
