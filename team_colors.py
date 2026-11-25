"""SC2 team (player) colours, and parsing of the --team-colors option. No dependencies, so both
sc2rip.py and the Blender side can use it."""

import re

# SC2's player colours (sRGB 0-255), in the game's player-colour order
TEAM_COLORS = [
    ('Red', (180, 20, 30)), ('Blue', (0, 66, 255)), ('Teal', (28, 167, 234)), ('Purple', (84, 0, 129)),
    ('Yellow', (235, 225, 41)), ('Orange', (254, 138, 14)), ('Green', (22, 128, 0)),
    ('Light Pink', (204, 166, 252)), ('Violet', (31, 1, 201)), ('Light Grey', (82, 84, 148)),
    ('Dark Green', (16, 98, 70)), ('Brown', (78, 42, 4)), ('Light Green', (150, 255, 145)),
    ('Dark Grey', (35, 35, 35)), ('Pink', (229, 91, 176)),
]
NO_TEAM = 'No Team Colour'


def _key(s):
    return re.sub(r'[\s_-]', '', s.lower()).replace('gray', 'grey').replace('color', 'colour')


def parse(spec):
    """'red,blue,#ff8800,none' or 'all' -> [(name, (r, g, b) 0-255, or None for "no team colour",
    i.e. the plain diffuse texture)], without duplicates. Raises ValueError for unknown names."""
    if isinstance(spec, (list, tuple)):
        spec = ','.join(spec)
    out = []
    for item in [s.strip() for s in (spec or '').split(',') if s.strip()]:
        key = _key(item)
        if key == 'all':
            out += TEAM_COLORS
        elif key in ('none', 'off', 'noteam', 'noteamcolour', 'plain'):
            out.append((NO_TEAM, None))
        elif re.fullmatch(r'#?[0-9a-f]{6}', key):
            h = key.lstrip('#')
            out.append(('#' + h.upper(), tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))))
        else:
            match = [(n, c) for n, c in TEAM_COLORS if _key(n) == key]
            if not match:
                raise ValueError(f'unknown team colour "{item}" (use any of: '
                                 f'{", ".join(n for n, _ in TEAM_COLORS)}, all, none or #rrggbb)')
            out.append(match[0])
    seen, uniq = set(), []
    for n, c in out:
        if n not in seen:
            seen.add(n)
            uniq.append((n, c))
    return uniq
