"""Build docs/k4cm-guide.pdf from docs/k4cm-guide.md.

The Markdown file stays the source of truth: it is what the client installers
ship and what Notepad opens on Windows. This only dresses it for print, with a
cover, a contents page, each `##` part opening on its own page, and numbered
`###` sections.

Rendering is headless Chrome's print-to-PDF, because it draws the box-drawing
diagram and tables faithfully and supports page-number footers; nothing else
needed is commonly installed. Needs the `markdown` package:

    python -m pip install markdown
    python scripts/guide_pdf.py
"""

from __future__ import annotations

import argparse
import html
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import markdown

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "docs" / "k4cm-guide.md"
TARGET = ROOT / "docs" / "k4cm-guide.pdf"

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    "google-chrome",
    "chromium",
]

# One line under each part's title, so a reader flipping pages knows where they are.
PART_BLURBS = {
    "Overview": "What k4cm is, and how a session runs from request to release.",
    "Admin guide": "Installing, configuring and running the site server.",
    "User guide": "Taking, extending and releasing a radio from home.",
    "Calendar guide": "Booking radios ahead, from the command line or the browser.",
    "Internals guide": "How the parts work, for anyone changing the code.",
}

# Code lines longer than this are set smaller so the diagram fits the page width.
WIDE_CODE = 78
# Tables with at most this many rows (header included) are never split across pages.
SHORT_TABLE = 9

CSS = """
@page {
  size: Letter;
  margin: 0.8in 0.75in 0.85in;
  @bottom-left { content: "k4cm Guide"; font: 8pt 'Helvetica Neue', Arial, sans-serif; color: #8a94a3; }
  @bottom-right { content: counter(page); font: 8pt 'Helvetica Neue', Arial, sans-serif; color: #8a94a3; }
}
@page cover { margin: 0; @bottom-left { content: none; } @bottom-right { content: none; } }

:root { --ink: #1d2733; --quiet: #5b6676; --accent: #1f5fa8; --accent-soft: #e8f0fa; --rule: #d8dee6; }
html { font: 10pt/1.5 'Helvetica Neue', Arial, sans-serif; color: var(--ink); }
body { margin: 0; }
a { color: var(--accent); text-decoration: none; }
p, li { orphans: 3; widows: 3; }
strong { font-weight: 600; }

.cover { page: cover; height: 11in; box-sizing: border-box; padding: 2.6in 1in 0;
  background: linear-gradient(160deg, #163f73 0%, #1f5fa8 60%, #2f7bc9 100%); color: #fff;
  break-after: page; }
.cover .eyebrow { font-size: 11pt; letter-spacing: 0.18em; text-transform: uppercase; opacity: 0.8; }
.cover h1 { font-size: 46pt; line-height: 1.05; margin: 0.15in 0 0.2in; font-weight: 700; }
.cover .sub { font-size: 15pt; opacity: 0.92; max-width: 5in; }
.cover .meta { margin-top: 1.2in; font-size: 10pt; opacity: 0.8; border-top: 1px solid rgba(255,255,255,0.4);
  padding-top: 0.15in; width: 5in; }

.toc { break-after: page; }
.toc h2 { font-size: 20pt; margin: 0 0 0.25in; color: var(--ink); }
.toc ol { list-style: none; padding: 0; margin: 0; }
.toc > ol > li { margin: 0 0 0.16in; }
.toc .part-link { display: flex; align-items: baseline; gap: 0.12in; font-size: 12.5pt; font-weight: 600; color: var(--ink); }
.toc .part-num { color: var(--accent); font-size: 9pt; letter-spacing: 0.12em; text-transform: uppercase; width: 0.7in; }
.toc ol ol { margin: 0.04in 0 0 0.82in; columns: 2; column-gap: 0.3in; }
.toc ol ol li { font-size: 9.5pt; line-height: 1.6; color: var(--quiet); break-inside: avoid; }
.toc ol ol a { color: var(--quiet); }
.toc .sec-num { color: var(--accent); display: inline-block; width: 0.32in; }

.part { break-before: page; margin: 0 0 0.3in; padding: 0.32in 0.3in 0.28in;
  background: var(--accent-soft); border-left: 6px solid var(--accent); border-radius: 0 6px 6px 0; }
.part .num { font-size: 9pt; font-weight: 700; letter-spacing: 0.16em; text-transform: uppercase; color: var(--accent); }
.part h2 { font-size: 24pt; line-height: 1.15; margin: 0.06in 0 0.08in; color: var(--ink); }
.part .blurb { margin: 0; color: var(--quiet); font-size: 11pt; }

h3 { display: flex; align-items: baseline; gap: 0.12in; font-size: 13.5pt; margin: 0.3in 0 0.1in;
  padding-bottom: 0.05in; border-bottom: 1.5px solid var(--rule); break-after: avoid; color: var(--ink); }
h3 .sec-num { color: #fff; background: var(--accent); font-size: 9pt; font-weight: 700; padding: 1px 6px;
  border-radius: 4px; position: relative; top: -1px; }
h3 + * { break-before: avoid; }

code { font: 8.6pt Menlo, Consolas, monospace; background: #f1f4f8; padding: 0 3px; border-radius: 3px; }
pre { background: #f6f8fb; border: 1px solid var(--rule); border-radius: 6px; padding: 9px 11px;
  font: 8.4pt/1.4 Menlo, Consolas, monospace; break-inside: avoid; overflow: hidden; }
pre.wide { font-size: 7.1pt; line-height: 1.17; }
pre code { background: none; padding: 0; font: inherit; }

table { border-collapse: collapse; width: 100%; margin: 0.08in 0 0.16in; font-size: 9pt; break-inside: auto; }
thead { display: table-header-group; }
tr { break-inside: avoid; }
th { background: #eef2f7; text-align: left; font-weight: 600; color: var(--ink); }
th, td { border: 1px solid var(--rule); padding: 4px 7px; vertical-align: top; }
tbody tr:nth-child(even) td { background: #fafbfd; }
td code, th code { font-size: 8pt; }
table.short { break-inside: avoid; }
"""


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def inline(text: str) -> str:
    """Heading text with `code` spans, escaped for HTML."""
    parts = re.split(r"(`[^`]+`)", text)
    return "".join(
        f"<code>{html.escape(p[1:-1])}</code>" if p.startswith("`") else html.escape(p) for p in parts
    )


def build_html(source: str) -> str:
    lines = source.splitlines()
    title = lines[0].removeprefix("# ").strip() if lines and lines[0].startswith("# ") else "k4cm Guide"
    body_lines: list[str] = []
    toc: list[tuple[str, str, list[tuple[str, str, str]]]] = []
    part = section = 0
    fenced = False
    for line in lines[1:]:
        if line.startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("## "):
            part += 1
            section = 0
            name = line[3:].strip()
            pid = slug(name)
            toc.append((pid, name, []))
            blurb = PART_BLURBS.get(name, "")
            body_lines += [
                "",
                (
                    f'<div class="part" id="{pid}"><div class="num">Part {part}</div>'
                    f'<h2>{inline(name)}</h2><p class="blurb">{html.escape(blurb)}</p></div>'
                ),
                "",
            ]
            continue
        if not fenced and line.startswith("### "):
            section += 1
            name = line[4:].strip()
            sid = f"{toc[-1][0]}-{slug(name)}"
            num = f"{part}.{section}"
            toc[-1][2].append((sid, num, name))
            body_lines += ["", f'<h3 id="{sid}"><span class="sec-num">{num}</span>{inline(name)}</h3>', ""]
            continue
        body_lines.append(line)

    body = markdown.markdown("\n".join(body_lines), extensions=["tables", "fenced_code"])

    def mark_wide(m: re.Match) -> str:
        text = html.unescape(re.sub(r"<[^>]+>", "", m.group(2)))
        wide = max((len(x) for x in text.splitlines()), default=0) > WIDE_CODE
        cls = ' class="wide"' if wide else ""
        return f"<pre{cls}>{m.group(1)}{m.group(2)}</pre>"

    body = re.sub(r"<pre>(<code[^>]*>)(.*?)</pre>", mark_wide, body, flags=re.DOTALL)

    # Keep a short table on one page; a long one may split, repeating its header.
    def mark_short(m: re.Match) -> str:
        rows = m.group(0).count("<tr>")
        return m.group(0).replace("<table>", '<table class="short">', 1) if rows <= SHORT_TABLE else m.group(0)

    body = re.sub(r"<table>.*?</table>", mark_short, body, flags=re.DOTALL)

    toc_html = ['<nav class="toc"><h2>Contents</h2><ol>']
    for i, (pid, name, sections) in enumerate(toc, 1):
        toc_html.append(
            f'<li><a class="part-link" href="#{pid}"><span class="part-num">Part {i}</span>{inline(name)}</a><ol>'
        )
        toc_html += [
            f'<li><a href="#{sid}"><span class="sec-num">{num}</span>{inline(n)}</a></li>' for sid, num, n in sections
        ]
        toc_html.append("</ol></li>")
    toc_html.append("</ol></nav>")

    cover = (
        '<section class="cover"><div class="eyebrow">K4 Connect Manager</div>'
        f"<h1>{html.escape(title)}</h1>"
        '<div class="sub">Sharing the mh01.net Elecraft K4s with members operating from home</div>'
        f'<div class="meta">Admin · User · Calendar · Internals<br>{datetime.now().astimezone():%d %B %Y}</div></section>'
    )
    return (
        f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>'
        f"<style>{CSS}</style></head><body>{cover}{''.join(toc_html)}<main>{body}</main></body></html>"
    )


def find_chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        found = shutil.which(candidate) or (candidate if Path(candidate).exists() else None)
        if found:
            return found
    sys.exit("Chrome or Chromium not found; install one, or pass --chrome PATH.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the guide PDF from its Markdown.")
    parser.add_argument("--source", type=Path, default=SOURCE)
    parser.add_argument("--output", type=Path, default=TARGET)
    parser.add_argument("--chrome", help="path to Chrome or Chromium")
    parser.add_argument("--html", type=Path, help="also save the intermediate HTML here")
    args = parser.parse_args()

    page = build_html(args.source.read_text(encoding="utf-8"))
    if args.html:
        args.html.write_text(page, encoding="utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "guide.html"
        src.write_text(page, encoding="utf-8")
        subprocess.run(
            [
                args.chrome or find_chrome(),
                "--headless",
                "--disable-gpu",
                "--no-pdf-header-footer",
                f"--print-to-pdf={args.output.resolve()}",
                src.as_uri(),
            ],
            check=True,
            capture_output=True,
        )
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
