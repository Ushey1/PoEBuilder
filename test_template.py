"""End-to-end: pull a Frostbolt template from poe.ninja, validate, calc DPS+EHP+QoL."""
from build import validate, PLAYSTYLE_MAPPING, PLAYSTYLE_BOSSING
import calc
import defense
import qol
import ninja_template


def divider(t): print(f"\n{'='*60}\n {t}\n{'='*60}")


divider("Pulling Frostbolt template from poe.ninja")
build, meta = ninja_template.template_for_skill("Frostbolt", n_supports=5, n_auras=3)
build.playstyle = PLAYSTYLE_MAPPING
ninja_template.print_template_summary(build, meta)

divider("Validation")
print(validate(build).report())

divider("DPS")
print(calc.compute_dps(build).report())

divider("Defense")
print(defense.compute_ehp(build).report())

divider(f"QoL (playstyle = {build.playstyle})")
print(qol.compute_qol(build).report())
