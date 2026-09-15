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
  - FLAG: oracles agree the player is at a club nowhere on the card AND the
    card's own move is still unfinished (rumour / "here we go") — the move
    likely collapsed or redirected (e.g. Suzuki's "Parma -> PSG here we go"
    that fell through; he signed for Aston Villa).
  - NOTE (not alerted): a *completed* deal whose player 2+ oracles place at a
    third club is the "signed, then loaned out" chain (Detourbet -> Monaco,
    P.Charles -> QPR, Openda -> Lyon, Cuenca -> Gijon). The card's destination
    genuinely happened; the oracle just shows the onward loan. Only an onward
    loan can follow a completed arrival, so these are downgraded from flags.
    If the feed already records the onward move it is silent; otherwise printed.
Flags are sent to Telegram in one summary message (only when any exist).
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

from shimshim_bot import (FEED_FILE, STATE_FILE, STAGE_RANK, _norm, _norm_club,
                          oracle_fotmob, oracle_sportsdb, oracle_wikipedia,
                          same_club, send_plain_telegram)

PACE = float(os.environ.get("VERIFY_PACE", "2.5"))


def _same_player(a, b):
    """Loose player-name match: surnames must agree (accent-folded); a first
    name is compared only when both sides supply one ('Suzuki' vs 'Zion
    Suzuki' still matches, 'Andrés Cuenca' vs 'David Cuenca' does not)."""
    na, nb = _norm(a).split(), _norm(b).split()
    if not na or not nb or na[-1] != nb[-1]:
        return False
    if len(na) > 1 and len(nb) > 1:
        return na[0] == nb[0]
    return True


def has_onward_move(feed, player, dest, later_club):
    """True when the feed already records `player` moving dest -> later_club.

    That onward card is proof the oracle's 'current club' is just a later loan
    from a genuine arrival at `dest`, not evidence the `dest` move was wrong.
    """
    for c in feed:
        if not _same_player(c.get("player", ""), player):
            continue
        if same_club(c.get("from_club", ""), dest) and \
           same_club(c.get("to_club", ""), later_club):
            return True
    return False


_LOAN_BACK_RE = re.compile(
    r"loan\w*(?:\s+\w+){0,3}\s+back\b|"          # loan back / loan him back
    r"\bstay(?:s|ing)? (?:at|with)\b|"
    r"\bremain(?:s|ing)? (?:at|with)\b|"
    r"\breturn(?:s|ing)? to \S+ on loan\b|"
    r"\bimmediately loaned\b"
)


def is_loan_back(card):
    """True when the card itself says the player was signed and loaned back.

    'Signed, then loaned back to the seller' is the one completed move where
    the oracles are RIGHT to keep showing the origin club (Brughmans ->
    Liverpool, loaned back to Genk, 2026-09-01). The card text carries the
    tell, the oracles don't — so read the card.
    """
    text = " ".join(str(card.get(k) or "") for k in ("summary", "fee", "title"))
    return bool(_LOAN_BACK_RE.search(text.lower()))


def origin_disposition(is_completed, loan_back):
    """Decide how to treat a completed card whose player still shows at origin.

      - card says loaned back -> 'note' (expected: oracle shows the loan club)
      - otherwise             -> 'flag' (over-staged, or collapsed after
                                 'Completed' — Camara, Oosterwolde 2026-09)
    """
    if not is_completed:
        return "ok"
    return "note" if loan_back else "flag"


def elsewhere_disposition(is_completed, onward_in_feed):
    """Decide how to treat a card whose player 2+ oracles place at a third club.

      - not a completed deal            -> 'flag' (move collapsed/redirected)
      - completed + onward move in feed -> 'ok'   (documented chain, silent)
      - completed, onward not recorded  -> 'note' (likely onward loan)

    Only a completed permanent arrival can be followed by an onward loan, so a
    still-unfinished move whose player is already elsewhere is a real problem.
    """
    if not is_completed:
        return "flag"
    return "ok" if onward_in_feed else "note"


def main():
    feed = json.loads(FEED_FILE.read_text())
    state = json.loads(STATE_FILE.read_text())
    now = datetime.now(timezone.utc)
    upgrades, flags, notes = [], [], []

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
            if origin_disposition(is_completed, is_loan_back(c)) == "note":
                notes.append(f"{player}: {c['to_club']} deal stands; loaned "
                             f"back to {teams[0]} per the card")
                print(f"[NOTE] {player}: completed {c['to_club']} deal, card "
                      f"says loaned back — oracle at {teams[0]} is expected")
            else:
                flags.append(f"• {player}: card says Completed -> {c['to_club']}, "
                             f"but oracles still show {teams[0]}")
                print(f"[FLAG] {player}: completed but oracles show {teams}")
        elif len(elsewhere) >= 2 and same_club(elsewhere[0], elsewhere[1]):
            moved_to = elsewhere[0]
            onward = has_onward_move(feed, player, c["to_club"], moved_to)
            disp = elsewhere_disposition(is_completed, onward)
            if disp == "ok":
                print(f"[OK] {player}: onward move {c['to_club']}->{moved_to} "
                      f"already recorded in feed")
            elif disp == "note":
                notes.append(f"{player}: {c['to_club']} deal stands; now at "
                             f"{moved_to} (likely onward loan)")
                print(f"[NOTE] {player}: completed {c['to_club']} deal, oracle "
                      f"shows {moved_to} — likely onward loan, not flagged")
            else:  # flag: an unfinished move whose player is already elsewhere
                flags.append(f"• {player}: shows at {moved_to}, but the "
                             f"{origin} -> {c['to_club']} move is unfinished "
                             f"— likely collapsed or redirected")
                print(f"[FLAG] {player}: unfinished {origin}->{c['to_club']}, "
                      f"now at {moved_to}")

    FEED_FILE.write_text(json.dumps(feed, indent=1))
    STATE_FILE.write_text(json.dumps(state, indent=2))
    for n in notes:
        print(f"[NOTE] {n}")
    print(f"done: {len(upgrades)} upgraded, {len(flags)} flagged, "
          f"{len(notes)} noted (onward loans, not alerted)")
    if flags and os.environ.get("TELEGRAM_BOT_TOKEN"):
        try:
            send_plain_telegram("🧾 ShimShim daily solidity check flagged:\n"
                                + "\n".join(flags[:12])
                                + "\nAsk Claude to investigate.")
        except Exception as e:  # noqa: BLE001
            print(f"flag alert failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
