import math
import random
import os
import sys
import threading

from collections import defaultdict
from typing import Dict, Tuple, Any
from dnd_auction_game import AuctionGameClient

# ---- Dashboard here  ------------------------------------------
FIRST_FAIR_PRICE_PER_POINT = 15.0
WIN_MARGIN = 1.05
MAX_SPEND_FRACTION = 0.95
MIN_BID = 1.0
TIE_JITTER_MAX = 10.0
LEARN_DECAY = 0.8
CAP_BID_AT_GOLD = True
IGNORE_LOW_EV = 0.5
# -----------------------------------------------------------------------------

manual_trigger = False

def _listen_for_manual_trigger():
    """Background stdin listener. Type 'spend' (or 's') to trigger."""
    global manual_trigger
    print("[Manual control] Type 'spend' or just 's' and press Enter "
          "to spend 20% of current gold across the 3 highest-EV auctions.")
    print("[Manual control] Type 'quit' or 'exit' to stop the listener.")
    try:
        for line in sys.stdin:
            cmd = (line or "").strip().lower()
            if cmd in {"spend", "s"}:
                manual_trigger = True
            elif cmd in {"quit", "exit", "q"}:
                print("[Manual control] Listener exiting.")
                break
    except Exception as e:
        print(f"[Manual control] Listener error: {e}")

threading.Thread(target=_listen_for_manual_trigger, daemon=True).start()


_learned_win_prices: Dict[Tuple[int,int,int], float] = defaultdict(float)

def _auction_key(a: Dict[str, Any]) -> Tuple[int,int,int]:
    return (int(a["die"]), int(a["num"]), int(a.get("bonus", 0)))

def _expected_points(a: Dict[str, Any]) -> float:
    # EV of X dY + B = X * (Y+1)/2 + B
    return float(a["num"]) * (float(a["die"]) + 1.0) / 2.0 + float(a.get("bonus", 0.0))

def _ema_update(old: float, new: float, decay: float) -> float:
    if old <= 0:
        return new
    return decay * old + (1.0 - decay) * new

def _estimate_competitiveness(states: Dict[str, Any], n_auctions: int) -> float:
    n_players = max(1, len(states))
    return max(1.0, n_players / max(1, n_auctions))

def _reserve_for_interest(gold: float, bank_state: Dict[str, Any]) -> float:
    if not bank_state:
        return 0.0
    try:
        r = float(bank_state["bank_interest_per_round"][0])
        cap = float(bank_state["bank_limit_per_round"][0])
    except Exception:
        return 0.0

    if r <= 0:
        return 0.0

    # reserve proportionally to interest rate, clamped.
    reserve_frac = min(0.5, 2.0 * r + 0.0)
    target_reserve = min(cap, gold) * reserve_frac
    return max(0.0, min(target_reserve, gold * 0.5))

def _learn_from_prev(prev_auctions: Dict[str, Any]) -> None:
    for _aid, info in (prev_auctions or {}).items():
        try:
            k = _auction_key(info)
            bids = info.get("bids") or []
            if not bids:
                continue
            win_bid = float(bids[0][1]) if isinstance(bids[0], (list, tuple)) else float(bids[0]["bid"])
            _learned_win_prices[k] = _ema_update(_learned_win_prices[k], win_bid, LEARN_DECAY)
        except Exception:
            continue

def _suggest_bid_for_auction(
    a_id: str,
    a: Dict[str, Any],
    competitiveness: float,
    available: float
) -> float:
    ev = _expected_points(a)
    if ev < IGNORE_LOW_EV:
        return 0.0

    k = _auction_key(a)
    learned = _learned_win_prices.get(k, 0.0)

    if learned > 0:
        fair = learned * WIN_MARGIN
    else:
        fair = FIRST_FAIR_PRICE_PER_POINT * ev * (0.75 + 0.25 * competitiveness)

    if CAP_BID_AT_GOLD:
        fair = min(fair, max(0.0, available))

    jitter = random.uniform(0.0, TIE_JITTER_MAX)
    bid = max(MIN_BID, fair + jitter)

    return max(0.0, float(bid))

def make_bid(
    agent_id: str,
    current_round: int,
    states: Dict[str, Dict[str, float]],
    auctions: Dict[str, Dict[str, int]],
    prev_auctions: Dict[str, Any],
    bank_state: Dict[str, Any],
) -> Dict[str, float]:

    global manual_trigger

    try:
        _learn_from_prev(prev_auctions)
    except Exception:
        pass

    my = states.get(agent_id, {}) or {}
    my_gold = float(my.get("gold", 0.0))

    if (manual_trigger and auctions) or (fucking_emergency and auctions):
        manual_trigger = False 
        spend_amount = 0.20 * my_gold
        if spend_amount > 0:
            top = sorted(auctions.items(),
                         key=lambda kv: _expected_points(kv[1]),
                         reverse=True)[:3]
            if top:
                per = max(MIN_BID, spend_amount / len(top))
                bids = {a_id: round(per, 2) for a_id, _ in top}
                print(f"[Manual control] Spending {spend_amount:.1f} gold "
                      f"({per:.1f} each) on {len(top)} top-EV auctions.")
                return bids

    budget = my_gold * MAX_SPEND_FRACTION
    reserve = _reserve_for_interest(my_gold, bank_state)
    spendable = max(0.0, budget - reserve)

    comp = _estimate_competitiveness(states, len(auctions))

    plan = []
    for a_id, a in (auctions or {}).items():
        suggested = _suggest_bid_for_auction(a_id, a, comp, spendable)
        ev = _expected_points(a)
        efficiency = ev / max(1.0, suggested)
        plan.append((efficiency, a_id, suggested))

    plan.sort(reverse=True, key=lambda x: x[0])

    bids: Dict[str, float] = {}
    remaining = spendable
    for eff, a_id, proposed in plan:
        if proposed <= 0 or remaining <= 0:
            continue
        bid = min(proposed, remaining)
        if bid < MIN_BID:
            continue
        bids[a_id] = round(bid, 2)
        remaining -= bid

    return bids

if __name__ == "__main__":
    host = "opentsetlin.com"
    agent_name = "She said she was 12"
    player_id = "Urs Erik Pfrommer"
    port = 8000

    game = AuctionGameClient(host=host,
                             agent_name=agent_name,
                             player_id=player_id,
                             port=port)
    try:
        game.run(make_bid)
    except KeyboardInterrupt:
        print("<interrupt - shutting down>")

    print("<game is done>")
