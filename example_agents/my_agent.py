# example_agents/agent_ev_learner.py
from __future__ import annotations
import math
import random
import os

from collections import defaultdict
from typing import Dict, Tuple, Any
from dnd_auction_game import AuctionGameClient
# ---- Dashboard here  ------------------------------------------
ALPHA_FAIR_PRICE_PER_POINT = 15.0   # baseline price per expected point when no history
WIN_MARGIN = 1.05                   # bid slightly above learned winning price
MAX_SPEND_FRACTION = 0.95           # leave a little buffer to avoid overspending
MIN_BID = 1.0
TIE_JITTER_MAX = 3.0                # add up to this many gold to avoid ties
LEARN_DECAY = 0.8                   # EMA decay for learned winning prices
CAP_BID_AT_GOLD = True              # never bid more than available
IGNORE_LOW_EV = 0.5                 # ignore auctions with too small EV
# -----------------------------------------------------------------------------

# Internal store across rounds (the server imports this module and calls make_bid)
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
    # crude heuristic: more players per auction => tougher market
    n_players = max(1, len(states))
    return max(1.0, n_players / max(1, n_auctions))

def _reserve_for_interest(gold: float, bank_state: Dict[str, Any]) -> float:
    """
    Keep some gold if interest next round is enticing (and within cap).
    bank_state structure (per README):
      gold_income_per_round, bank_interest_per_round, bank_limit_per_round
      index [0] gives next round
    """
    if not bank_state:
        return 0.0
    try:
        r = float(bank_state["bank_interest_per_round"][0])  # e.g., 0.10 for 10%
        cap = float(bank_state["bank_limit_per_round"][0])   # interest applies up to this
    except Exception:
        return 0.0

    if r <= 0:
        return 0.0

    # Simple policy: reserve proportionally to r, up to the cap and current gold.
    # If r=10%, reserve ~20% of gold (capped), if r=20%, reserve ~35%, etc.
    reserve_frac = min(0.5, 2.0 * r + 0.0)  # clamp to 50% max
    target_reserve = min(cap, gold) * reserve_frac
    return max(0.0, min(target_reserve, gold * 0.5))  # also clamp to 50% of current gold

def _learn_from_prev(prev_auctions: Dict[str, Any]) -> None:
    # prev_auctions[auction_id] has {"die","num","bonus","reward","bids":[(agent_id,bid),...]} (winner first)
    for _aid, info in (prev_auctions or {}).items():
        try:
            k = _auction_key(info)
            bids = info.get("bids") or []
            if not bids:
                continue
            # winning bid is first in list per README
            win_bid = float(bids[0][1]) if isinstance(bids[0], (list, tuple)) else float(bids[0]["bid"])
            # update EMA of typical winning price for this auction type
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
        # No history: set a fair price proportional to EV, scaled by competitiveness.
        fair = ALPHA_FAIR_PRICE_PER_POINT * ev * (0.75 + 0.25 * competitiveness)

    # Never exceed current available gold if configured (otherwise you risk forced overspend).
    if CAP_BID_AT_GOLD:
        fair = min(fair, max(0.0, available))

    # Add a tiny jitter to avoid ties between similar agents.
    jitter = random.uniform(0.0, TIE_JITTER_MAX)
    bid = max(MIN_BID, fair + jitter)

    # Safety: don't bid absurd numbers
    return max(0.0, float(bid))

def make_bid(
    agent_id: str,
    current_round: int,
    states: Dict[str, Dict[str, float]],
    auctions: Dict[str, Dict[str, int]],
    prev_auctions: Dict[str, Any],
    bank_state: Dict[str, Any],
) -> Dict[str, float]:
    """
    Return a dict: {auction_id: bid_amount}
    """
    # learn from last round
    try:
        _learn_from_prev(prev_auctions)
    except Exception:
        pass

    my = states.get(agent_id, {}) or {}
    my_gold = float(my.get("gold", 0.0))

    # Keep a bit unspent to avoid round-off + to allow interest
    budget = my_gold * MAX_SPEND_FRACTION
    reserve = _reserve_for_interest(my_gold, bank_state)
    spendable = max(0.0, budget - reserve)

    comp = _estimate_competitiveness(states, len(auctions))

    # Score auctions by EV / (suggested bid), i.e., points per gold
    plan = []
    for a_id, a in (auctions or {}).items():
        # For planning we pretend we can spend full spendable on each; we’ll re-check during allocation
        suggested = _suggest_bid_for_auction(a_id, a, comp, spendable)
        ev = _expected_points(a)
        efficiency = ev / max(1.0, suggested)
        plan.append((efficiency, a_id, suggested))

    # Pick the best efficiencies first until we run out of gold
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
    
    host = "localhost"
    agent_name = "{}_{}".format(os.path.basename(__file__), random.randint(1, 1000))
    player_id = "agent_007_asdf"
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
