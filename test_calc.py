"""Smoke-test the stat builder against a few known skills."""
import calc
import pob_data


def dump_stats(name: str, gem_level: int, quality: int = 0, actor_level: int | None = None):
    skill = pob_data.get_skill(name)
    if skill is None:
        print(f"\n--- {name}: NOT FOUND ---")
        return
    stats = calc.build_skill_instance_stats(skill, gem_level, quality, actor_level)
    print(f"\n--- {skill.get('name')} (gem lvl {gem_level}, quality {quality}, "
          f"actor lvl {actor_level or 'default(levelRequirement)'}) ---")
    for s, v in stats.items():
        print(f"  {s:60s} = {v}")


# Main test skill at endgame
dump_stats("FrostBolt", gem_level=20, quality=20, actor_level=90)

# Same gem level but with the gem's own default actor level (gem's req = 70 at lvl 20)
dump_stats("FrostBolt", gem_level=20, quality=20)

# Lower gem level, leveling actor
dump_stats("FrostBolt", gem_level=1, quality=0, actor_level=1)

# An aura
dump_stats("Hatred", gem_level=20, quality=20, actor_level=90)
dump_stats("Discipline", gem_level=20, quality=0, actor_level=90)

# A support gem
dump_stats("SupportControlledDestruction", gem_level=20, quality=20, actor_level=90)
dump_stats("SupportAddedColdDamage", gem_level=20, quality=20, actor_level=90)
dump_stats("SupportSpellEcho", gem_level=20, quality=20, actor_level=90)
dump_stats("SupportHypothermia", gem_level=20, quality=20, actor_level=90)
dump_stats("SupportInspiration", gem_level=20, quality=20, actor_level=90)
dump_stats("SupportColdPenetration", gem_level=20, quality=20, actor_level=90)
dump_stats("HeraldOfIce", gem_level=20, quality=20, actor_level=90)
dump_stats("Determination", gem_level=20, quality=20, actor_level=90)
dump_stats("Grace", gem_level=20, quality=20, actor_level=90)
dump_stats("Anger", gem_level=20, quality=20, actor_level=90)
dump_stats("Malevolence", gem_level=20, quality=20, actor_level=90)
dump_stats("Vitality", gem_level=20, quality=20, actor_level=90)
