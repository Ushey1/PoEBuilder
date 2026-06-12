"""
Defensive composer — EHP from pools, resists, armour, evasion.

Standard MVP scope (per the project plan):
  - Life / Mana / Energy Shield pools (base + flat + inc + more)
  - Resistances (4 types, capped, with penalty)
  - Armour with PoE's mitigation formula vs a reference hit
  - Evasion with PoE's evade-chance formula
  - EHP per damage type vs a reference incoming hit

Skipped for now (additive layers we can drop in later):
  - Block / spell block / suppression
  - "Taken as" conversion (e.g., 30% phys taken as fire)
  - Recoup / regen / leech
  - Fortify / Endurance Charges / Arcane Cloak / etc.
"""
from __future__ import annotations
from dataclasses import dataclass, field

import pob_data
import calc as offense_calc  # reuse collect_modifiers + ModifierPool
import character
from stat_map import Modifier


# Re-export helpers so existing callers / tests that imported from defense
# keep working without forcing them to know the new module layout.
base_life = character.base_life
base_mana = character.base_mana
int_es_inc = character.int_es_inc
_sum_typed = character.sum_typed_mods
_apply_pool = character.apply_pool


# ---------------------------------------------------------------------------
# Armour / Evasion mechanics
# ---------------------------------------------------------------------------

def armour_mitigation(armour: float, hit_damage: float) -> float:
    """PoE formula: armour / (armour + 12 * hit). Returns the fraction reduced."""
    if armour <= 0 or hit_damage <= 0:
        return 0.0
    raw = armour / (armour + 12 * hit_damage)
    return min(0.9, raw)  # cap at 90% (the game cap)


def evade_chance(evasion: float, enemy_accuracy: float) -> float:
    """PoE formula: 1 - acc / (acc + (eva/4)^0.9). Returns evade probability (0..1)."""
    if evasion <= 0 or enemy_accuracy <= 0:
        return 0.0
    raw = 1 - enemy_accuracy / (enemy_accuracy + (evasion / 4) ** 0.9)
    return max(0.0, min(0.95, raw))  # cap 95%


# ---------------------------------------------------------------------------
# Result object
# ---------------------------------------------------------------------------

@dataclass
class DefenseResult:
    life: float
    mana: float
    es: float
    pool: float                    # life + ES

    resists: dict[str, float]      # {"Fire": 75, ...} after cap and penalty
    armour: float
    evasion: float

    # Mitigation summaries
    avg_phys_mitigation_pct: float       # armour vs reference hit
    evade_chance_pct: float              # vs enemy_accuracy

    # EHP by damage type (vs reference incoming hit)
    ehp_vs_phys_attack: float
    ehp_vs_cold_spell: float
    ehp_vs_fire_spell: float
    ehp_vs_lightning_spell: float
    ehp_vs_chaos_hit: float

    notes: list[str] = field(default_factory=list)
    skipped_unknown_stats: list[tuple[str, str, float]] = field(default_factory=list)

    def report(self) -> str:
        L = [
            "Defense breakdown",
            f"  Life:                    {self.life:>8.0f}",
            f"  Energy Shield:           {self.es:>8.0f}",
            f"  Mana:                    {self.mana:>8.0f}",
            f"  Total effective pool:    {self.pool:>8.0f}  (life + ES)",
            f"  ----",
            f"  Resists (capped):        Fire {self.resists['Fire']:>3.0f}%   "
            f"Cold {self.resists['Cold']:>3.0f}%   Lightning {self.resists['Lightning']:>3.0f}%   "
            f"Chaos {self.resists['Chaos']:>3.0f}%",
            f"  Armour:                  {self.armour:>8.0f}  "
            f"(vs ref hit: {self.avg_phys_mitigation_pct:.1f}% mitigation)",
            f"  Evasion:                 {self.evasion:>8.0f}  "
            f"(evade chance vs attacks: {self.evade_chance_pct:.1f}%)",
            f"  ----",
            f"  EHP vs phys attack:      {self.ehp_vs_phys_attack:>8.0f}",
            f"  EHP vs cold spell:       {self.ehp_vs_cold_spell:>8.0f}",
            f"  EHP vs fire spell:       {self.ehp_vs_fire_spell:>8.0f}",
            f"  EHP vs lightning spell:  {self.ehp_vs_lightning_spell:>8.0f}",
            f"  EHP vs chaos hit:        {self.ehp_vs_chaos_hit:>8.0f}",
        ]
        if self.notes:
            L.append("  Notes:")
            for n in self.notes:
                L.append(f"    - {n}")
        unknown_lines = offense_calc.format_unknown_stats(self.skipped_unknown_stats)
        if unknown_lines:
            L.append("  " + unknown_lines[0])
            for ln in unknown_lines[1:]:
                L.append("  " + ln)
        return "\n".join(L)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------

def compute_ehp(build) -> DefenseResult:
    """Compute defensive summary + EHP per damage type vs reference incoming hit."""
    pool = offense_calc.collect_modifiers(build)
    all_mods = pool.inc + pool.more  # all the modifier objects in one list for filtering
    # Pool flats stored separately for damage; defensive flats live in inc/more lists
    # because _push_modifier sends BASE non-Added/non-MinMax targets to pool.inc.

    notes = []

    # 1. Pools (shared with calc.py via character.compute_pools)
    pools = character.compute_pools(build, pool)
    life = pools["life"]
    mana = pools["mana"]
    es = pools["es"]

    # 2. Resistances
    resists = {}
    for elem in ("Fire", "Cold", "Lightning"):
        flat = (_sum_typed(all_mods, f"{elem}Resist", "BASE")
                + _sum_typed(all_mods, "AllElementalResist", "BASE"))
        total = flat + build.resist_penalty
        resists[elem] = min(build.max_resist_pct, total)
    chaos_flat = _sum_typed(all_mods, "ChaosResist", "BASE")
    resists["Chaos"] = min(build.max_resist_pct, chaos_flat + build.resist_penalty)

    # 3. Armour and Evasion
    armour = _apply_pool(0, all_mods, "Armour")
    evasion = _apply_pool(0, all_mods, "Evasion")

    # 4. Mitigation against the reference hit
    ref_hit = build.reference_hit_damage
    phys_mit = armour_mitigation(armour, ref_hit)
    evade = evade_chance(evasion, build.enemy_accuracy)

    total_pool = life + es

    def ehp_against(resist_pct: float, *, is_phys: bool, is_attack: bool) -> float:
        taken_factor = 1.0 - resist_pct / 100.0
        if is_phys:
            taken_factor *= (1.0 - phys_mit)
        if is_attack:
            # On average, evade reduces effective damage by `evade_chance`
            taken_factor *= (1.0 - evade)
        if taken_factor <= 0:
            return float("inf")
        return total_pool / taken_factor

    return DefenseResult(
        life=life,
        mana=mana,
        es=es,
        pool=total_pool,
        resists=resists,
        armour=armour,
        evasion=evasion,
        avg_phys_mitigation_pct=phys_mit * 100,
        evade_chance_pct=evade * 100,
        ehp_vs_phys_attack=ehp_against(0, is_phys=True, is_attack=True),
        ehp_vs_cold_spell=ehp_against(resists["Cold"], is_phys=False, is_attack=False),
        ehp_vs_fire_spell=ehp_against(resists["Fire"], is_phys=False, is_attack=False),
        ehp_vs_lightning_spell=ehp_against(resists["Lightning"], is_phys=False, is_attack=False),
        ehp_vs_chaos_hit=ehp_against(resists["Chaos"], is_phys=False, is_attack=False),
        notes=notes,
        skipped_unknown_stats=pool.unknown_stats,
    )
