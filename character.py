"""
Character-level pool computation: life, mana, energy shield from a Build +
ModifierPool. Shared by both calc.py (DPS path) and defense.py (EHP path)
so we don't duplicate the formulas — and so calc-time rules like Frostmage's
"% of max reserved mana as cold damage" can resolve the mana pool the same
way EHP does.

Kept narrow: just pool sums right now. Mana cost, regen, leech, etc. will
land here later.
"""
from __future__ import annotations
from stat_map import Modifier


# ---------------------------------------------------------------------------
# Base stat formulas — straight from the PoE wiki
# ---------------------------------------------------------------------------

def base_life(level: int, strength: int) -> float:
    """38 base + 12 per level past 1, +1 life per 2 strength."""
    return 38 + 12 * (level - 1) + strength * 0.5


def base_mana(level: int, intelligence: int) -> float:
    """40 base + 6 per level past 1, +1 mana per 2 intelligence."""
    return 40 + 6 * (level - 1) + intelligence * 0.5


def int_es_inc(intelligence: int) -> float:
    """Each 5 int gives +1% increased maximum energy shield."""
    return intelligence / 5.0


# ---------------------------------------------------------------------------
# Modifier-pool helpers
# ---------------------------------------------------------------------------

def sum_typed_mods(mods, target: str, mod_type: str) -> float:
    return sum(m.value for m in mods if m.target == target and m.type == mod_type)


def apply_pool(base: float, mods, target: str) -> float:
    """Standard PoE pool composition: (base + sum_BASE) * (1 + sum_INC/100) * prod(1 + MORE/100)."""
    flat = sum_typed_mods(mods, target, "BASE")
    inc = sum_typed_mods(mods, target, "INC")
    more_factor = 1.0
    for m in mods:
        if m.target == target and m.type == "MORE":
            more_factor *= (1 + m.value / 100)
    return (base + flat) * (1 + inc / 100) * more_factor


# ---------------------------------------------------------------------------
# Compute the three character pools from a Build + already-collected pool
# ---------------------------------------------------------------------------

def compute_pools(build, modifier_pool) -> dict[str, float]:
    """Return {'life', 'mana', 'es'} for a build, applying every relevant mod
    in the pool. Used by calc (for Frostmage-style "% of mana" rules) and
    defense (for EHP composition)."""
    all_mods = modifier_pool.inc + modifier_pool.more
    es_int_inc = Modifier("MaximumEnergyShield", "INC", int_es_inc(build.intelligence))
    return {
        "life": apply_pool(base_life(build.level, build.strength), all_mods, "MaximumLife"),
        "mana": apply_pool(base_mana(build.level, build.intelligence), all_mods, "MaximumMana"),
        "es":   apply_pool(0, all_mods + [es_int_inc], "MaximumEnergyShield"),
    }
