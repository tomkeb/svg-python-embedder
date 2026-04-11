#!/usr/bin/env python3
"""
embed_fonts.py — Subset and embed fonts into an SVG file.

Usage (auto-discover fonts from the script directory):
    python embed_fonts.py folder/

Usage (explicit font mappings):
    python embed_fonts.py folder/ --fonts "Roboto=Roboto-Regular.ttf" "Roboto:bold=Roboto-Bold.ttf"

When --fonts is omitted, every TTF/OTF file found in --fonts-dir (default:
the directory containing embed_fonts.py) is read and its embedded family
name, weight, and style are used to build the mapping automatically.

Each --fonts entry maps  "FamilyName[:weight][:style]=path/to/font.ttf"
If no weight/style qualifier, it matches font-weight:normal, font-style:normal.

The script:
  1. Parses the SVG and collects every character used per (family, weight, style).
  2. Subsets each matched font TTF to only those glyphs.
  3. Converts to WOFF and base64-encodes it.
  4. Injects @font-face rules into <defs><style>.
  5. Strips Inkscape/sodipodi metadata (optional, --clean flag).
"""

import argparse
import base64
from collections import defaultdict
import io
import re
from pathlib import Path
from xml.etree import ElementTree as ET

# fonttools
from fontTools.ttLib import TTFont
from fontTools.subset import Subsetter, Options

SVG_NS = "http://www.w3.org/2000/svg"

# Register namespaces we want to KEEP (without prefix)
ET.register_namespace("", SVG_NS)
ET.register_namespace("xlink", "http://www.w3.org/1999/xlink")
# Pre-register Inkscape/sodipodi so ET doesn't crash on them
ET.register_namespace("inkscape", "http://www.inkscape.org/namespaces/inkscape")
ET.register_namespace("sodipodi", "http://sodipodi.sourceforge.net/DTD/sodipodi-0.dtd")

# ── helpers ──────────────────────────────────────────────────────────────────

def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_style_declarations(style_text: str | None) -> dict[str, str]:
    if not style_text:
        return {}

    declarations: dict[str, str] = {}
    for chunk in style_text.split(";"):
        if ":" not in chunk:
            continue
        name, value = chunk.split(":", 1)
        declarations[name.strip().lower()] = value.strip()
    return declarations


def normalize_font_family(value: str | None) -> str:
    if not value:
        return ""
    return value.split(",", 1)[0].strip().strip("\"'").lower()


def normalize_font_weight(value: str | None) -> str:
    if not value:
        return "normal"

    weight = value.strip().lower()
    if weight in {"normal", "400"}:
        return "normal"
    if weight in {"bold", "700"}:
        return "bold"
    return weight


def normalize_font_style(value: str | None) -> str:
    if not value:
        return "normal"

    style = value.strip().lower()
    if style in {"italic", "oblique"}:
        return style
    return "normal"


def resolve_font_usage(
    elem,
    inherited: dict[str, str],
) -> dict[str, str]:
    declarations = parse_style_declarations(elem.get("style"))

    family = elem.get("font-family", inherited["family"])
    weight = elem.get("font-weight", inherited["weight"])
    style = elem.get("font-style", inherited["style"])

    family = declarations.get("font-family", family)
    weight = declarations.get("font-weight", weight)
    style = declarations.get("font-style", style)

    return {
        "family": normalize_font_family(family),
        "weight": normalize_font_weight(weight),
        "style": normalize_font_style(style),
    }


def add_text_chars(
    usage: dict[tuple[str, str, str], set[str]],
    font_key: tuple[str, str, str],
    text: str,
) -> None:
    family, _, _ = font_key
    if not family:
        return

    for char in text:
        if char not in "\n\r\t":
            usage[font_key].add(char)


def collect_text_usage(root) -> dict[tuple[str, str, str], set[str]]:
    """Return characters used by each concrete (family, weight, style) text run."""
    usage: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    text_tags = {"text", "tspan", "textPath"}

    def visit(elem, inherited: dict[str, str], in_text_context: bool) -> None:
        current = resolve_font_usage(elem, inherited)
        current_in_text = in_text_context or local_name(elem.tag) in text_tags
        current_key = (current["family"], current["weight"], current["style"])

        if current_in_text and elem.text:
            add_text_chars(usage, current_key, elem.text)

        for child in elem:
            visit(child, current, current_in_text)
            if current_in_text and child.tail:
                add_text_chars(usage, current_key, child.tail)

    visit(root, {"family": "", "weight": "normal", "style": "normal"}, False)
    return dict(usage)


def subset_to_woff(ttf_path: str, unicodes: set[str]) -> bytes:
    """Subset a TTF to the given characters and return WOFF bytes."""
    text = "".join(sorted(unicodes))
    options = Options()
    options.flavor = "woff"
    options.hinting = False          # drop hinting tables (cvt/fpgm/prep) — not used by browsers
    options.desubroutinize = True    # inline CFF subroutines before subsetting

    font = TTFont(ttf_path)
    subsetter = Subsetter(options=options)
    subsetter.populate(text=text)
    subsetter.subset(font)

    buf = io.BytesIO()
    font.save(buf)
    return buf.getvalue()


def woff_to_data_uri(woff_bytes: bytes) -> str:
    b64 = base64.b64encode(woff_bytes).decode("ascii")
    return f"data:application/font-woff;charset=utf-8;base64,{b64}"


def build_font_face(family: str, weight: str, style: str, data_uri: str) -> str:
    return (
        f'@font-face{{'
        f'font-family:"{family}";'
        f'src:url({data_uri}) format("woff");'
        f'font-weight:{weight};font-style:{style};}}'
    )


def parse_font_arg(arg: str) -> tuple[str, str, str, str]:
    """
    Parse  "Family[:bold][:italic]=path/to/font.ttf"
    Returns (family, weight, style, path)
    """
    lhs, path = arg.rsplit("=", 1)
    parts = lhs.split(":")
    family = parts[0]
    weight = "normal"
    style  = "normal"
    for part in parts[1:]:
        p = part.lower()
        if p in ("bold", "700"):
            weight = "bold"
        elif p in ("italic", "oblique"):
            style = p
        elif p.isdigit():
            weight = p
    return family, weight, style, path


# ── font auto-discovery ───────────────────────────────────────────────────────

_WEIGHT_KEYWORDS: list[tuple[str, str]] = [
    ("extralight", "200"), ("extra light", "200"),
    ("ultralight", "200"), ("ultra light", "200"),
    ("thin", "100"),
    ("light", "300"),
    ("medium", "500"),
    ("semibold", "600"), ("semi bold", "600"),
    ("demibold", "600"), ("demi bold", "600"),
    ("extrabold", "800"), ("extra bold", "800"),
    ("ultrabold", "800"), ("ultra bold", "800"),
    ("black", "900"), ("heavy", "900"),
    ("bold", "bold"),
]


def read_font_metadata(path: str) -> tuple[str, str, str]:
    """Read (family, weight, style) from a font file's name table."""
    font = TTFont(path)
    name_table = font["name"]

    def best(nameID: int) -> str:
        rec = name_table.getName(nameID, 3, 1, 0x0409)  # Windows/English
        if rec is None:
            for r in name_table.names:
                if r.nameID == nameID:
                    rec = r
                    break
        return rec.toUnicode() if rec else ""

    # Prefer typographic names (16/17) over legacy (1/2)
    family   = (best(16) or best(1)).lower()
    subfamily = best(17) or best(2) or "Regular"
    font.close()

    sf = subfamily.lower()

    weight = "normal"
    for keyword, w in _WEIGHT_KEYWORDS:
        if keyword in sf:
            weight = w
            break

    style = "normal"
    if "oblique" in sf:
        style = "oblique"
    elif "italic" in sf:
        style = "italic"

    return family, weight, style


def discover_fonts(fonts_dir: Path) -> dict[tuple[str, str, str], str]:
    """Scan *fonts_dir* for TTF/OTF files and return a font map keyed by
    (family, weight, style)."""
    font_map: dict[tuple[str, str, str], str] = {}
    globs = ["*.ttf", "*.otf", "*.TTF", "*.OTF"]
    for pattern in globs:
        for font_path in sorted(fonts_dir.glob(pattern)):
            try:
                family, weight, style = read_font_metadata(str(font_path))
            except Exception as exc:
                print(f"  Warning: could not read {font_path.name}: {exc}")
                continue
            if not family:
                continue
            key = (family, weight, style)
            if key not in font_map:          # first match wins
                font_map[key] = str(font_path)
    return font_map


def strip_inkscape(svg_text: str) -> str:
    """Remove Inkscape/sodipodi namespaces, elements, and attributes."""
    # Remove xmlns declarations
    svg_text = re.sub(r'\s+xmlns:(inkscape|sodipodi)="[^"]*"', "", svg_text)
    # Remove inkscape:/sodipodi: attributes on any element
    svg_text = re.sub(r'\s+(inkscape|sodipodi):[a-zA-Z\-:]+="[^"]*"', "", svg_text)
    # Remove <sodipodi:…/> self-closing elements
    svg_text = re.sub(r'\s*<sodipodi:[^>]*/>', "", svg_text)
    # Remove <sodipodi:…>…</sodipodi:…> block elements
    svg_text = re.sub(r'\s*<sodipodi:.*?</sodipodi:[^>]+>', "", svg_text, flags=re.DOTALL)
    # Remove <inkscape:…> elements
    svg_text = re.sub(r'\s*<inkscape:.*?(?:</inkscape:[^>]+>|/>)', "", svg_text, flags=re.DOTALL)
    return svg_text


# ── helpers (font injection) ──────────────────────────────────────────────────

# Matches a complete @font-face { … } block (handles nested braces naively via
# a simple brace-counting scan — safe enough for generated CSS).
_FONT_FACE_RE = re.compile(r'@font-face\s*\{[^}]*\}', re.DOTALL)


def strip_font_face_rules(css: str) -> str:
    """Remove all @font-face blocks from a CSS string."""
    return _FONT_FACE_RE.sub("", css)


def process_svg(svg_path: Path, font_map: dict[tuple[str, str, str], str], clean: bool) -> bool:
    """Process one SVG. Returns True if the file was written."""
    tree = ET.parse(svg_path)
    root = tree.getroot()

    char_map = collect_text_usage(root)

    # Build @font-face CSS for each matched font
    css_rules: list[str] = []
    for family, weight, style in sorted(char_map):
        font_key = (family, weight, style)
        chars = char_map[font_key]
        ttf_path = font_map.get(font_key)
        if ttf_path is None:
            continue
        woff_bytes = subset_to_woff(ttf_path, chars)
        data_uri = woff_to_data_uri(woff_bytes)
        css_rules.append(build_font_face(family, weight, style, data_uri))
        print(f"  embed '{family}' {weight}/{style}: {len(chars)} chars, {len(woff_bytes):,} B")

    if not css_rules:
        if not char_map:
            print("  skip — no text")
        else:
            missing = ", ".join(
                f"'{f}' {w}" for f, w, s in sorted(char_map)
                if font_map.get((f, w, s)) is None
            )
            print(f"  skip — no font match for: {missing}")
        return False

    new_css = "".join(css_rules)

    # Inject into <defs><style> (create if absent)
    defs = root.find(f"{{{SVG_NS}}}defs")
    if defs is None:
        defs = ET.SubElement(root, f"{{{SVG_NS}}}defs")
        root.insert(0, defs)

    style_elem = defs.find(f"{{{SVG_NS}}}style")
    if style_elem is None:
        style_elem = ET.SubElement(defs, f"{{{SVG_NS}}}style")
        style_elem.text = ""

    # Strip any pre-existing @font-face rules, then prepend fresh ones
    existing = strip_font_face_rules(style_elem.text or "")
    style_elem.text = new_css + existing

    # Serialise
    ET.indent(tree, space="")  # compact
    out_text = ET.tostring(root, encoding="unicode", xml_declaration=False)

    if clean:
        out_text = strip_inkscape(out_text)

    svg_path.write_text(out_text, encoding="utf-8")
    return True


# ── main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Embed subsetted fonts into every SVG in a folder (in-place)."
    )
    parser.add_argument("folder", help="Folder containing SVG files to process")
    parser.add_argument(
        "--fonts", nargs="+", metavar="FAMILY[[:WEIGHT][:STYLE]]=FONT.ttf",
        help='Explicit font mappings, e.g. "Roboto=Roboto-Regular.ttf". '
             'If omitted, fonts are auto-discovered from --fonts-dir.'
    )
    parser.add_argument(
        "--fonts-dir", metavar="DIR",
        help="Directory to scan for font files when --fonts is not given "
             "(default: directory containing embed_fonts.py)"
    )
    parser.add_argument("--clean", action="store_true",
                        help="Strip Inkscape/sodipodi metadata from output")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        parser.error(f"Not a directory: {folder}")

    # Build font map — explicit mappings take precedence over auto-discovery
    font_map: dict[tuple[str, str, str], str] = {}
    if args.fonts:
        for arg in args.fonts:
            family, weight, style, path = parse_font_arg(arg)
            font_map[(family, weight, style)] = path
        print(f"Using {len(font_map)} explicit font mapping(s)")
    else:
        fonts_dir = Path(args.fonts_dir) if args.fonts_dir else Path(__file__).parent
        font_map = discover_fonts(fonts_dir)
        if not font_map:
            print("No font files found. Drop TTF/OTF files next to the script, "
                  "or use --fonts / --fonts-dir to specify them.")
            return
        print(f"Discovered {len(font_map)} font(s) in {fonts_dir}")

    svgs = sorted(folder.glob("*.svg"))
    if not svgs:
        print(f"No SVG files found in {folder}")
        return

    print(f"Processing {len(svgs)} SVG(s) in {folder}")
    written = 0
    for svg_path in svgs:
        print(f"[{svg_path.name}]")
        if process_svg(svg_path, font_map, args.clean):
            written += 1

    print(f"\nDone. Wrote {written}/{len(svgs)} file(s).")


if __name__ == "__main__":
    main()
