"""Check the local ComfyUI files, and optionally query its loaded node inventory."""
import argparse
import json
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


def main():
    features = json.loads(Path(__file__).with_name("models.required.json").read_text(encoding="utf-8"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--comfy-root", type=Path, required=True)
    parser.add_argument("--feature", choices=features, action="append")
    parser.add_argument("--url", help="Running ComfyUI URL, for example http://127.0.0.1:8188")
    args = parser.parse_args()
    selected = [features[name] for name in (args.feature or ["i2v"])]
    root = args.comfy_root.resolve()
    files = {name for feature in selected for name in feature["files"]}
    nodes = {name for feature in selected for name in feature["nodes"]}
    missing = [str(root / name) for name in sorted(files | {"main.py"}) if not (root / name).is_file()]
    if args.url:
        base = args.url.rstrip("/")
        try:
            with urlopen(base + "/object_info", timeout=15) as response:
                loaded = json.load(response)
            missing.extend(f"Node: {node}" for node in sorted(nodes - loaded.keys()))
            with urlopen(base + "/easy", timeout=15) as response:
                if b"singleSegmentPlan" not in response.read():
                    missing.append("Huanzhen Engine /easy page")
        except (URLError, ValueError) as error:
            missing.append(f"ComfyUI HTTP check: {error}")
    if missing:
        print("Missing or unavailable:")
        print("\n".join(f"  {item}" for item in missing))
        return 1
    print("Required files and loaded nodes found." if args.url else "Required files found. Use --url to check loaded nodes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
