#!/usr/bin/env python3
"""
Render analysis/report.md into docs/report.html, styled like the rest of the board.

Kept as a generator rather than a hand-written page so the published report can
never drift from the markdown it came from: edit report.md, re-run this.

    python analysis/render_report.py
"""
import re
from datetime import datetime, timezone
from pathlib import Path

import markdown

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SRC = HERE / "report.md"
OUT = ROOT / "docs" / "report.html"
ASSET_V = 9

PAGES = [("index.html", "Dashboard"), ("rainfall.html", "Rainfall"),
         ("creek.html", "Creek"), ("storm.html", "Storm"), ("report.html", "Report")]

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>52 Weatherboard &mdash; Winter 2025-26 analysis</title>
<link rel="stylesheet" href="assets/app.css?v={v}">
<style>
/* Report-specific: a reading column, not a dashboard grid. */
.doc{{max-width:820px;margin-inline:auto;padding:0 16px 64px}}
.doc h1{{font-size:1.9rem;line-height:1.2;margin:28px 0 6px}}
.doc .lede{{color:var(--mute);font-style:italic;margin:0 0 28px}}
.doc h2{{font-size:1.25rem;margin:40px 0 12px;padding-top:18px;
  border-top:2px solid var(--rule);color:var(--navy)}}
.doc h3{{font-size:1.02rem;margin:26px 0 8px;color:var(--ink)}}
.doc p,.doc li{{line-height:1.62}}
.doc ul,.doc ol{{padding-left:22px}}
.doc li{{margin:5px 0}}
.doc strong{{color:var(--ink);font-weight:650}}
.doc code{{background:var(--water-soft);color:var(--ink);padding:1px 5px;
  border-radius:4px;font-size:.9em}}
.doc pre{{background:var(--card);border:1px solid var(--rule);border-left:3px solid var(--gold);
  border-radius:8px;padding:12px 14px;overflow-x:auto;box-shadow:var(--shadow)}}
.doc pre code{{background:none;padding:0;font-size:.88rem;line-height:1.5}}
.doc hr{{border:0;border-top:1px solid var(--rule);margin:34px 0}}
.doc blockquote{{margin:18px 0;padding:10px 16px;border-left:3px solid var(--gold);
  background:var(--card);color:var(--mute)}}
/* tables scroll on their own rather than pushing the page sideways */
.tw{{overflow-x:auto;margin:16px 0;border:1px solid var(--rule);border-radius:10px;
  box-shadow:var(--shadow);background:var(--card)}}
.doc table{{border-collapse:collapse;width:100%;font-size:.9rem}}
.doc th{{background:var(--navy);color:var(--gold);text-align:left;padding:9px 12px;
  font-size:.75rem;letter-spacing:.04em;text-transform:uppercase;white-space:nowrap}}
.doc td{{padding:8px 12px;border-top:1px solid var(--rule);white-space:nowrap}}
.doc td:first-child{{white-space:normal}}
.doc tbody tr:nth-child(even){{background:rgba(127,127,127,.05)}}
/* Column alignment comes from the markdown itself (the ---: markers), which the
   tables extension emits as inline styles. Do not blanket-align here: it would
   right-align prose columns too. */
.toc{{background:var(--card);border:1px solid var(--rule);border-radius:12px;
  padding:14px 18px;box-shadow:var(--shadow);margin:0 0 30px}}
.toc h2{{border:0;margin:0 0 8px;padding:0;font-size:.75rem;letter-spacing:.05em;
  text-transform:uppercase;color:var(--gold-dark)}}
.toc ol{{margin:0;padding-left:20px;columns:2;column-gap:26px}}
.toc li{{margin:3px 0;break-inside:avoid}}
@media (max-width:620px){{.toc ol{{columns:1}}}}
.stamp{{color:var(--mute);font-size:.82rem;margin-top:40px;
  border-top:1px solid var(--rule);padding-top:12px}}
</style>
</head>
<body>
<header class="top">
  <div class="top-in">
    <span class="brand">52 Weatherboard</span>
    <nav class="tabs">{nav}</nav>
  </div>
</header>
<main class="doc">
"""

FOOT = """
<p class="stamp">Generated from <code>analysis/report.md</code> by
<code>analysis/render_report.py</code> on {stamp}. Edit the markdown and re-run
that script rather than editing this page &mdash; it is overwritten.</p>
</main>
</body>
</html>
"""


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def main():
    md = SRC.read_text(encoding="utf-8")

    html = markdown.markdown(
        md, extensions=["tables", "fenced_code", "sane_lists", "attr_list"])

    # anchor every h2 so the contents list can point at it
    heads = []

    def anchor(m):
        text = re.sub(r"<[^>]+>", "", m.group(1))
        s = slug(text)
        heads.append((s, text))
        return f'<h2 id="{s}">{m.group(1)}</h2>'

    html = re.sub(r"<h2>(.*?)</h2>", anchor, html, flags=re.S)

    # wrap tables so wide ones scroll inside their own box
    html = re.sub(r"<table>", '<div class="tw"><table>', html)
    html = re.sub(r"</table>", "</table></div>", html)

    # the italic date line under the title is the lede
    html = html.replace("<p><em>Analysis of", '<p class="lede"><em>Analysis of', 1)

    toc = "".join(f'<li><a href="#{s}">{t}</a></li>' for s, t in heads)
    contents = f'<nav class="toc"><h2>Contents</h2><ol>{toc}</ol></nav>'

    # drop the contents block in after the lede paragraph
    m = re.search(r'<p class="lede">.*?</p>', html, flags=re.S)
    if m:
        html = html[:m.end()] + contents + html[m.end():]
    else:
        html = contents + html

    nav = "".join(
        f'<a href="{href}"{" class=\"active\"" if href == "report.html" else ""}>{label}</a>'
        for href, label in PAGES)
    stamp = datetime.now(timezone.utc).strftime("%d %B %Y")

    OUT.write_text(HEAD.format(v=ASSET_V, nav=nav) + html + FOOT.format(stamp=stamp),
                   encoding="utf-8")
    print(f"wrote {OUT}  ({OUT.stat().st_size/1024:.1f} KB, {len(heads)} sections)")


if __name__ == "__main__":
    main()
