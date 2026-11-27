#!/usr/bin/env python3
"""
Create ZIP Archive with ZIP64 Support
======================================

This script creates a ZIP archive with ZIP64 support for large files (>2GB).
It's used in the conda-pack build workflow to package the distribution.

PowerShell's Compress-Archive has a 2GB limitation, so we use Python's zipfile
module with allowZip64=True to handle large conda-packed environments.

Usage:
    python create_zip_archive.py <source_dir> <output_zip> [--compression-level LEVEL]

Arguments:
    source_dir: Directory to compress
    output_zip: Output ZIP file path
    --compression-level: Compression level (0-9, default: 6)

Example:
    python create_zip_archive.py dist-package unilab-pack-win-64.zip
"""

import argparse
import os
import sys
import zipfile
from pathlib import Path


def create_zip_archive(source_dir: str, output_zip: str, compression_level: int = 6) -> bool:
    """
    Create a ZIP archive with ZIP64 support.

    Args:
        source_dir: Directory to compress
        output_zip: Output ZIP file path
        compression_level: Compression level (0-9)

    Returns:
        bool: True if successful
    """
    try:
        source_path = Path(source_dir)
        output_path = Path(output_zip)

        # Validate source directory
        if not source_path.exists():
            print(f"Error: Source directory does not exist: {source_dir}", file=sys.stderr)
            return False

        if not source_path.is_dir():
            print(f"Error: Source path is not a directory: {source_dir}", file=sys.stderr)
            return False

        # Remove existing output file if present
        if output_path.exists():
            print(f"Removing existing archive: {output_path}")
            output_path.unlink()

        # Create ZIP archive
        print("=" * 70)
        print(f"Creating ZIP archive with ZIP64 support")
        print(f"  Source: {source_path.absolute()}")
        print(f"  Output: {output_path.absolute()}")
        print(f"  Compression: Level {compression_level}")
        print("=" * 70)

        total_size = 0
        file_count = 0

        with zipfile.ZipFile(
            output_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True, compresslevel=compression_level
        ) as zipf:
            # Walk through source directory
            for root, dirs, files in os.walk(source_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, source_dir)
                    file_size = os.path.getsize(file_path)

                    # Add file to archive
                    zipf.write(file_path, arcname)

                    # Display progress
                    total_size += file_size
                    file_count += 1
                    print(f"  [{file_count:3d}] Adding: {arcname:50s} {file_size:>15,} bytes")

        # Get final archive size
        archive_size = output_path.stat().st_size
        compression_ratio = (1 - archive_size / total_size) * 100 if total_size > 0 else 0

        # Display summary
        print("=" * 70)
        print("Archive created successfully!")
        print(f"  Files added: {file_count}")
        print(f"  Total size (uncompressed): {total_size:>15,} bytes ({total_size / (1024**3):.2f} GB)")
        print(f"  Archive size (compressed):  {archive_size:>15,} bytes ({archive_size / (1024**3):.2f} GB)")
        print(f"  Compression ratio: {compression_ratio:.1f}%")
        print("=" * 70)

        return True

    except Exception as e:
        print(f"Error creating ZIP archive: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc()
        return False


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Create ZIP archive with ZIP64 support for large files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python create_zip_archive.py dist-package unilab-pack-win-64.zip
  python create_zip_archive.py dist-package unilab-pack-win-64.zip --compression-level 9
        """,
    )

    parser.add_argument("source_dir", help="Directory to compress")

    parser.add_argument("output_zip", help="Output ZIP file path")

    parser.add_argument(
        "--compression-level",
        type=int,
        default=6,
        choices=range(0, 10),
        metavar="LEVEL",
        help="Compression level (0=no compression, 9=maximum compression, default=6)",
    )

    args = parser.parse_args()

    # Create archive
    success = create_zip_archive(args.source_dir, args.output_zip, args.compression_level)

    # Exit with appropriate code
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
