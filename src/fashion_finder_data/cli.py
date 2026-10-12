"""Placeholder CLI until collection, normalization, and storage are implemented."""
from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fashion-finder",
        description="Local product-data pipeline for Fashion Finder",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="fashion-finder-data 0.1.0 (planning scaffold)",
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["sources"],
        help="Available commands will be implemented in the first engineering milestone.",
    )
    parser.parse_args()
    parser.print_help()


if __name__ == "__main__":
    main()
