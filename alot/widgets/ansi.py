# Copyright (C) 2011-2017  Patrick Totzke <patricktotzke@gmail.com>
# This file is released under the GNU GPL, version 3 or a later revision.
# For further details see the COPYING file

import logging

import urwid

from ..utils import ansi


class ANSIText(urwid.WidgetWrap):

    """Selectable Text widget that interprets ANSI color codes"""

    def __init__(self, txt,
                 default_attr=None,
                 default_attr_focus=None,
                 ansi_background=True,
                 mimepart=False,
                 **kwds):
        self.mimepart = mimepart
        ct, focus_map = parse_escapes_to_urwid(txt, default_attr,
                                               default_attr_focus,
                                               ansi_background)
        t = urwid.Text(ct, **kwds)
        attr_map = {default_attr.background: ''}
        w = urwid.AttrMap(t, attr_map, focus_map)
        urwid.WidgetWrap.__init__(self, w)

    def selectable(self):
        return True

    def keypress(self, size, key):
        return key


ECODES = {
    '1': {'bold': True},
    '3': {'italics': True},
    '4': {'underline': True},
    '5': {'blink': True},
    '7': {'standout': True},
    '9': {'strikethrough': True},
    '30': {'fg': 'black'},
    '31': {'fg': 'dark red'},
    '32': {'fg': 'dark green'},
    '33': {'fg': 'brown'},
    '34': {'fg': 'dark blue'},
    '35': {'fg': 'dark magenta'},
    '36': {'fg': 'dark cyan'},
    '37': {'fg': 'light gray'},
    '40': {'bg': 'black'},
    '41': {'bg': 'dark red'},
    '42': {'bg': 'dark green'},
    '43': {'bg': 'brown'},
    '44': {'bg': 'dark blue'},
    '45': {'bg': 'dark magenta'},
    '46': {'bg': 'dark cyan'},
    '47': {'bg': 'light gray'},
}

URWID_MODS = [
    'bold',
    'underline',
    'standout',
    'blink',
    'italics',
    'strikethrough',
]


def parse_escapes_to_urwid(text, default_attr=None, default_attr_focus=None,
                           parse_background=True):
    """This function converts a text with ANSI escape for terminal
    attributes and returns a list containing each part of text and its
    corresponding Urwid Attributes object, it also returns a dictionary which
    maps all attributes applied here to focused attribute.

    This will only translate (a subset of) CSI sequences:
    we interpret only SGR parameters that urwid supports (excluding true color)
    See https://en.wikipedia.org/wiki/ANSI_escape_code#CSI_sequences
    """
    # these two will be returned
    urwid_text = []  # we will accumulate text (with attributes) here
    # mapping from included attributes to focused attr
    urwid_focus = {None: default_attr_focus}

    # Escapes are cumulative so we always keep previous values until it's
    # changed by another escape.
    attr = dict(fg=default_attr.foreground, bg=default_attr.background,
                bold=default_attr.bold, underline=default_attr.underline,
                standout=default_attr.underline)

    def append_themed_infix(infix):
        if not infix:
            # FIXME: Not sure why, but things are not rendered correctly if we
            # have empty infixes (it seems that the following one is not
            # rendered). Let's bail for now. We need to understand why this is
            # happening later.
            return
        urwid_fg = attr['fg']
        urwid_bg = default_attr.background
        for mod in URWID_MODS:
            if mod in attr and attr[mod]:
                urwid_fg += ',' + mod
        if parse_background:
            urwid_bg = attr['bg']
        urwid_attr = urwid.AttrSpec(urwid_fg, urwid_bg)
        urwid_focus[urwid_attr] = default_attr_focus
        urwid_text.append((urwid_attr, infix))

    def reset_attr():
        attr.clear()
        attr.update(fg=default_attr.foreground,
                    bg=default_attr.background, bold=default_attr.bold,
                    underline=default_attr.underline,
                    standout=default_attr.underline)

    def update_attr(pb, _, fb):
        if fb == 'm':
            # selector bit found. this means theming changes

            # Several attributes can be set in the same sequence,
            # separated by semicolons.
            param_iter = ("0" if v == "" else v for v in pb.split(";"))
            while True:
                try:
                    param = next(param_iter)
                except StopIteration:
                    # Done consuming parameters.
                    break

                if param == "" or param == "0":
                    reset_attr()
                elif param in ECODES:
                    attr.update(ECODES[param])
                elif param in ('38', '48', '58'):
                    # Foreground (38), background (48) or underline (58) colour.
                    # The underline one is currently not supported, but we at
                    # least parse it for completeness.
                    try:
                        color_type = next(param_iter)
                    except StopIteration:
                        logging.warning(f'{pb!r}: color param {param} requires arguments')
                        break

                    attr_name = ''
                    attr_value = ''

                    if color_type == '5':
                        # 8-bit index
                        try:
                            color_index = next(param_iter)
                        except StopIteration:
                            logging.warning(f'{pb!r}: missing 8-bit color index')
                            break
                        attr_value = 'h' + color_index
                    elif color_type == '2':
                        # RGB
                        try:
                            r, g, b = next(param_iter), next(param_iter), next(param_iter)
                        except StopIteration:
                            logging.warning(f'{pb!r}: missing RGB components')
                            break

                        try:
                            r, g, b = int(r), int(g), int(b)
                        except ValueError:
                            logging.warning(f'{pb!r}: expected integer values for RGB color')
                        else:
                            attr_value = f'#{r:02x}{g:02x}{b:02x}'
                    else:
                        logging.warning(f'{pb!r}: color type {color_type} ignored')

                    if param == '38':
                        attr_name = 'fg'
                    elif param == '48':
                        attr_name = 'bg'
                    else:
                        attr_name = ''
                        logging.warning(f'{pb!r}: coloring parameter {param} ignored')

                    if attr_name and attr_value:
                        attr.update({attr_name: attr_value})
                else:
                    logging.warning(f'{pb!r}: parameter {param} ignored')

    for code, args, infix in ansi.parse_ansi_escapes(text):
        if code == '[':
            pb, ib, fb = args
            update_attr(pb, ib, fb)
        append_themed_infix(infix)

    return urwid_text, urwid_focus
