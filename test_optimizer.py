"""Run the single-aura swap optimizer over the live Frostbolt template."""
from build import PLAYSTYLE_MAPPING
import calc
import defense
import qol
import optimizer
import ninja_template


def divider(t): print(f"\n{'='*60}\n {t}\n{'='*60}")


divider("Baseline: Frostbolt template from poe.ninja")
build, meta = ninja_template.template_for_skill("Frostbolt", n_supports=5, n_auras=3)
build.playstyle = PLAYSTYLE_MAPPING
ninja_template.print_template_summary(build, meta)

print()
print("Baseline metrics:")
print(f"  DPS:                {calc.compute_dps(build).dps:>10.0f}")
ehp = defense.compute_ehp(build)
print(f"  EHP vs phys attack: {ehp.ehp_vs_phys_attack:>10.0f}")
print(f"  EHP vs cold spell:  {ehp.ehp_vs_cold_spell:>10.0f}")
q = qol.compute_qol(build)
print(f"  Move speed:         {q.movement_speed_pct:>10.0f}%")
print(f"  Immune to:          {[k for k,v in q.immunities.items() if v]}")

divider("Enumerating single-aura swaps...")
changes = optimizer.enumerate_aura_swaps(build, include_invalid=True)
print(f"Evaluated {len(changes)} single-change candidates.")

optimizer.print_top_swaps(changes, n=10)

divider("Enumerating multi-aura subsets (sizes 1, 2, 3)...")
import time
from build import validate
t0 = time.time()
subset_changes = optimizer.enumerate_aura_subsets(build, sizes=(1, 2, 3), valid_only=True)
print(f"Evaluated {len(subset_changes)} valid subset candidates in {time.time()-t0:.1f}s.")

baseline_invalid = not validate(build).valid
optimizer.print_top_subsets(subset_changes, n=8, baseline_was_invalid=baseline_invalid)


divider("Enumerating support-gem swaps...")
t0 = time.time()
support_changes = optimizer.enumerate_support_swaps(build, include_invalid=True)
print(f"Evaluated {len(support_changes)} candidate changes in {time.time()-t0:.1f}s.")
optimizer.print_top_swaps(support_changes, n=10)
