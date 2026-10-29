"""Export docs/Performance_Audit_Report.md to Performance_Audit_Report.pdf (the file name the CP3 specification asks for).

Run it from the repository root. Nothing is installed on the host, a throw-away container does the work
(it installs the PDF libraries inside the container, so it needs internet access):

  docker run --rm -v "$PWD:/repo" python:3.11-slim python /repo/docs/perf/make_report_pdf.py

What it does
  * the Markdown (tables, code blocks, the four charts) is rendered to HTML (CommonMark, the same rules as GitHub) and printed to an A4 PDF with WeasyPrint;
  * the formulas written as LaTeX in the report ($p95 < 200\\text{ ms}$, $\\ge$, $\\approx$) are turned into plain Unicode
    (p95 < 200 ms, >=, ~=), because a PDF printer does not render LaTeX; GitHub renders it in the .md, the PDF shows the same numbers;
  * links to other files of the repository are printed as plain text (a link inside a PDF would point nowhere).

Run it again after every change of the report, so that the PDF and the Markdown always show the same figures.
"""
import re
import subprocess
import sys
from pathlib import Path

SOURCE = Path("/repo/docs/Performance_Audit_Report.md")
TARGET = Path("/repo/Performance_Audit_Report.pdf")

# --- the PDF libraries live only inside the throw-away container
subprocess.run(["apt-get", "update", "-qq"], check=True, stdout=subprocess.DEVNULL)
subprocess.run(
    ["apt-get", "install", "-y", "-qq", "--no-install-recommends",
     "libpango-1.0-0", "libpangoft2-1.0-0", "libharfbuzz0b", "fonts-dejavu-core", "shared-mime-info"],
    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "markdown-it-py", "weasyprint"],
               check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

from markdown_it import MarkdownIt  # noqa: E402
from weasyprint import HTML  # noqa: E402

SYMBOLS = {
    r"\geq": "\u2265", r"\ge": "\u2265", r"\leq": "\u2264", r"\le": "\u2264", r"\approx": "\u2248", r"\times": "\u00d7",
    r"\rightarrow": "\u2192", r"\to": "\u2192", r"\cdot": "\u00b7", r"\pm": "\u00b1", r"\sim": "~", r"\%": "%",
    r"\lt": "<", r"\gt": ">", r"\,": " ", r"\;": " ", r"\ ": " ",
}
unknown_commands = set()


def plain_math(expression: str) -> str:
    """LaTeX of the report ($...$) -> readable text: \\text{ ms} -> ms, \\ge -> the symbol, braces dropped."""
    text = re.sub(r"\\text\{([^}]*)\}", r"\1", expression)
    for command in sorted(SYMBOLS, key=len, reverse=True):
        text = text.replace(command, SYMBOLS[command])
    unknown_commands.update(re.findall(r"\\[A-Za-z]+", text))
    text = re.sub(r"[{}]", "", text)
    return re.sub(r"\s+", " ", text).strip().replace("<", "&lt;").replace(">", "&gt;")


def convert_math(markdown_text: str) -> str:
    """Convert $...$ outside fenced code blocks and inline code."""
    pieces = re.split(r"(```.*?```|`[^`\n]*`)", markdown_text, flags=re.S)
    for i in range(0, len(pieces), 2):                       # even pieces are plain text
        pieces[i] = re.sub(r"\$([^$\n]+)\$", lambda m: plain_math(m.group(1)), pieces[i])
    return "".join(pieces)


CSS = """
@page { size: A4; margin: 16mm 14mm 18mm 14mm;
        @bottom-center { content: "269340 Team Project - CP3 Performance Audit Report - page " counter(page) " of " counter(pages);
                         font-size: 7.5pt; color: #666; } }
body { font-family: "DejaVu Sans", sans-serif; font-size: 9.2pt; line-height: 1.42; color: #1b1b1b; }
h1 { font-size: 19pt; margin: 0 0 6pt; border-bottom: 2px solid #1f4e79; padding-bottom: 4pt; color: #1f4e79; }
h2 { font-size: 13.5pt; margin: 16pt 0 5pt; border-bottom: 1px solid #b8c4d0; padding-bottom: 2pt; color: #1f4e79; page-break-after: avoid; }
h3 { font-size: 10.8pt; margin: 11pt 0 4pt; color: #2d3b4a; page-break-after: avoid; }
p, li { orphans: 3; widows: 3; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0 8pt; font-size: 7.8pt; line-height: 1.3; }
th { background: #e8eef5; text-align: left; }
th, td { border: 0.6pt solid #b0b8c2; padding: 2.5pt 4pt; vertical-align: top; overflow-wrap: break-word; }
tr { page-break-inside: avoid; }
code { font-family: "DejaVu Sans Mono", monospace; font-size: 0.88em; background: #f1f3f5; padding: 0 1.5pt; overflow-wrap: break-word; }
td code { word-break: break-all; }
pre { font-size: 6.9pt; line-height: 1.28; background: #f5f6f8; border: 0.5pt solid #d5d9de; padding: 5pt 6pt;
      white-space: pre-wrap; overflow-wrap: anywhere; page-break-inside: auto; }
pre code { background: none; padding: 0; font-size: 1em; }
img { display: block; max-width: 100%; margin: 6pt auto 2pt; page-break-inside: avoid; }
hr { border: 0; border-top: 0.6pt solid #c9d0d8; margin: 10pt 0; }
em { color: #444; }
"""

source = SOURCE.read_text(encoding="utf-8")
body = MarkdownIt("commonmark").enable("table").render(convert_math(source))   # CommonMark, like GitHub: lists and tables render the same
body = re.sub(r'<a href="(?!https?:)[^"]*">(.*?)</a>', r"\1", body, flags=re.S)   # links to repository files -> plain text
html = f'<!doctype html><html><head><meta charset="utf-8"><title>Checkpoint 3: Performance Audit Report</title>' \
       f'<meta name="author" content="269340 Team Project"><style>{CSS}</style></head><body>{body}</body></html>'

HTML(string=html, base_url=str(SOURCE.parent)).write_pdf(str(TARGET))
print(f"written {TARGET.name}: {TARGET.stat().st_size // 1024} KB")
if unknown_commands:
    print("WARNING: LaTeX commands this script does not know (check the PDF): " + ", ".join(sorted(unknown_commands)))
