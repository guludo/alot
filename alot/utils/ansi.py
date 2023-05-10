# This file is released under the GNU GPL, version 3 or a later revision.
# For further details see the COPYING file

import logging
import re


_b1 = r'\033\['  # Control Sequence Introducer
_b2 = r'[0-9:;<=>?]*'  # parameter bytes
_b3 = r'[ !\"#$%&\'()*+,-./]*'  # intermediate bytes
_b4 = r'[A-Z[\]^_`a-z{|}~]'  # final byte"
csi_pattern = re.compile(
    _b1 + r'(?P<pb>' + _b2 + ')' + r'(?P<ib>' + _b3 + ')' + r'(?P<fb>' + _b4 + ')')


def parse_ansi_escapes(text):
    """
    TODO: Add doc
    """
    i = 0
    j = 0
    code, args = None, None
    while True:
        j = text.find('\033', j)
        if j == -1:
            break

        if j + 1 >= len(text):
            break

        new_code = text[j + 1]
        if new_code == '[':
            new_args, k = parse_csi(text, j)
        else:
            new_args = None

        if new_args is None:
            logging.warning(f'sequence for ESC {new_code} ignored: {text[j:j+10]!r}...')
            j = j + 2
        else:
            yield code, args, text[i:j]
            code, args = new_code, new_args
            i = j = k

    yield code, args, text[i:]


def strip_ansi_escapes(text):
    """Return text with ANSI escape sequences removed."""
    return "".join(s for *_, s in parse_ansi_escapes(text))


def parse_csi(text, pos):
    """
    TODO: Add doc
    """
    m = csi_pattern.match(text, pos)
    if not m:
        return None, -1
    pb, ib, fb = m.groups()
    return (pb, ib, fb), m.end()
