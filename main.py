import requests
import blackboxprotobuf
import sys

BASE_URL = "https://poe.ninja/poe1/api"

# Time machine snapshots ordered by progression (early -> late)
PROGRESSION_STAGES = [
    ("hour-18", "Hour 18 (League Start)"),
    ("day-1", "Day 1"),
    ("day-3", "Day 3"),
    ("week-1", "Week 1"),
]


def get_snapshot_version(league, build_type):
    """Fetch the current snapshot version for a league."""
    resp = requests.get(f"{BASE_URL}/data/index-state")
    if resp.status_code != 200:
        return None, None
    data = resp.json()
    for sv in data.get("snapshotVersions", []):
        if sv["url"] == league and sv["type"] == build_type:
            return sv["version"], sv.get("timeMachineLabels", [])
    return None, None


def fetch_search(snapshot_version, league, build_type, skill=None, timemachine=None):
    """Fetch build search data, optionally filtered by skill and time machine."""
    url = f"{BASE_URL}/builds/{snapshot_version}/search"
    params = {"overview": league, "type": build_type}
    if skill:
        params["skills"] = skill
    if timemachine:
        params["timemachine"] = timemachine
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        return None
    msg, _ = blackboxprotobuf.decode_message(resp.content)
    return msg["1"]


def fetch_dictionary(dict_hash):
    """Fetch a dictionary and return list of names indexed by ID."""
    if not dict_hash:
        return []
    resp = requests.get(f"{BASE_URL}/builds/dictionary/{dict_hash}")
    if resp.status_code != 200:
        return []
    msg, _ = blackboxprotobuf.decode_message(resp.content)
    entries = msg.get("2", [])
    if not isinstance(entries, list):
        entries = [entries]
    names = []
    for entry in entries:
        if isinstance(entry, bytes):
            names.append(entry.decode("utf-8"))
        elif isinstance(entry, dict):
            for v in entry.values():
                if isinstance(v, bytes):
                    names.append(v.decode("utf-8"))
                    break
        else:
            names.append(str(entry))
    return names


def get_dict_hashes(root):
    """Extract dimension name -> dictionary hash mappings."""
    refs = root["6"]
    if not isinstance(refs, list):
        refs = [refs]
    hashes = {}
    for ref in refs:
        name = ref["1"].decode("utf-8") if isinstance(ref["1"], bytes) else ref["1"]
        hash_val = ref["2"].decode("utf-8") if isinstance(ref["2"], bytes) else ref["2"]
        hashes[name] = hash_val
    return hashes


def parse_dimension(dimensions, dim_name, name_lookup):
    """Parse a dimension and return sorted (name, count) pairs."""
    if not isinstance(dimensions, list):
        dimensions = [dimensions]
    for dim in dimensions:
        d_name = dim["1"].decode("utf-8") if isinstance(dim["1"], bytes) else dim["1"]
        if d_name != dim_name:
            continue
        entries = dim.get("3", [])
        if not isinstance(entries, list):
            entries = [entries]
        results = []
        for entry in entries:
            if isinstance(entry, dict):
                vals = list(entry.values())
                if len(vals) == 2:
                    idx, count = vals
                    name = name_lookup[idx] if idx < len(name_lookup) else f"Unknown({idx})"
                    results.append((name, count))
                elif len(vals) == 1:
                    results.append(("Unspecified", vals[0]))
        results.sort(key=lambda x: x[1], reverse=True)
        return results
    return []


def parse_dimension_as_dict(dimensions, dim_name, name_lookup):
    """Parse a dimension and return {name: count} dict."""
    data = parse_dimension(dimensions, dim_name, name_lookup)
    return {name: count for name, count in data}


def print_table(title, data, total, top_n=15):
    """Print a formatted table with percentages."""
    print(f"\n{'='*55}")
    print(f" {title}")
    print(f"{'='*55}")
    for i, (name, count) in enumerate(data[:top_n]):
        pct = (count / total) * 100 if total > 0 else 0
        bar = "#" * int(pct / 2)
        print(f"  {i+1:2}. {name:<30} {count:>7} ({pct:5.1f}%) {bar}")


def show_build_overview(skill_input, version, league, build_type):
    """Show class, ascendancy, skill, and item breakdown for a skill."""
    root = fetch_search(version, league, build_type, skill=skill_input)
    if not root:
        print(f"No results found for '{skill_input}'.")
        return

    total = root["1"]
    print(f"Characters using '{skill_input}': {total}")

    dict_hashes = get_dict_hashes(root)
    class_names = fetch_dictionary(dict_hashes.get("class", ""))
    ascendancy_names = fetch_dictionary(dict_hashes.get("secondascendancy", ""))
    item_names = fetch_dictionary(dict_hashes.get("item", ""))
    gem_names = fetch_dictionary(dict_hashes.get("gem", ""))

    dimensions = root["2"]
    if not isinstance(dimensions, list):
        dimensions = [dimensions]

    class_data = parse_dimension(dimensions, "class", class_names)
    print_table(f"BEST CLASSES for {skill_input}", class_data, total)

    ascendancy_data = parse_dimension(dimensions, "secondascendancy", ascendancy_names)
    print_table(f"SECOND ASCENDANCY for {skill_input}", ascendancy_data, total)

    skill_data = parse_dimension(dimensions, "skills", gem_names)
    print_table(f"COMMONLY PAIRED SKILLS with {skill_input}", skill_data, total, top_n=10)

    item_data = parse_dimension(dimensions, "items", item_names)
    print_table(f"POPULAR ITEMS for {skill_input}", item_data, total, top_n=15)


def show_progression(skill_input, version, league, build_type, available_labels):
    """Show skill tree progression heatmap across time machine snapshots."""
    # Filter stages to only those available for this league
    stages = [
        (label, display)
        for label, display in PROGRESSION_STAGES
        if label in available_labels
    ]

    if not stages:
        print("No time machine snapshots available for this league.")
        return

    print(f"\n{'='*70}")
    print(f" SKILL TREE PROGRESSION for {skill_input}")
    print(f"{'='*70}")

    # Fetch keypassive data at each time machine snapshot + latest
    keypassive_names = None
    snapshots = {}  # label -> {passive_name: usage_pct}

    # Fetch latest snapshot first (to get dictionary)
    print("\nFetching snapshots: latest", end="", flush=True)
    root = fetch_search(version, league, build_type, skill=skill_input)
    if not root:
        print(f"\nNo results for '{skill_input}'.")
        return

    dict_hashes = get_dict_hashes(root)
    keypassive_names = fetch_dictionary(dict_hashes.get("keypassive", ""))

    latest_total = root["1"]
    dims = root["2"] if isinstance(root["2"], list) else [root["2"]]
    latest_passives = parse_dimension_as_dict(dims, "keypassives", keypassive_names)
    snapshots["latest"] = {
        name: (count / latest_total * 100) if latest_total > 0 else 0
        for name, count in latest_passives.items()
    }

    # Fetch each time machine snapshot
    for label, display in stages:
        print(f", {label}", end="", flush=True)
        tm_root = fetch_search(version, league, build_type, skill=skill_input, timemachine=label)
        if not tm_root:
            continue
        tm_total = tm_root["1"]
        if tm_total == 0:
            continue
        tm_dims = tm_root["2"] if isinstance(tm_root["2"], list) else [tm_root["2"]]
        tm_passives = parse_dimension_as_dict(tm_dims, "keypassives", keypassive_names)
        snapshots[label] = {
            name: (count / tm_total * 100) if tm_total > 0 else 0
            for name, count in tm_passives.items()
        }

    print(" ...done!\n")

    # Collect all passive names that appear in any snapshot
    all_passives = set()
    for snap_data in snapshots.values():
        all_passives.update(snap_data.keys())

    # Remove "Unspecified" entries
    all_passives.discard("Unspecified")

    # Categorize passives by when they appear
    # "Early core" = high usage in early snapshots AND latest
    # "Late addition" = low/no usage early, high usage latest
    # "Leveling only" = high usage early, low usage latest

    early_label = stages[0][0] if stages else None
    late_label = stages[-1][0] if stages else None

    early_data = snapshots.get(early_label, {})
    late_data = snapshots.get(late_label, snapshots.get("latest", {}))
    latest_data = snapshots.get("latest", {})

    core_passives = []      # High early + high late (always take)
    late_passives = []       # Low early + high late (endgame picks)
    early_passives = []      # High early + low late (leveling/respec)

    for name in all_passives:
        early_pct = early_data.get(name, 0)
        latest_pct = latest_data.get(name, 0)

        if latest_pct < 3:
            continue  # Skip very low usage passives

        if early_pct >= 10 and latest_pct >= 10:
            core_passives.append((name, early_pct, latest_pct))
        elif early_pct < 5 and latest_pct >= 10:
            late_passives.append((name, early_pct, latest_pct))
        elif early_pct >= 10 and latest_pct < 10:
            early_passives.append((name, early_pct, latest_pct))

    # Sort each category by latest usage
    core_passives.sort(key=lambda x: x[2], reverse=True)
    late_passives.sort(key=lambda x: x[2], reverse=True)
    early_passives.sort(key=lambda x: x[2], reverse=True)

    # Display progression header with snapshot character counts
    print(f"  Snapshot character counts:")
    if early_label and early_label in snapshots:
        early_root = fetch_search(version, league, build_type, skill=skill_input, timemachine=early_label)
        if early_root:
            print(f"    {stages[0][1]}: {early_root['1']} characters")
    print(f"    Latest: {latest_total} characters")

    # Display categories
    print(f"\n  --- CORE PASSIVES (take early, keep always) ---")
    print(f"  {'Passive':<40} {'Early':>7} {'Latest':>7}")
    print(f"  {'-'*40} {'-'*7} {'-'*7}")
    for name, early_pct, latest_pct in core_passives[:20]:
        print(f"  {name:<40} {early_pct:6.1f}% {latest_pct:6.1f}%")

    if late_passives:
        print(f"\n  --- ENDGAME ADDITIONS (pick up later, levels 90+) ---")
        print(f"  {'Passive':<40} {'Early':>7} {'Latest':>7}")
        print(f"  {'-'*40} {'-'*7} {'-'*7}")
        for name, early_pct, latest_pct in late_passives[:15]:
            print(f"  {name:<40} {early_pct:6.1f}% {latest_pct:6.1f}%")

    if early_passives:
        print(f"\n  --- LEVELING PASSIVES (respec out later) ---")
        print(f"  {'Passive':<40} {'Early':>7} {'Latest':>7}")
        print(f"  {'-'*40} {'-'*7} {'-'*7}")
        for name, early_pct, latest_pct in early_passives[:10]:
            print(f"  {name:<40} {early_pct:6.1f}% {latest_pct:6.1f}%")

    # Full timeline view for top passives
    stage_labels = [label for label, _ in stages]
    all_labels = stage_labels + ["latest"]
    display_labels = [display for _, display in stages] + ["Latest"]

    # Get top 25 passives by latest usage
    top_passives = sorted(
        [(name, latest_data.get(name, 0)) for name in all_passives],
        key=lambda x: x[1],
        reverse=True
    )[:25]

    print(f"\n  --- FULL TIMELINE (top 25 passives by latest usage) ---")
    header = f"  {'Passive':<35}"
    for dl in display_labels:
        # Truncate display label to fit
        short = dl[:8]
        header += f" {short:>8}"
    print(header)
    print(f"  {'-'*35}" + f" {'-'*8}" * len(display_labels))

    for name, _ in top_passives:
        row = f"  {name:<35}"
        for label in all_labels:
            pct = snapshots.get(label, {}).get(name, 0)
            if pct >= 1:
                row += f" {pct:7.1f}%"
            else:
                row += f"       -"
        print(row)


def main():
    # Get skill from command line or prompt
    if len(sys.argv) > 1:
        skill_input = " ".join(sys.argv[1:])
    else:
        skill_input = input("Enter a skill name (e.g. Kinetic Blast): ").strip()

    if not skill_input:
        print("No skill provided.")
        return

    league = "mirage"
    build_type = "exp"

    # Get snapshot version and available time machine labels
    print(f"Fetching data for '{skill_input}' in Mirage league...")
    version, tm_labels = get_snapshot_version(league, build_type)
    if not version:
        print("Could not find snapshot version.")
        return

    # Show build overview
    show_build_overview(skill_input, version, league, build_type)

    # Show skill tree progression
    show_progression(skill_input, version, league, build_type, tm_labels)


if __name__ == "__main__":
    main()