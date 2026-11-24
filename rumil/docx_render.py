"""Word support: turn [@citekey] markers in a .docx into formatted citations plus a
reference list, writing a new file (the original is never modified).

Marker syntax (pandoc's): [@key], [@a; @b], [see @key, p. 12], [-@key] (year
only), and @key in running text for a narrative citation ("Phillips et al.
(2023)"). Put [@bibliography] on its own line where the reference list should go;
otherwise it is added at the end under a "References" heading.
"""

from __future__ import annotations

import copy
import re
from pathlib import Path

from .formatting import Formatter, parse_cite_string

KEYCHARS = r"[\w:.#$%&\-+?<>~/]"
BRACKET_RE = re.compile(r"\[[^\[\]]*?-?@" + KEYCHARS + r"+[^\[\]]*\]")
BARE_RE = re.compile(r"(?<![\w@.\[/])@([A-Za-z]" + KEYCHARS + r"*[\w])")
BIB_PLACEHOLDERS = {"[@bibliography]", "{{bibliography}}", "[bibliography]", "#refs", "{bibliography}"}


def _iter_paragraphs(doc):
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    def walk(container):
        for child in container.iter_inner_content() if hasattr(container, "iter_inner_content") else container.paragraphs:
            if isinstance(child, Paragraph):
                yield child
            elif isinstance(child, Table):
                for row in child.rows:
                    for cell in row.cells:
                        yield from walk(cell)

    yield from walk(doc)
    for section in doc.sections:
        for hf in (section.header, section.footer):
            if hf is not None and not hf.is_linked_to_previous:
                yield from walk(hf)


def _find_markers(text: str, key_exists) -> list[tuple[int, int, str, bool]]:
    found = []
    taken = []
    for m in BRACKET_RE.finditer(text):
        found.append((m.start(), m.end(), m.group(0), False))
        taken.append((m.start(), m.end()))
    for m in BARE_RE.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        key = m.group(1).rstrip(".,;:")
        if key_exists(key):
            end = m.start() + 1 + len(key)
            found.append((m.start(), end, "@" + key, True))
    return sorted(found)


def _apply_format(run, fmt: dict) -> None:
    f = run.font
    if fmt.get("italic"):
        f.italic = not bool(f.italic) if f.italic else True
    if fmt.get("bold"):
        f.bold = True
    if fmt.get("superscript"):
        f.superscript = True
    if fmt.get("subscript"):
        f.subscript = True
    if fmt.get("smallcaps"):
        f.small_caps = True


def _replace_span(par, start: int, end: int, new_runs: list[dict], highlight: bool = False) -> bool:
    from docx.enum.text import WD_COLOR_INDEX
    from docx.text.run import Run

    runs = list(par.runs)
    pos, i_start, i_end, o_start, o_end = 0, None, None, 0, 0
    for i, r in enumerate(runs):
        t = r.text
        if i_start is None and start < pos + len(t):
            i_start, o_start = i, start - pos
        if i_start is not None and end <= pos + len(t):
            i_end, o_end = i, end - pos
            break
        pos += len(t)
    if i_start is None or i_end is None:
        return False
    first, last = runs[i_start], runs[i_end]
    before, after = first.text[:o_start], last.text[o_end:]
    template = copy.deepcopy(first._r)
    for r in runs[i_start + 1: i_end]:
        r._r.getparent().remove(r._r)
    if i_end != i_start:
        last.text = after
    first.text = before
    anchor = first._r
    for spec in new_runs:
        el = copy.deepcopy(template)
        anchor.addnext(el)
        run = Run(el, par)
        run.text = spec.get("text", "")
        _apply_format(run, spec)
        if highlight:
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW
        anchor = el
    if i_end == i_start and after:
        el = copy.deepcopy(template)
        anchor.addnext(el)
        Run(el, par).text = after
    if not before:
        first._r.getparent().remove(first._r)
    return True


def render_docx(formatter: Formatter, in_path: str | Path, *, style: str | None = None, out_path: str | Path | None = None,
                bibliography_title: str = "References") -> dict:
    from docx import Document
    from docx.shared import Inches

    src = Path(in_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(src)
    out = Path(out_path).expanduser() if out_path else src.with_name(f"{src.stem}-cited{src.suffix}")
    if out.resolve() == src.resolve():
        raise ValueError("Refusing to overwrite the input document; choose a different output path")
    doc = Document(str(src))
    lib = formatter.lib
    cache: dict[str, bool] = {}

    def key_exists(k):
        if k not in cache:
            cache[k] = lib.resolve_id(k) is not None
        return cache[k]

    work = []  # (paragraph, start, end, group)
    unparsed: list[str] = []
    placeholder = None
    for par in _iter_paragraphs(doc):
        run_text = "".join(r.text for r in par.runs)
        if run_text.strip().lower() in BIB_PLACEHOLDERS and placeholder is None:
            placeholder = par
            continue
        for s, e, raw, bare in _find_markers(run_text, key_exists):
            g = parse_cite_string(raw)
            if bare:
                g.narrative = True
            if g.refs:
                work.append((par, s, e, g))
            else:
                unparsed.append(raw)
    if not work:
        return {"input": str(src), "output": None, "citations": 0,
                "message": "No [@citekey] markers found in the document"}
    # Citations with any key missing from the library are left as written (highlighted) and kept out of the
    # numbering and reference list, so the document never cites something that isn't there.
    good = [w for w in work if all(key_exists(r.key) for r in w[3].refs)]
    bad = [w for w in work if w not in good]
    missing_keys = sorted({r.key for w in bad for r in w[3].refs if not key_exists(r.key)})
    rendered = formatter.render([w[3] for w in good], style, output="runs", bibliography=True)
    jobs = [(par, s, e, g, cit) for (par, s, e, g), cit in zip(good, rendered["citations"])]
    jobs += [(par, s, e, g, None) for (par, s, e, g) in bad]
    by_par: dict[int, list] = {}
    for j in jobs:
        by_par.setdefault(id(j[0]), []).append(j)
    replaced, failed, unresolved = 0, [], [w[3].raw for w in bad]
    # Replace from the end of each paragraph backwards so offsets stay valid.
    for lst in by_par.values():
        for par, s, e, g, cit in sorted(lst, key=lambda x: -x[1]):
            if cit is None:
                ok = _replace_span(par, s, e, [{"text": g.raw}], highlight=True)
            else:
                ok = _replace_span(par, s, e, cit.get("runs") or [{"text": cit["text"]}])
                replaced += 1 if ok else 0
            if not ok:
                failed.append(g.raw)
    entries = rendered["bibliography"]
    style_names = {s.name for s in doc.styles}
    numeric = rendered["style"]["format"] == "numeric"

    def fill(p, entry):
        for spec in entry.get("runs") or [{"text": entry["text"]}]:
            r = p.add_run(spec.get("text", ""))
            _apply_format(r, spec)
        if "Bibliography" in style_names:
            p.style = doc.styles["Bibliography"]
        pf = p.paragraph_format
        pf.left_indent = Inches(0.5 if not numeric else 0.35)
        pf.first_line_indent = Inches(-0.5 if not numeric else -0.35)

    if entries:
        if placeholder is not None:
            anchor = placeholder
            for entry in entries:
                new_p = copy.deepcopy(placeholder._p)
                for child in list(new_p):
                    if child.tag.endswith("}r") or child.tag.endswith("}hyperlink"):
                        new_p.remove(child)
                anchor._p.addnext(new_p)
                from docx.text.paragraph import Paragraph
                p = Paragraph(new_p, placeholder._parent)
                fill(p, entry)
                anchor = p
            placeholder._p.getparent().remove(placeholder._p)
        else:
            if bibliography_title:
                doc.add_heading(bibliography_title, level=1) if "Heading 1" in style_names else doc.add_paragraph(bibliography_title)
            for entry in entries:
                fill(doc.add_paragraph(), entry)
    doc.save(str(out))
    return {
        "input": str(src), "output": str(out), "style": rendered["style"], "citations": len(work),
        "replaced": replaced, "left_unresolved": unresolved, "unparsed_markers": unparsed,
        "failed_to_replace": failed, "references": len(entries), "missing_keys": missing_keys,
        "warnings": rendered["warnings"],
        "message": (f"Formatted {replaced} citations and {len(entries)} references in {rendered['style']['title']} style."
                    + (f" {len(unresolved)} citations contain keys not in the library ({', '.join(missing_keys)}); "
                       "they were left as written and highlighted in yellow." if unresolved else "")
                    + (f" {len(unparsed)} markers could not be parsed: {', '.join(unparsed[:5])}." if unparsed else "")),
    }
