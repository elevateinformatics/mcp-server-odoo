#!/usr/bin/env python3
"""Build script for creating optimized executables using Nuitka.

This script compiles mcp-server-odoo into a standalone executable with
improved startup time and runtime performance.

Usage:
    # Install build dependencies first
    pip install -e ".[build]"

    # Build standalone executable
    python scripts/build_nuitka.py

    # Build with specific options
    python scripts/build_nuitka.py --onefile --output-dir=dist
"""

import argparse
import subprocess
import sys
from pathlib import Path


def get_project_root() -> Path:
    """Get the project root directory."""
    return Path(__file__).parent.parent


def build_nuitka(
    onefile: bool = True,
    output_dir: str = "dist",
    output_name: str = "mcp-server-odoo",
) -> int:
    """Build the project using Nuitka.

    Args:
        onefile: Create a single executable file
        output_dir: Output directory for the build
        output_name: Name of the output executable

    Returns:
        Return code from Nuitka
    """
    project_root = get_project_root()
    entry_point = project_root / "mcp_server_odoo" / "__main__.py"

    if not entry_point.exists():
        print(f"Error: Entry point not found: {entry_point}")
        return 1

    # Base Nuitka command
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        # Include the package
        "--include-package=mcp_server_odoo",
        # Follow imports
        "--follow-imports",
        # Output settings
        f"--output-dir={output_dir}",
        f"--output-filename={output_name}",
        # Performance optimizations
        "--enable-plugin=anti-bloat",
        "--python-flag=no_site",
        "--python-flag=no_warnings",
        "--python-flag=no_docstrings",
        # Exclude unnecessary packages
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=_pytest",
        "--nofollow-import-to=setuptools",
        "--nofollow-import-to=pip",
        "--nofollow-import-to=wheel",
        "--nofollow-import-to=distutils",
        "--nofollow-import-to=unittest",
        "--nofollow-import-to=test",
        # Assume yes to prompts
        "--assume-yes-for-downloads",
    ]

    # Add standalone/onefile options
    if onefile:
        cmd.append("--onefile")
    else:
        cmd.append("--standalone")

    # Add the entry point
    cmd.append(str(entry_point))

    print("Building with Nuitka...")
    print(f"Command: {' '.join(cmd)}")
    print()

    # Run Nuitka
    result = subprocess.run(cmd, cwd=project_root)

    if result.returncode == 0:
        print()
        print("Build successful!")
        print(f"Output: {output_dir}/{output_name}")
    else:
        print()
        print(f"Build failed with return code: {result.returncode}")

    return result.returncode


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Build mcp-server-odoo with Nuitka for optimized performance"
    )
    parser.add_argument(
        "--onefile",
        action="store_true",
        default=True,
        help="Create a single executable file (default: True)",
    )
    parser.add_argument(
        "--standalone",
        action="store_true",
        help="Create a standalone directory instead of onefile",
    )
    parser.add_argument(
        "--output-dir",
        default="dist",
        help="Output directory (default: dist)",
    )
    parser.add_argument(
        "--output-name",
        default="mcp-server-odoo",
        help="Output executable name (default: mcp-server-odoo)",
    )

    args = parser.parse_args()

    # If standalone is specified, disable onefile
    onefile = not args.standalone

    return build_nuitka(
        onefile=onefile,
        output_dir=args.output_dir,
        output_name=args.output_name,
    )


if __name__ == "__main__":
    sys.exit(main())
