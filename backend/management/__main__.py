"""Release candidate tools; installation dispatch is supplied by the next stack layer."""

from backend.management.release import load_manifest

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        raise SystemExit("Pass a release manifest path to validate candidate metadata")
    from pathlib import Path

    print(load_manifest(Path(sys.argv[1]))["version"])
