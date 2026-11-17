"""Build the static website with APP_NAME embedded in HTML and OpenAPI."""

import argparse
from html import escape
import os
from pathlib import Path
import re
import sys
from urllib.parse import quote

from dotenv import dotenv_values

from factoryvpn_common.branding import DEFAULT_APP_NAME, normalize_app_name
from scripts.export_openapi import customer_schema, load_schema, render_schema

ROOT = Path(__file__).resolve().parents[1]
WEBSITE = ROOT / "website"
DEFAULT_OUTPUT = WEBSITE / "dist"
TOKEN = re.compile(r"\{\{(APP_NAME|APP_INITIAL|APP_FAVICON)\}\}")


def resolve_app_name(explicit: str | None = None, env_file: Path | None = None) -> str:
    """Select only the public label; never pass deployment settings to the exporter."""
    if explicit is not None:
        value = explicit
    elif "APP_NAME" in os.environ:
        value = os.environ["APP_NAME"]
    else:
        values = dotenv_values(env_file or ROOT / ".env", interpolate=False)
        value = values.get("APP_NAME", DEFAULT_APP_NAME)
        if value is None:
            raise ValueError("APP_NAME requires a value")
    return normalize_app_name(value)


def render_page(template: str, app_name: str) -> str:
    app_name = normalize_app_name(app_name)
    initial = app_name[0]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="7" fill="#132fea"/>'
        '<text x="16" y="23" text-anchor="middle" fill="white" '
        'font-family="sans-serif" font-size="24" font-weight="700">'
        f"{escape(initial)}</text></svg>"
    )
    values = {
        "APP_NAME": escape(app_name),
        "APP_INITIAL": escape(initial),
        "APP_FAVICON": "data:image/svg+xml," + quote(svg, safe=""),
    }
    # A callback leaves dollars and token-shaped text in the chosen name literal.
    return TOKEN.sub(lambda match: values[match[1]], template)


def build_files(app_name: str) -> dict[Path, bytes]:
    """Generate a complete public tree exclusively from its two source directories."""
    app_name = normalize_app_name(app_name)
    files = {
        path.relative_to(WEBSITE / "static"): path.read_bytes()
        for path in sorted((WEBSITE / "static").rglob("*"))
        if path.is_file()
    }
    for path in sorted((WEBSITE / "src").rglob("*.html")):
        files[path.relative_to(WEBSITE / "src")] = render_page(
            path.read_text(encoding="utf-8"), app_name
        ).encode("utf-8")
    files[Path("api/openapi.json")] = render_schema(
        customer_schema(load_schema(), app_name=app_name)
    ).encode("utf-8")
    return files


def build(output: Path, app_name: str, *, check: bool = False) -> bool:
    output = output.resolve()
    for source in (WEBSITE / "src", WEBSITE / "static"):
        source = source.resolve()
        if output == source or output in source.parents or source in output.parents:
            raise ValueError("Build output must be separate from website sources")
    files = build_files(app_name)
    existing = {
        path.relative_to(output) for path in output.rglob("*") if path.is_file()
    }
    extra = existing - files.keys()
    if extra:
        names = ", ".join(str(path) for path in sorted(extra))
        raise ValueError(f"Unexpected files in output; choose a clean directory: {names}")
    changed = [
        relative
        for relative, content in files.items()
        if not (output / relative).is_file() or not same_content(output / relative, content)
    ]
    if check:
        return not changed
    for relative in changed:
        target = output / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[relative])
    return True


def same_content(path: Path, expected: bytes) -> bool:
    actual = path.read_bytes()
    # Git's Windows checkout may convert generated text to CRLF.
    if path.suffix in {".html", ".css", ".js", ".json", ".xml", ".txt"} or path.name in {
        "LICENSE", "NOTICE", "VERSION"
    }:
        return actual.replace(b"\r\n", b"\n") == expected.replace(b"\r\n", b"\n")
    return actual == expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-name", help="Override APP_NAME from the environment or .env")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="Check output without writing")
    args = parser.parse_args()
    try:
        app_name = resolve_app_name(args.app_name)
        current = build(args.output, app_name, check=args.check)
    except ValueError as error:
        parser.error(str(error))
    if not current:
        print("Website build is stale. Run python -m scripts.build_website.", file=sys.stderr)
        return 1
    print(f"Website {'is up to date' if args.check else 'built'}: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
