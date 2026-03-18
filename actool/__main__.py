"""
actool - Asset Catalog Tool

CLI-compatible reimplementation of Apple's actool for compiling xcassets.
"""

import argparse
import json
import plistlib
import sys

from .compiler import compile_catalog


BUNDLE_VERSION = "24506"
SHORT_BUNDLE_VERSION = "26.3"


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


def _print_human_readable(data, indent=0, top_level=True):
    """Print a plist dict in Apple's human-readable text format.

    Top-level keys use /* key */ comment style.
    Nested dicts use 'key: value' with aligned indentation.
    Arrays list items at the current indentation.
    """
    prefix = " " * indent
    if isinstance(data, dict):
        for k, v in data.items():
            if top_level:
                print(f"/* {k} */")
                _print_human_readable(v, 0, top_level=False)
            elif isinstance(v, dict):
                label = f"{k}: "
                print(f"{prefix}{label}")
                _print_human_readable(v, indent + len(label),
                                      top_level=False)
            elif isinstance(v, list):
                label = f"{k}: "
                # Print label with trailing spaces to align children
                child_indent = indent + len(label)
                print(f"{prefix}{label}", end="")
                # Pad to child indent width
                print()
                _print_hr_array(v, child_indent)
            else:
                val = _hr_format_value(v)
                print(f"{prefix}{k}: {val}")
    elif isinstance(data, list):
        _print_hr_array(data, indent)
    else:
        print(f"{prefix}{_hr_format_value(data)}")


def _hr_format_value(v):
    """Format a scalar value for human-readable output."""
    if isinstance(v, float) and v == int(v):
        return str(int(v))
    return str(v)


def _print_hr_array(items, indent):
    """Print array items in human-readable format."""
    prefix = " " * indent
    for i, item in enumerate(items):
        if isinstance(item, dict):
            _print_human_readable(item, indent, top_level=False)
            if i < len(items) - 1:
                print(prefix)
        elif isinstance(item, list):
            _print_hr_array(item, indent)
        else:
            print(f"{prefix}{_hr_format_value(item)}")


def main():
    parser = argparse.ArgumentParser(
        prog="actool",
        description="Compiles, prints, updates, and verifies asset catalogs.")

    parser.add_argument("document", nargs="?", help="Path to .xcassets document")

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
    parser.add_argument("--enable-on-demand-resources",
                        metavar="YES/NO", default="NO",
                        help="Enable on-demand resources (accepted for "
                             "compatibility, no effect on macOS)")
    parser.add_argument("--development-region", metavar="REGION",
                        help="Development region (always included in output)")
    parser.add_argument("--include-language", metavar="LANG",
                        action="append", default=[],
                        help="Include only specified language(s). May be "
                             "repeated. Development region is always included.")
    parser.add_argument("--include-partial-info-plist-localizations",
                        choices=["yes", "no", "YES", "NO"],
                        default="YES",
                        help="Include CFBundleLocalizations in partial plist")

    # Listing
    parser.add_argument("--print-contents", action="store_true",
                        help="List the catalog's content in the output")

    # Version
    parser.add_argument("--version", action="store_true",
                        help="Print version information")

    args = parser.parse_args()

    # Handle --version (no document required)
    if args.version:
        version_data = {
            "com.apple.actool.version": {
                "bundle-version": BUNDLE_VERSION,
                "short-bundle-version": SHORT_BUNDLE_VERSION,
            }
        }
        _output_plist(version_data, args.output_format)
        return

    # All remaining commands require a document
    if not args.document:
        parser.error("a document path is required")

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

    include_langs = args.include_language or None
    plist_l10n = args.include_partial_info_plist_localizations.upper() != "NO"

    # Check if input is a .icon bundle
    from .icon_bundle import is_icon_bundle, compile_icon_bundle
    if is_icon_bundle(args.document):
        output_files = []
        try:
            output_files = compile_icon_bundle(
                icon_path=args.document,
                output_dir=args.compile,
                platform=args.platform,
                min_deploy=args.minimum_deployment_target,
                app_icon=args.app_icon,
                info_plist_path=args.output_partial_info_plist,
                accent_color=args.accent_color,
                standalone_icon_behavior=args.standalone_icon_behavior,
                warnings_list=warnings,
                notices_list=notices,
            )
        except Exception as e:
            errors.append({"description": str(e)})

        # Output results
        if args.output_format == "human-readable-text":
            results_data = {"com.apple.actool.compilation-results": output_files}
        else:
            results_data = {"com.apple.actool.compilation-results": {
                "output-files": output_files}}

        if notices and (args.notices or args.warnings):
            notices_data = {"com.apple.actool.notices": notices}
            _output_plist(notices_data, args.output_format)

        _output_plist(results_data, args.output_format)

        if errors:
            err_data = {"com.apple.actool.errors": errors}
            _output_plist(err_data, args.output_format)
            sys.exit(1)
        return

    output_files = []
    try:
        output_files = compile_catalog(
            xcassets_path=args.document,
            output_dir=args.compile,
            platform=args.platform,
            min_deploy=args.minimum_deployment_target,
            app_icon=args.app_icon,
            info_plist_path=args.output_partial_info_plist,
            accent_color=args.accent_color,
            widget_background_color=args.widget_background_color,
            standalone_icon_behavior=args.standalone_icon_behavior,
            include_languages=include_langs,
            development_region=args.development_region,
            plist_localizations=plist_l10n,
            warnings_list=warnings,
            errors_list=errors,
            notices_list=notices,
        )
    except FileNotFoundError as e:
        errors.append({"description": str(e)})
    except Exception as e:
        errors.append({"description": str(e)})

    # Output compilation results
    if args.output_format == "human-readable-text":
        # Apple's human-readable format lists files directly
        results_data = {
            "com.apple.actool.compilation-results": output_files,
        }
    else:
        results_data = {
            "com.apple.actool.compilation-results": {
                "output-files": output_files,
            }
        }
    _output_plist(results_data, args.output_format)

    # Output diagnostics
    if args.warnings and warnings:
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
