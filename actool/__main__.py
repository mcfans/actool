"""
actool - Asset Catalog Tool

CLI-compatible reimplementation of Apple's actool for compiling xcassets.
"""

import argparse
import json
import plistlib
import sys

from .compiler import compile_catalog


VERSION = "1.0.0"


def _output_plist(data: dict, fmt: str):
    """Output a plist dict in the requested format."""
    if fmt == "binary1":
        sys.stdout.buffer.write(plistlib.dumps(data, fmt=plistlib.FMT_BINARY))
    elif fmt == "human-readable-text":
        _print_human_readable(data)
    else:
        import re
        xml = plistlib.dumps(data, fmt=plistlib.FMT_XML)
        # Apple writes <real>16</real> not <real>16.0</real>
        xml = re.sub(rb"<real>(\d+)\.0</real>", rb"<real>\1</real>", xml)
        sys.stdout.buffer.write(xml)


def _print_human_readable(data, indent=0):
    """Print a plist dict in human-readable text format."""
    prefix = "  " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, (dict, list)):
                print(f"{prefix}{k}:")
                _print_human_readable(v, indent + 1)
            else:
                print(f"{prefix}{k}: {v}")
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                _print_human_readable(item, indent)
            else:
                print(f"{prefix}- {item}")
    else:
        print(f"{prefix}{data}")


def main():
    parser = argparse.ArgumentParser(
        prog="actool",
        description="Compiles, prints, updates, and verifies asset catalogs.")

    parser.add_argument("document", help="Path to .xcassets document")

    # Output format
    parser.add_argument("--output-format", default="xml1",
                        choices=["xml1", "binary1", "human-readable-text"],
                        help="Output format (default: xml1)")

    # Compiling
    parser.add_argument("--compile", metavar="PATH",
                        help="Compile document and write output to PATH")
    parser.add_argument("--warnings", action="store_true",
                        help="Include document warnings in output")
    parser.add_argument("--errors", action="store_true",
                        help="Include document errors in output")
    parser.add_argument("--notices", action="store_true",
                        help="Include document notices in output")
    parser.add_argument("--output-partial-info-plist", metavar="PATH",
                        help="Emit a partial info plist to PATH")

    # App icon
    parser.add_argument("--app-icon", metavar="NAME",
                        help="Select a primary app icon")
    parser.add_argument("--include-all-app-icons", action="store_true",
                        help="Include all app icon assets in the CAR file")
    parser.add_argument("--alternate-app-icon", metavar="NAME",
                        action="append", default=[],
                        help="Include an additional app icon set")

    # Colors
    parser.add_argument("--accent-color", metavar="NAME",
                        help="Select a named accent color")
    parser.add_argument("--widget-background-color", metavar="NAME",
                        help="Select a named widget background color")

    # Platform / deployment
    parser.add_argument("--platform", default="macosx",
                        help="Target platform (default: macosx)")
    parser.add_argument("--minimum-deployment-target", default="11.0",
                        help="Minimum deployment target version")
    parser.add_argument("--target-device", metavar="DEVICE",
                        action="append", default=[],
                        help="Target device (may be specified multiple times)")

    # Icon behavior
    parser.add_argument("--standalone-icon-behavior",
                        choices=["default", "all", "none"],
                        default="default",
                        help="Control loose icon file generation "
                             "(default/all/none)")

    # Misc compilation options
    parser.add_argument("--compress-pngs", action="store_true",
                        help="Compress PNGs (no effect on CAR contents)")
    parser.add_argument("--skip-app-store-deployment", action="store_true",
                        help="Skip App Store-specific validations")

    # Listing
    parser.add_argument("--print-contents", action="store_true",
                        help="List the catalog's content in the output")

    # Version
    parser.add_argument("--version", action="store_true",
                        help="Print version information")

    args = parser.parse_args()

    # Handle --version
    if args.version:
        version_data = {
            "com.apple.actool.version": {
                "bundle-version": VERSION,
                "short-bundle-version": VERSION,
            }
        }
        _output_plist(version_data, args.output_format)
        return

    # Handle --print-contents (without --compile)
    if args.print_contents and not args.compile:
        from .catalog import list_catalog_contents
        contents_tree = list_catalog_contents(args.document)
        contents_data = {
            "com.apple.actool.catalog-contents": [contents_tree]
        }
        _output_plist(contents_data, args.output_format)
        return

    # --compile is required for actual work
    if not args.compile:
        parser.error("--compile is required")

    warnings = []
    errors = []
    notices = []

    try:
        compile_catalog(
            xcassets_path=args.document,
            output_dir=args.compile,
            platform=args.platform,
            min_deploy=args.minimum_deployment_target,
            app_icon=args.app_icon,
            info_plist_path=args.output_partial_info_plist,
            accent_color=args.accent_color,
            widget_background_color=args.widget_background_color,
            standalone_icon_behavior=args.standalone_icon_behavior,
            warnings_list=warnings,
            errors_list=errors,
            notices_list=notices,
        )
    except FileNotFoundError as e:
        errors.append({"message": str(e), "type": "error"})
    except Exception as e:
        errors.append({"message": str(e), "type": "error"})

    # Build output
    if args.warnings and warnings:
        # Output warnings
        output = {"com.apple.actool.document.warnings": warnings}
        _output_plist(output, args.output_format)

    if args.errors and errors:
        output = {"com.apple.actool.document.errors": errors}
        _output_plist(output, args.output_format)
        sys.exit(1)

    if args.notices and notices:
        output = {"com.apple.actool.document.notices": notices}
        _output_plist(output, args.output_format)


if __name__ == "__main__":
    main()
