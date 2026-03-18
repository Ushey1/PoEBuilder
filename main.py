import requests
import blackboxprotobuf
import sys

BASE_URL = "https://poe.ninja/poe1/api"


def get_snapshot_version(league, build_type):
    """Fetch the current snapshot version for a league."""
    resp = requests.get(f"{BASE_URL}/data/index-state")
    if resp.status_code != 200:
        return None
    for sv in resp.json().get("snapshotVersions", []):
        if sv["url"] == league and sv["type"] == build_type:
            return sv["version"]
    return None


def fetch_search(snapshot_version, league, build_type, skill=None):
    """Fetch build search data, optionally filtered by skill."""
    url = f"{BASE_URL}/builds/{snapshot_version}/search"
    params = {"overview": league, "type": build_type}
    if skill:
        params["skills"] = skill
    resp = requests.get(url, params=params)
    if resp.status_code != 200:
        return None
    msg, _ = blackboxprotobuf.decode_message(resp.content)
    return msg["1"]


def fetch_dictionary(dict_hash):
    """Fetch a dictionary and return list of names indexed by ID."""
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


def print_table(title, data, total, top_n=15):
    """Print a formatted table with percentages."""
    print(f"\n{'='*55}")
    print(f" {title}")
    print(f"{'='*55}")
    for i, (name, count) in enumerate(data[:top_n]):
        pct = (count / total) * 100 if total > 0 else 0
        bar = "#" * int(pct / 2)
        print(f"  {i+1:2}. {name:<30} {count:>7} ({pct:5.1f}%) {bar}")


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

    # Step 1: Get snapshot version
    print(f"Fetching data for '{skill_input}' in Mirage league...")
    version = get_snapshot_version(league, build_type)
    if not version:
        print("Could not find snapshot version.")
        return

    # Step 2: Fetch filtered search data
    root = fetch_search(version, league, build_type, skill=skill_input)
    if not root:
        print(f"No results found for '{skill_input}'.")
        return

    total = root["1"]
    print(f"Characters using '{skill_input}': {total}")

    # Step 3: Get dictionaries
    dict_hashes = get_dict_hashes(root)
    class_names = fetch_dictionary(dict_hashes.get("class", ""))
    ascendancy_names = fetch_dictionary(dict_hashes.get("secondascendancy", ""))
    item_names = fetch_dictionary(dict_hashes.get("item", ""))
    gem_names = fetch_dictionary(dict_hashes.get("gem", ""))

    # Step 4: Parse and display
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


if __name__ == "__main__":
    main()