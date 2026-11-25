"""
Reading SC2 game data catalogs (the .xml files in */base.sc2data/GameData), with SC2's templates.

Entries often don't spell out their values but inherit them from a parent entry, with tokens:

    <CModel default="1" id="Unit">
        <?token id="race" value="Terran"?>
        <Model value="Assets\\Units\\##race##\\##id##\\##id##.m3"/>
    </CModel>
    <CModel id="Marine" parent="Unit"/>                      -> Assets\\Units\\Terran\\Marine\\Marine.m3
    <CModel id="Zealot" parent="Unit" race="Protoss"/>       -> Assets\\Units\\Protoss\\Zealot\\Zealot.m3

Token values come from the entry's own attributes (other than id / parent / default), then its
parents' attributes, then the <?token?> defaults declared on the way; ##id## is the entry's id.
An entry may also be defined again by another mod (same id, no parent): it then extends the
earlier definition rather than replacing it.
"""
import re
import xml.etree.ElementTree as ET

TOKEN_RE = re.compile(r'##([A-Za-z0-9_]+)##')
PI_ATTR_RE = re.compile(r'([A-Za-z_][\w.-]*)\s*=\s*"([^"]*)"')
RESERVED = ('id', 'parent', 'default')


class Entry:
    __slots__ = ('tag', 'id', 'container', 'parent', 'default', 'attrs', 'tokens', 'fields')

    def __init__(self, tag, eid, container):
        self.tag, self.id, self.container = tag, eid, container
        self.parent, self.default = None, False
        self.attrs = {}      # token values set on the entry (attributes other than id/parent/default)
        self.tokens = {}     # token defaults from <?token id=".." value=".."?>
        self.fields = []     # (field tag, value) of the entry's children, in document order


def parse_catalog(data, container, tags, keep_value=None):
    """Entries of the given element tags (a tuple of prefixes, e.g. ('CModel',)) from one game data
    .xml. keep_value(str) -> bool chooses which field values are kept (all by default)."""
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_pis=True))
    root = ET.fromstring(data, parser=parser)
    out = []
    for el in root:
        if not isinstance(el.tag, str) or not el.tag.startswith(tags):
            continue
        eid = el.get('id')
        if not eid:
            continue
        e = Entry(el.tag, eid, container)
        e.parent = el.get('parent') or None
        e.default = el.get('default') == '1'
        e.attrs = {k: v for k, v in el.attrib.items() if k not in RESERVED}
        for sub in el.iter():
            if sub is el:
                continue
            if sub.tag is ET.ProcessingInstruction:
                text = sub.text or ''
                if text.startswith('token'):
                    a = dict(PI_ATTR_RE.findall(text))
                    if a.get('id'):
                        e.tokens[a['id']] = a.get('value', '')
                continue
            if not isinstance(sub.tag, str):
                continue
            for k, v in sub.attrib.items():
                if (k == 'value' or '.' in v or '##' in v) and (keep_value is None or keep_value(v)):
                    e.fields.append((sub.tag.lower(), v))
            if sub.text and sub.text.strip() and (keep_value is None or keep_value(sub.text)):
                e.fields.append((sub.tag.lower(), sub.text.strip()))
        out.append(e)
    return out


def expand(value, tokens):
    """Replace ##token## by its value (tokens may contain tokens). Unknown tokens are left as is."""
    for _ in range(5):
        new = TOKEN_RE.sub(lambda m: tokens.get(m.group(1), m.group(0)), value)
        if new == value:
            break
        value = new
    return value


class Catalog:
    """Entries of one or more catalogs across mods. base_containers are the containers every
    other container builds on (the melee mods), in load order."""

    def __init__(self, entries, base_containers=()):
        self.by_id = {}                 # (tag, id) -> [Entry, ...] in the order they were added
        for e in entries:
            self.by_id.setdefault((e.tag, e.id), []).append(e)
        self.base = list(base_containers)
        self._rank = {c: i for i, c in enumerate(self.base)}
        self._cache = {}

    def _definitions(self, tag, eid, container):
        """Definitions of an entry visible from a container: the base mods' then its own."""
        defs = self.by_id.get((tag, eid), ())
        base = sorted((d for d in defs if d.container in self._rank), key=lambda d: self._rank[d.container])
        own = [d for d in defs if d.container == container and d.container not in self._rank]
        if not base and not own:
            own = list(defs)            # defined only by unrelated mods: take them all
        return base + own

    def resolve(self, tag, eid, container, _depth=0):
        """-> (tokens, fields, is_default) of an entry as seen from a container, with its parent
        chain applied. fields maps a field tag to its list of values (own values first)."""
        key = (tag, eid, container)
        if key in self._cache:
            return self._cache[key]
        defs = self._definitions(tag, eid, container)
        if not defs or _depth > 32:
            return {}, {}, False
        self._cache[key] = ({}, {}, False)          # guard against cycles
        parent, attrs, pis, own, default = None, {}, {}, {}, False
        for d in defs:
            if d.parent:
                parent = d.parent
            attrs.update(d.attrs)
            pis.update(d.tokens)
            default = default or d.default
        for d in reversed(defs):                    # later definitions override earlier ones
            for t, v in d.fields:
                if v not in own.get(t, ()):
                    own.setdefault(t, []).append(v)
        tokens, fields = {}, {}
        if parent and parent != eid:
            ptok, pfields, _ = self.resolve(tag, parent, container, _depth + 1)
            tokens.update(ptok)
            fields = {t: list(v) for t, v in pfields.items()}
        tokens.update(pis)
        tokens.update(attrs)
        for t, v in own.items():
            fields[t] = v + [x for x in fields.get(t, []) if x not in v]
        res = (tokens, fields, default)
        self._cache[key] = res
        return res

    def concrete(self):
        """(tag, id, container) of every definition that isn't a template (default="1")."""
        for (tag, eid), defs in self.by_id.items():
            for d in defs:
                if not d.default:
                    yield tag, eid, d.container

    def values(self, tag, eid, container, field=None):
        """Field values of an entry with its tokens expanded (##id## = the entry's id). Values that
        still contain unknown tokens are dropped."""
        tokens, fields, _ = self.resolve(tag, eid, container)
        tokens = dict(tokens, id=eid)
        out = []
        for t, vs in fields.items():
            if field is not None and t != field:
                continue
            for v in vs:
                x = expand(v, tokens)
                if '##' not in x:
                    out.append((t, x))
        return out
