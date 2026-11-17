"""Code shown in the chat: a snippet with a copy button for new files, and a before/after view for edited files."""

import difflib
import html

import streamlit.components.v1 as components

MAX_ROWS = 2000
ROW_HEIGHT = 20
MIN_HEIGHT = 140
MAX_HEIGHT = 560

STYLE = """
body { margin: 0; background: transparent; font-family: Consolas, 'Courier New', monospace; font-size: 13px; }
.cv-card { border: 1px solid #334155; border-radius: 10px; overflow: hidden; background: #0f172a; color: #e2e8f0; }
.cv-head { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 8px 12px;
  background: #1e293b; border-bottom: 1px solid #334155; font-family: 'Segoe UI', sans-serif; font-size: 13px; color: #cbd5e1; }
.cv-title { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.cv-tag { margin-left: 8px; padding: 1px 8px; border-radius: 999px; background: #334155; font-size: 11px; color: #94a3b8; }
.cv-actions { display: flex; gap: 6px; flex-shrink: 0; }
.cv-btn { background: #0d9488; color: #f0fdfa; border: none; border-radius: 6px; padding: 4px 10px;
  font-size: 12px; cursor: pointer; }
.cv-btn:hover { background: #0f766e; }
.cv-scroll { overflow: auto; }
pre { margin: 0; padding: 12px 14px; line-height: 20px; color: #e2e8f0; }
table { border-collapse: collapse; width: 100%; min-width: 720px; }
td { padding: 0 10px; line-height: 20px; vertical-align: top; white-space: pre; }
td.num { width: 44px; text-align: right; color: #64748b; background: #111827; user-select: none; padding-right: 10px; }
td.code { width: 50%; }
td.add { background: rgba(34, 197, 94, 0.18); }
td.num.add { background: rgba(34, 197, 94, 0.32); color: #bbf7d0; }
td.del { background: rgba(244, 63, 94, 0.18); }
td.num.del { background: rgba(244, 63, 94, 0.32); color: #fecdd3; }
td.empty { background: #0b1120; }
.cv-note { padding: 8px 12px; font-family: 'Segoe UI', sans-serif; font-size: 12px; color: #94a3b8; border-top: 1px solid #334155; }
"""

COPY_SCRIPT = """
function cvCopy(id, btn) {
  var text = document.getElementById(id).value;
  var label = btn.dataset.label;
  var done = function () {
    btn.textContent = 'Copied';
    setTimeout(function () { btn.textContent = label; }, 1500);
  };
  var fallback = function () {
    var area = document.createElement('textarea');
    area.value = text;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    try { document.execCommand('copy'); done(); } catch (e) {}
    document.body.removeChild(area);
  };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(done, fallback);
  } else {
    fallback();
  }
}
"""


def side_by_side_rows(old, new):
    """Aligned rows of (old number, old text, old kind, new number, new text, new kind), matched by difflib."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    rows = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for offset in range(i2 - i1):
                rows.append((i1 + offset + 1, old_lines[i1 + offset], "same",
                             j1 + offset + 1, new_lines[j1 + offset], "same"))
            continue
        for k in range(max(i2 - i1, j2 - j1)):
            left = (i1 + k + 1, old_lines[i1 + k], "del") if i1 + k < i2 else (None, "", "empty")
            right = (j1 + k + 1, new_lines[j1 + k], "add") if j1 + k < j2 else (None, "", "empty")
            rows.append(left + right)
    return rows


def _cell(number, text, kind):
    if number is None:
        return '<td class="num"></td><td class="code empty"></td>'
    return (f'<td class="num {kind}">{number}</td>'
            f'<td class="code {kind}">{html.escape(text) or " "}</td>')


def _copy_target(element_id, text):
    # A leading newline right after <textarea> is dropped by the parser, so it keeps the text exact.
    return f'<textarea id="{element_id}" hidden>\n{html.escape(text)}</textarea>'


def _document(body):
    return (f"<!doctype html><html><head><meta charset='utf-8'><style>{STYLE}</style></head>"
            f"<body>{body}<script>{COPY_SCRIPT}</script></body></html>")


def _copy_button(element_id, label):
    return f'<button class="cv-btn" data-label="{label}" onclick="cvCopy(\'{element_id}\', this)">{label}</button>'


def render_snippet(code, title):
    """A single code block with the language name and a copy button, for new files."""
    body = (
        '<div class="cv-card">'
        f'<div class="cv-head"><span class="cv-title">{html.escape(title)}</span>'
        f'<div class="cv-actions">{_copy_button("snippet", "Copy")}</div></div>'
        f'<div class="cv-scroll"><pre><code>{html.escape(code)}</code></pre></div>'
        f'{_copy_target("snippet", code)}</div>'
    )
    lines = code.count("\n") + 1
    height = max(MIN_HEIGHT, min(MAX_HEIGHT, 40 + lines * ROW_HEIGHT + 24))
    components.html(_document(body), height=height, scrolling=True)


def render_side_by_side(path, status, old, new):
    """The file before and after a change, in two aligned columns with changed lines highlighted."""
    rows = side_by_side_rows(old, new)
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    body_rows = "".join(
        f"<tr>{_cell(ln, lt, lk)}{_cell(rn, rt, rk)}</tr>" for ln, lt, lk, rn, rt, rk in rows
    )
    note = '<div class="cv-note">Showing the first lines only. Use Copy to get the whole file.</div>' if truncated else ""
    body = (
        '<div class="cv-card">'
        f'<div class="cv-head"><span class="cv-title">{html.escape(path)}'
        f'<span class="cv-tag">{html.escape(status)}</span></span>'
        f'<div class="cv-actions">{_copy_button("before", "Copy before")}{_copy_button("after", "Copy after")}</div></div>'
        f'<div class="cv-scroll"><table>{body_rows}</table></div>{note}'
        f'{_copy_target("before", old)}{_copy_target("after", new)}</div>'
    )
    height = max(MIN_HEIGHT, min(MAX_HEIGHT, 48 + len(rows) * ROW_HEIGHT + 24))
    components.html(_document(body), height=height, scrolling=True)
