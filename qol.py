"""
Quality-of-Life composer.

Tracks the non-DPS, non-EHP metrics that determine how a build *feels* to play:
  - Movement speed (mapping-critical, near-irrelevant for bossing)
  - Ailment immunities and reduced effect (broad coverage for mapping, specific
    immunities for known boss skills)

Per the project_optimization_loop memory: do NOT collapse this into a single
"QoL score". Always show the underlying metrics with their playstyle relevance
so the user can sanity-check.

Phase 3 (the optimizer) will read these results and weight them by playstyle.
This module just measures.
"""
from __future__ import annotations
from dataclasses import dataclass, field

import calc as offense_calc
import character
import pob_data
import build as build_module
from stat_map import Modifier


# Ailments we surface explicitly. Order matters for stable output.
AILMENTS = (
    ("Freeze",   "ImmuneToFreeze",   "elemental"),
    ("Chill",    "ImmuneToChill",    "elemental"),
    ("Ignite",   "ImmuneToIgnite",   "elemental"),
    ("Shock",    "ImmuneToShock",    "elemental"),
    ("Stun",     "ImmuneToStun",     "physical"),
    ("Poison",   "ImmuneToPoison",   "chaos"),
    ("Bleed",    "ImmuneToBleed",    "physical"),
    ("Curse",    "ImmuneToCurse",    "debuff"),
)

# How each metric is weighted per playstyle. NOT a single score — used only by
# the report() to tag metrics with how much they matter for the chosen play.
# Phase 3's optimizer will read these to produce actual rankings.
PLAYSTYLE_WEIGHTS = {
    build_module.PLAYSTYLE_MAPPING: {
        "MovementSpeed": "critical",
        "AilmentImmunity": "high",  # broad coverage matters; you eat random hits
    },
    build_module.PLAYSTYLE_BOSSING: {
        "MovementSpeed": "low",     # you stand and deliver
        "AilmentImmunity": "specific",  # only what the boss can apply
    },
    build_module.PLAYSTYLE_BALANCED: {
        "MovementSpeed": "high",
        "AilmentImmunity": "high",
    },
}


@dataclass
class QolResult:
    playstyle: str
    movement_speed_pct: float           # total % increased; flat would be folded in if we modeled it
    immunities: dict[str, bool]         # ailment name -> immune?
    notes: list[str] = field(default_factory=list)

    def report(self) -> str:
        weights = PLAYSTYLE_WEIGHTS.get(self.playstyle, PLAYSTYLE_WEIGHTS[build_module.PLAYSTYLE_BALANCED])
        ms_weight = weights["MovementSpeed"]
        ai_weight = weights["AilmentImmunity"]
        L = [
            f"QoL breakdown (playstyle: {self.playstyle})",
            f"  Movement speed:          +{self.movement_speed_pct:.0f}%   [{ms_weight} for {self.playstyle}]",
            f"  Ailment immunities       [{ai_weight} for {self.playstyle}]:",
        ]
        # Group: immune (+) vs not (-)
        immune = [name for name, immune in self.immunities.items() if immune]
        not_immune = [name for name, immune in self.immunities.items() if not immune]
        if immune:
            L.append(f"    + immune to: {', '.join(immune)}")
        if not_immune:
            L.append(f"    - vulnerable to: {', '.join(not_immune)}")
        if self.notes:
            L.append("  Notes:")
            for n in self.notes:
                L.append(f"    - {n}")
        return "\n".join(L)


def _has_immunity(mods, target: str) -> bool:
    """A boolean immunity is granted when any BASE source contributes >= 1."""
    total = sum(m.value for m in mods if m.target == target and m.type == "BASE")
    return total >= 1


def compute_qol(build) -> QolResult:
    """Snapshot the QoL metrics for a build. Does NOT score them — that's the
    optimizer's job (phase 3), informed by build.playstyle."""
    pool = offense_calc.collect_modifiers(build)
    all_mods = pool.inc + pool.more

    notes: list[str] = []

    # Movement speed (we treat both INC and MORE as "% increased" for display)
    ms_inc = sum(m.value for m in all_mods
                 if m.target == "MovementSpeed" and m.type == "INC")
    ms_more_factor = 1.0
    for m in all_mods:
        if m.target == "MovementSpeed" and m.type == "MORE":
            ms_more_factor *= (1 + m.value / 100)
    # Approximate as a single inc% display: (1+inc)*more_factor - 1
    effective_ms = ((1 + ms_inc / 100) * ms_more_factor - 1) * 100

    # Ailment immunities. ImmuneToElementalAilments is a composite from Purity
    # of Elements that covers Freeze/Chill/Ignite/Shock; expand it across the
    # four ailment-specific targets.
    elemental_covered = _has_immunity(all_mods, "ImmuneToElementalAilments")
    if elemental_covered:
        notes.append("Purity of Elements (or similar) grants immunity to all elemental ailments")

    immunities = {}
    for name, target, category in AILMENTS:
        direct = _has_immunity(all_mods, target)
        covered_by_purity = elemental_covered and category == "elemental"
        immunities[name] = direct or covered_by_purity

    return QolResult(
        playstyle=build.playstyle,
        movement_speed_pct=effective_ms,
        immunities=immunities,
        notes=notes,
    )
