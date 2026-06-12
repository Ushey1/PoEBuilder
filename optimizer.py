"""
Single-dimension swap optimizer.

For a baseline Build, enumerate one-at-a-time changes along a chosen dimension
(starting with `auras`), evaluate each change with the full calc pipeline,
and produce a ranked list of suggestions.

Per the project_optimization_loop memory: do NOT collapse to a single weighted
score. Surface deltas as a tuple (DPS, EHP, QoL) so the user can read what
each swap actually does. Phase 3b will add playstyle-weighted ranking on top.
"""
from __future__ import annotations
from dataclasses import dataclass, field, replace
from copy import deepcopy

from build import Build, GemSocket, Modifier, validate
import calc
import defense
import qol
import pob_data
import ninja_template


# ---------------------------------------------------------------------------
# Candidate pool helpers
# ---------------------------------------------------------------------------

def all_support_keys() -> list[str]:
    """PoB keys for every support gem, minus meta-filtered + Vaal variants.
    Caller is responsible for tag-compatibility filtering against a main skill."""
    keys: list[str] = []
    for key, data in pob_data.load_skills().items():
        if not isinstance(data, dict):
            continue
        if not data.get("support"):
            continue
        if key.startswith("Vaal"):
            continue
        display = data.get("name", "") or ""
        if ninja_template.is_excluded(display, key):
            continue
        keys.append(key)
    return keys


def supports_compatible_with(main_skill_data: dict, support_keys: list[str]) -> list[str]:
    """Filter `support_keys` to those whose require/exclude tags fit the main."""
    main_tags = pob_data.skill_tags(main_skill_data)
    out = []
    for key in support_keys:
        sup = pob_data.get_skill(key)
        if sup is None:
            continue
        req = pob_data.support_requires(sup)
        exc = pob_data.support_excludes(sup)
        if req and not (req & main_tags):
            continue
        if exc and (exc & main_tags):
            continue
        out.append(key)
    return out


def all_aura_keys() -> list[str]:
    """PoB keys for every gem that's a real buff-aura — Aura tag AND reserves
    mana. Excludes Vaal variants and meta-filtered gems."""
    keys: list[str] = []
    for key, data in pob_data.load_skills().items():
        if key.startswith("Vaal"):
            continue
        if not isinstance(data, dict):
            continue
        if not ninja_template._is_reserved_buff(data):
            continue
        display = data.get("name", "") or ""
        if ninja_template.is_excluded(display, key):
            continue
        keys.append(key)
    return keys


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class BuildChange:
    """A proposed modification (one or many gem changes) + its evaluation
    against the baseline build. `removed` and `added` are lists so the same
    type handles single-swap (1 in / 1 out) and subset-search (N in / M out).
    """
    action: str                     # "REPLACE" | "ADD" | "REMOVE" | "SUBSET-K"
    removed: list[GemSocket]        # auras dropped from baseline
    added: list[GemSocket]          # auras added (not in baseline)
    dps_delta: float                # final DPS minus baseline DPS
    dps_pct_delta: float
    ehp_delta_phys: float
    ehp_delta_cold: float
    qol_immunity_gained: list[str]
    qol_immunity_lost: list[str]
    qol_movespeed_delta: float
    valid_after: bool
    invalid_reasons: list[str]

    def _short_change(self) -> str:
        rm = ",".join(s.gem_name for s in self.removed) if self.removed else "-"
        ad = ",".join(s.gem_name for s in self.added) if self.added else "-"
        if self.action == "REPLACE" and len(self.removed) == 1 and len(self.added) == 1:
            return f"REPLACE {self.removed[0].gem_name} -> {self.added[0].gem_name}"
        if self.action == "ADD" and not self.removed and len(self.added) == 1:
            return f"ADD     {self.added[0].gem_name}"
        if self.action == "REMOVE" and not self.added and len(self.removed) == 1:
            return f"REMOVE  {self.removed[0].gem_name}"
        return f"{self.action}: -[{rm}] +[{ad}]"

    def summary(self) -> str:
        change = self._short_change()

        sign = "+" if self.dps_delta >= 0 else ""
        dps_str = f"{sign}{self.dps_delta:>9.0f} DPS ({sign}{self.dps_pct_delta:>5.1f}%)"

        ehp_sign_p = "+" if self.ehp_delta_phys >= 0 else ""
        ehp_sign_c = "+" if self.ehp_delta_cold >= 0 else ""
        ehp_str = (f"EHP phys {ehp_sign_p}{self.ehp_delta_phys:>6.0f}  "
                   f"cold {ehp_sign_c}{self.ehp_delta_cold:>6.0f}")

        qol_parts = []
        if self.qol_immunity_gained:
            qol_parts.append(f"+immunity:{','.join(self.qol_immunity_gained)}")
        if self.qol_immunity_lost:
            qol_parts.append(f"-immunity:{','.join(self.qol_immunity_lost)}")
        if self.qol_movespeed_delta:
            sign_m = "+" if self.qol_movespeed_delta >= 0 else ""
            qol_parts.append(f"ms {sign_m}{self.qol_movespeed_delta:.0f}%")
        qol_str = ("  " + " | ".join(qol_parts)) if qol_parts else ""

        validity = "" if self.valid_after else "  [INVALID]"

        return f"  {change:<70} {dps_str}  {ehp_str}{qol_str}{validity}"


# ---------------------------------------------------------------------------
# Build cloning + change application
# ---------------------------------------------------------------------------

def _clone_build(b: Build) -> Build:
    """Cheap deep copy of a Build for hypothetical evaluation. Lists are
    fresh so mutations don't leak back."""
    return Build(
        char_class=b.char_class,
        ascendancy=b.ascendancy,
        level=b.level,
        main_skill=GemSocket(b.main_skill.gem_name, b.main_skill.level, b.main_skill.quality),
        supports=[GemSocket(s.gem_name, s.level, s.quality) for s in b.supports],
        auras=[GemSocket(a.gem_name, a.level, a.quality) for a in b.auras],
        gear_mods=list(b.gear_mods),
        passive_mods=list(b.passive_mods),
        allocated_nodes=list(b.allocated_nodes),
        strength=b.strength,
        dexterity=b.dexterity,
        intelligence=b.intelligence,
        resist_penalty=b.resist_penalty,
        max_resist_pct=b.max_resist_pct,
        enemy_accuracy=b.enemy_accuracy,
        reference_hit_damage=b.reference_hit_damage,
        min_free_mana_pct=b.min_free_mana_pct,
        reservation_reduction_pct=b.reservation_reduction_pct,
        playstyle=b.playstyle,
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------

@dataclass
class _Snapshot:
    """Cached baseline metrics so we don't rerun the calc per candidate."""
    dps: float
    ehp_phys: float
    ehp_cold: float
    immunities: dict[str, bool]
    movespeed_pct: float


def _measure(b: Build) -> _Snapshot:
    dps_res = calc.compute_dps(b)
    ehp_res = defense.compute_ehp(b)
    qol_res = qol.compute_qol(b)
    return _Snapshot(
        dps=dps_res.dps,
        ehp_phys=ehp_res.ehp_vs_phys_attack,
        ehp_cold=ehp_res.ehp_vs_cold_spell,
        immunities=dict(qol_res.immunities),
        movespeed_pct=qol_res.movement_speed_pct,
    )


def _diff_to_change(action: str, removed: list, added: list, baseline: _Snapshot,
                    candidate: _Snapshot, val_result) -> BuildChange:
    dps_delta = candidate.dps - baseline.dps
    pct = (dps_delta / baseline.dps * 100) if baseline.dps else 0
    gained = sorted([k for k, v in candidate.immunities.items()
                     if v and not baseline.immunities.get(k, False)])
    lost = sorted([k for k, v in baseline.immunities.items()
                   if v and not candidate.immunities.get(k, False)])
    return BuildChange(
        action=action,
        removed=removed,
        added=added,
        dps_delta=dps_delta,
        dps_pct_delta=pct,
        ehp_delta_phys=candidate.ehp_phys - baseline.ehp_phys,
        ehp_delta_cold=candidate.ehp_cold - baseline.ehp_cold,
        qol_immunity_gained=gained,
        qol_immunity_lost=lost,
        qol_movespeed_delta=candidate.movespeed_pct - baseline.movespeed_pct,
        valid_after=val_result.valid,
        invalid_reasons=list(val_result.warnings),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enumerate_aura_swaps(
    build: Build,
    *,
    gem_level: int = 20,
    include_invalid: bool = True,
) -> list[BuildChange]:
    """Try every single-aura modification (REPLACE / ADD / REMOVE) and return
    a list of evaluated BuildChange objects, unsorted.

    `include_invalid=True` keeps changes whose resulting build fails validation
    (e.g., over-reserved). They're tagged INVALID — useful info ("this would
    help if you had reservation reduction").
    """
    baseline = _measure(build)
    skills = pob_data.load_skills()

    current_aura_keys = {a.gem_name for a in build.auras}
    candidates = [k for k in all_aura_keys()
                  if k not in current_aura_keys and skills.get(k) is not None]

    changes: list[BuildChange] = []

    # REPLACE: for each existing aura, try every candidate
    for slot_idx, existing in enumerate(build.auras):
        for cand_key in candidates:
            cand_data = skills[cand_key]
            cand_lvl = ninja_template.clamp_gem_level(cand_data, gem_level)
            new_socket = GemSocket(cand_key, cand_lvl, 0)
            new_build = _clone_build(build)
            new_build.auras[slot_idx] = new_socket
            vr = validate(new_build)
            if not include_invalid and not vr.valid:
                continue
            snap = _measure(new_build)
            changes.append(_diff_to_change("REPLACE", [existing], [new_socket], baseline, snap, vr))

    # REMOVE: drop one aura, see what happens (frees reservation)
    for slot_idx, existing in enumerate(build.auras):
        new_build = _clone_build(build)
        new_build.auras.pop(slot_idx)
        vr = validate(new_build)
        snap = _measure(new_build)
        changes.append(_diff_to_change("REMOVE", [existing], [], baseline, snap, vr))

    # ADD: append candidate to the aura list (only valid ones if requested)
    for cand_key in candidates:
        cand_data = skills[cand_key]
        cand_lvl = ninja_template.clamp_gem_level(cand_data, gem_level)
        new_socket = GemSocket(cand_key, cand_lvl, 0)
        new_build = _clone_build(build)
        new_build.auras.append(new_socket)
        vr = validate(new_build)
        if not include_invalid and not vr.valid:
            continue
        snap = _measure(new_build)
        changes.append(_diff_to_change("ADD", [], [new_socket], baseline, snap, vr))

    return changes


# ---------------------------------------------------------------------------
# Multi-aura subset search (phase 3b)
# ---------------------------------------------------------------------------

def enumerate_aura_subsets(
    build: Build,
    *,
    sizes: tuple[int, ...] = (1, 2, 3),
    gem_level: int = 20,
    valid_only: bool = True,
) -> list[BuildChange]:
    """For each subset of the candidate pool with size in `sizes`, evaluate
    that subset AS the new aura set. Pool = current build auras + every
    known aura candidate.

    This is the multi-swap search the single-swap optimizer can't do: it
    finds combinations like "drop Purity AND Zealotry, add Anger AND Hatred"
    that escape an over-reserved baseline.

    Cost: C(N, k) calc runs per size k. For ~50 candidates: size=3 -> ~20k.
    Each run is fast (<10ms) but the time adds up — pass smaller `sizes`
    if you want a quick pass.
    """
    import itertools
    baseline = _measure(build)
    skills = pob_data.load_skills()
    current_keys = [a.gem_name for a in build.auras]
    current_set = set(current_keys)
    pool_keys = sorted(current_set | set(all_aura_keys()))

    # Pre-build GemSocket per pool key (deterministic level clamp)
    socket_for: dict[str, GemSocket] = {}
    for k in pool_keys:
        data = skills.get(k)
        if data is None:
            continue
        lvl = ninja_template.clamp_gem_level(data, gem_level)
        socket_for[k] = GemSocket(k, lvl, 0)

    changes: list[BuildChange] = []
    for size in sizes:
        if size < 0 or size > len(socket_for):
            continue
        for combo in itertools.combinations(sorted(socket_for.keys()), size):
            combo_set = set(combo)
            if combo_set == current_set:
                continue  # this IS the baseline
            new_build = _clone_build(build)
            new_build.auras = [socket_for[k] for k in combo]
            vr = validate(new_build)
            if valid_only and not vr.valid:
                continue
            snap = _measure(new_build)
            removed = [s for s in build.auras if s.gem_name not in combo_set]
            added = [socket_for[k] for k in combo if k not in current_set]
            changes.append(_diff_to_change(f"SUBSET-{size}", removed, added,
                                            baseline, snap, vr))
    return changes


def pareto_frontier(changes: list[BuildChange]) -> list[BuildChange]:
    """Return the subset of `changes` that are non-dominated across our
    optimization axes: DPS, EHP-vs-phys, EHP-vs-cold, move-speed, and
    ailment-immunity count.

    A change `c` is dominated by `d` if d is >= c on every axis and strictly
    greater on at least one. The Pareto frontier contains all changes for
    which no such `d` exists — these are the trade-offs worth considering.
    """
    def axes(c: BuildChange) -> tuple[float, ...]:
        return (
            c.dps_delta,
            c.ehp_delta_phys,
            c.ehp_delta_cold,
            c.qol_movespeed_delta,
            len(c.qol_immunity_gained),
        )

    frontier: list[BuildChange] = []
    all_axes = [axes(c) for c in changes]
    for i, ai in enumerate(all_axes):
        dominated = False
        for j, aj in enumerate(all_axes):
            if i == j:
                continue
            # aj dominates ai if aj >= ai on all axes and strictly > on at least one
            if all(j_v >= i_v for j_v, i_v in zip(aj, ai)) and \
               any(j_v >  i_v for j_v, i_v in zip(aj, ai)):
                dominated = True
                break
        if not dominated:
            frontier.append(changes[i])
    return frontier


def _dedup_subsets(changes: list[BuildChange]) -> list[BuildChange]:
    """Many subsets reach identical (DPS, EHP, QoL) outcomes because some
    auras contribute nothing in our model. Collapse them: for each unique
    metric signature, keep the change with the fewest aura modifications
    (the "minimal" path to that outcome)."""
    by_sig: dict[tuple, BuildChange] = {}
    for c in changes:
        sig = (
            round(c.dps_delta, 0),
            round(c.ehp_delta_phys, 0),
            round(c.ehp_delta_cold, 0),
            tuple(c.qol_immunity_gained),
            tuple(c.qol_immunity_lost),
            round(c.qol_movespeed_delta, 1),
        )
        existing = by_sig.get(sig)
        n_changes = len(c.removed) + len(c.added)
        if existing is None or n_changes < len(existing.removed) + len(existing.added):
            by_sig[sig] = c
    return list(by_sig.values())


def enumerate_support_swaps(
    build: Build,
    *,
    gem_level: int = 20,
    include_invalid: bool = True,
) -> list[BuildChange]:
    """Single-change enumeration for supports: REPLACE / REMOVE / ADD.

    Candidate pool is filtered by tag compatibility with the main skill — we
    only try supports the main skill can actually socket (Spell-supports for
    spells, Melee-supports for melee attacks, etc.).
    """
    baseline = _measure(build)
    main_data = pob_data.get_skill(build.main_skill.gem_name)
    if main_data is None:
        return []

    current_support_keys = {s.gem_name for s in build.supports}
    pool_all = all_support_keys()
    pool = [k for k in supports_compatible_with(main_data, pool_all)
            if k not in current_support_keys]

    skills = pob_data.load_skills()
    changes: list[BuildChange] = []

    # REPLACE: swap each existing support with each candidate
    for slot_idx, existing in enumerate(build.supports):
        for cand_key in pool:
            cand_data = skills[cand_key]
            cand_lvl = ninja_template.clamp_gem_level(cand_data, gem_level)
            new_socket = GemSocket(cand_key, cand_lvl, build.supports[slot_idx].quality)
            new_build = _clone_build(build)
            new_build.supports[slot_idx] = new_socket
            vr = validate(new_build)
            if not include_invalid and not vr.valid:
                continue
            snap = _measure(new_build)
            changes.append(_diff_to_change("REPLACE", [existing], [new_socket],
                                            baseline, snap, vr))

    # REMOVE: drop one support (frees a link slot but loses its effect)
    for slot_idx, existing in enumerate(build.supports):
        new_build = _clone_build(build)
        new_build.supports.pop(slot_idx)
        vr = validate(new_build)
        snap = _measure(new_build)
        changes.append(_diff_to_change("REMOVE", [existing], [], baseline, snap, vr))

    # ADD: append a candidate (assumes user has an open link)
    for cand_key in pool:
        cand_data = skills[cand_key]
        cand_lvl = ninja_template.clamp_gem_level(cand_data, gem_level)
        new_socket = GemSocket(cand_key, cand_lvl, 20)
        new_build = _clone_build(build)
        new_build.supports.append(new_socket)
        vr = validate(new_build)
        if not include_invalid and not vr.valid:
            continue
        snap = _measure(new_build)
        changes.append(_diff_to_change("ADD", [], [new_socket], baseline, snap, vr))

    return changes


def print_top_subsets(changes: list[BuildChange], *, n: int = 8,
                      baseline_was_invalid: bool = False):
    """Print best subsets by DPS, EHP, and QoL gain. Deduplicates first."""
    if not changes:
        print("(no subset changes evaluated)")
        return

    if baseline_was_invalid:
        print()
        print("NOTE: baseline build is INVALID (impossible reservation).")
        print("      The deltas below are vs that invalid baseline, so every")
        print("      VALID alternative will likely show a DPS loss. Pick the")
        print("      least-bad one or fix the baseline's reservation first.")

    deduped = _dedup_subsets(changes)
    print(f"\n(deduplicated {len(changes)} candidates -> {len(deduped)} unique outcomes)")

    print(f"\nTop {n} subsets by DPS delta:")
    for c in sorted(deduped, key=lambda c: c.dps_delta, reverse=True)[:n]:
        print(c.summary())

    print(f"\nTop {n} subsets by EHP-vs-phys delta:")
    for c in sorted(deduped, key=lambda c: c.ehp_delta_phys, reverse=True)[:n]:
        print(c.summary())

    qol_winners = [c for c in deduped if c.qol_immunity_gained or c.qol_movespeed_delta > 0]
    if qol_winners:
        print(f"\nTop {n} subsets by QoL gain (immunity or move speed):")
        def qol_score(c):
            return len(c.qol_immunity_gained) * 10 + max(0, c.qol_movespeed_delta)
        for c in sorted(qol_winners, key=qol_score, reverse=True)[:n]:
            print(c.summary())

    frontier = pareto_frontier(deduped)
    print(f"\nPareto frontier ({len(frontier)} non-dominated subsets — the real trade-offs):")
    for c in sorted(frontier, key=lambda c: c.dps_delta, reverse=True):
        print(c.summary())


def print_top_swaps(changes: list[BuildChange], *, n: int = 10,
                    show_pareto: bool = True):
    """Print top-N changes by each axis, then the Pareto frontier (trade-offs
    that aren't strictly worse than another option)."""
    valid = [c for c in changes if c.valid_after]
    if not valid:
        print("(no valid swaps found)")
    else:
        print(f"\nTop {n} valid swaps by DPS gain:")
        for c in sorted(valid, key=lambda c: c.dps_delta, reverse=True)[:n]:
            print(c.summary())

        print(f"\nTop {n} valid swaps by EHP-vs-phys gain:")
        for c in sorted(valid, key=lambda c: c.ehp_delta_phys, reverse=True)[:n]:
            print(c.summary())

        qol_winners = [c for c in valid if c.qol_immunity_gained]
        if qol_winners:
            print(f"\nTop {n} valid swaps adding ailment immunity:")
            for c in sorted(qol_winners, key=lambda c: len(c.qol_immunity_gained), reverse=True)[:n]:
                print(c.summary())

        if show_pareto:
            frontier = pareto_frontier(valid)
            print(f"\nPareto frontier ({len(frontier)} non-dominated valid swaps):")
            for c in sorted(frontier, key=lambda c: c.dps_delta, reverse=True):
                print(c.summary())

    invalid = [c for c in changes if not c.valid_after]
    high_dps_invalid = sorted(invalid, key=lambda c: c.dps_delta, reverse=True)[:n]
    if high_dps_invalid:
        print(f"\nTop {n} INVALID swaps by DPS gain "
              "(would need reservation reduction or other fix):")
        for c in high_dps_invalid:
            print(c.summary())
        if show_pareto:
            invalid_frontier = pareto_frontier(invalid)
            print(f"\nPareto frontier of INVALID swaps "
                  f"({len(invalid_frontier)} non-dominated):")
            for c in sorted(invalid_frontier, key=lambda c: c.dps_delta, reverse=True)[:n]:
                print(c.summary())
