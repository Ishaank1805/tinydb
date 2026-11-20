"""Treat issue and comment text as untrusted input.

Principle: an agent may only see what the maintainer could see when they
triggered the run. GitHub does not render HTML comments or invisible
Unicode, so those are removed before any agent reads the text. Everything
that is left is wrapped in ``<untrusted_*>`` tags and described as data in
every system prompt.
"""
from __future__ import annotations

import re

HTML_COMMENT = re.compile(r'<!--.*?(?:-->|\Z)', re.S)
# zero-width chars, bidi controls, word joiners, BOM, soft hyphen, Unicode "tag" characters
INVISIBLE = re.compile('[\u00ad\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\U000e0000-\U000e007f]')
SUSPICIOUS = [
    ('instruction override', re.compile(r'\b(ignore|disregard|forget)\b.{0,40}\b(previous|prior|above|all|any)\b.{0,20}\b(instructions?|rules?|prompts?)', re.I)),
    ('role injection', re.compile(r'\b(you are now|act as|system prompt|developer message)\b', re.I)),
    ('targets the agents', re.compile(r'\b(autose|reviewer|implementer|planner)\s+agents?\b', re.I)),
    ('approval request', re.compile(r'\bapprove\b.{0,40}\b(without|automatically|unconditionally|regardless)\b', re.I)),
    ('secret exfiltration', re.compile(r'\b(environment variables?|env vars?|api[_ ]?keys?|secrets?|tokens?)\b.{0,60}\b(print|show|post|send|add|include|dump)\b|\b(print|show|post|send|dump)\b.{0,60}\b(environment variables?|env vars?|api[_ ]?keys?|secrets?|tokens?)\b', re.I)),
    ('destructive request', re.compile(r'\b(delete|remove|wipe)\b.{0,30}\b(tests?|tests/ directory|\.github|workflows?)\b', re.I)),
]


def sanitize(text: str | None, max_chars: int = 12000) -> tuple[str, dict]:
    """Return ``(clean_text, report)``; the report says what was removed."""
    text = text or ''
    comments = HTML_COMMENT.findall(text)
    text = HTML_COMMENT.sub('', text)
    invisible = len(INVISIBLE.findall(text))
    text = INVISIBLE.sub('', text)
    text = text.replace('\r\n', '\n').strip()
    truncated = len(text) > max_chars
    if truncated:
        text = text[:max_chars] + '\n[... truncated by AutoSE ...]'
    report = {
        'html_comments_removed': len(comments),
        'hidden_chars_removed': sum(len(c) for c in comments),
        'invisible_chars_removed': invisible,
        'truncated': truncated,
        'suspicious_visible': scan(text),
        'suspicious_hidden': scan(' '.join(comments)),
    }
    return text, report


def scan(text: str) -> list[str]:
    return [name for name, pattern in SUSPICIOUS if pattern.search(text or '')]


def wrap(kind: str, text: str, **attrs: object) -> str:
    """Wrap untrusted text in tags it cannot close itself."""
    safe = re.sub(r'<(/?)untrusted', r'&lt;\1untrusted', text or '', flags=re.I)
    attr = ''.join(f' {k}="{str(v).replace(chr(34), "")}"' for k, v in attrs.items())
    return f'<untrusted_{kind}{attr}>\n{safe}\n</untrusted_{kind}>'


def describe(report: dict) -> str:
    """One line for status comments and artifacts."""
    parts = []
    if report.get('html_comments_removed'):
        parts.append(f"{report['html_comments_removed']} hidden HTML comment(s) "
                     f"({report.get('hidden_chars_removed', 0)} chars) removed")
    if report.get('invisible_chars_removed'):
        parts.append(f"{report['invisible_chars_removed']} invisible character(s) removed")
    if report.get('truncated'):
        parts.append('text truncated')
    if report.get('suspicious_hidden'):
        parts.append('hidden text looked like prompt injection: ' + ', '.join(report['suspicious_hidden']))
    if report.get('suspicious_visible'):
        parts.append('visible text contains instruction-like phrases: ' + ', '.join(report['suspicious_visible']))
    return '; '.join(parts) or 'nothing removed'
