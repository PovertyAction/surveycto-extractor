"""``surveycto-init`` -- write a starter ``config.toml`` into the current directory.

The blank template ships bundled inside the package (``templates/``); this copies
it into the working directory as ``config.toml`` for the user to fill in. It never
overwrites an existing config without ``--force``.
"""

import argparse
import sys
from importlib import resources
from pathlib import Path


def _template_text() -> str:
    return (
        resources.files("surveycto_extractor.templates") / "config.template.toml"
    ).read_text(encoding="utf-8")


def main() -> None:
    """Write a starter config.toml into the destination directory."""
    parser = argparse.ArgumentParser(
        prog="surveycto-init",
        description="Write a starter config.toml into the current directory.",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="config.toml",
        help="destination path (default: ./config.toml)",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="overwrite an existing file at the destination",
    )
    args = parser.parse_args()

    dest = Path(args.output)
    if dest.exists() and not args.force:
        print(
            f"ERROR: {dest} already exists. Use --force to overwrite.",
            file=sys.stderr,
        )
        sys.exit(1)

    dest.write_text(_template_text(), encoding="utf-8")
    print(f"Wrote {dest} -- edit it and fill in SURVEYS and DATASETS for your project.")


if __name__ == "__main__":
    main()
