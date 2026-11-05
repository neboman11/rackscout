"""Hardware claim extraction and conservative evidence resolution.

Source text (index titles, seller descriptions, eBay page variations) is
untrusted. Extraction returns explicit claims only; resolution combines them
into one configuration per verified variant (or one base configuration) with
per-field confidence. Unknown values stay ``None``; they never become zero.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cache, partial
from pathlib import Path
from typing import Any

CERTAIN = ("confirmed", "inferred")
MAX_EVIDENCE_TEXT = 300
DATA_DIR = Path(__file__).resolve().parent / "data"

# ---------------------------------------------------------------------------
# Vendor and model identity
# ---------------------------------------------------------------------------

SUPPORTED_VENDORS = ("Dell", "HPE", "Supermicro", "Lenovo", "Cisco")
_VENDOR_ALIASES = {
    "dell": "Dell", "dell emc": "Dell", "dell technologies": "Dell",
    "hpe": "HPE", "hp": "HPE", "hewlett packard enterprise": "HPE", "hewlett-packard enterprise": "HPE",
    "hewlett packard": "HPE", "hewlett-packard": "HPE",
    "supermicro": "Supermicro", "super micro": "Supermicro", "super micro computer": "Supermicro",
    "lenovo": "Lenovo",
    "cisco": "Cisco", "cisco systems": "Cisco",
}
_VENDOR_PREFIX_RE = re.compile(
    r"^\s*(?:dell(?:\s+emc)?|hewlett[\s-]packard(?:\s+enterprise)?|hpe?|super\s*micro|lenovo|cisco)\b\s*", re.I
)
_HPE_GEN_RE = re.compile(r"\bgen\s*(\d+)\b", re.I)
_HPE_PLUS_RE = re.compile(r"\bgen\s*10\s*(?:plus\b|\+)", re.I)
_LENOVO_BASE_RE = re.compile(r"^S[RT]\d{3}$")
# Supermicro system IDs: explicit SYS-/AS-/SSG- SKUs, or the bare SuperServer/A+ name (e.g. 6019U-TRTP2).
# Chassis (CSE-...) and motherboard (X11DPU, H11DSU-iN) names are deliberately not system IDs.
_SMC_PREFIXED_RE = re.compile(r"\b(SYS|AS|SSG)\s?-\s?(\d{4}[A-Z0-9]*-[A-Z0-9]+(?:-[A-Z0-9]+)*\+?)", re.I)
_SMC_BARE_RE = re.compile(r"(?<![\w-])(\d{4}[A-Z]{1,3}-[A-Z0-9]+(?:-[A-Z0-9]+)*\+?)", re.I)


def normalize_vendor(brand: str | None) -> str | None:
    """Canonical supported vendor for a source brand (HP is HPE); unknown brands stay None."""
    return _VENDOR_ALIASES.get(" ".join(str(brand or "").lower().split()))


@cache
def _supermicro_ids() -> dict[str, str]:
    """Exact Supermicro spellings (upper-case, without vendor prefix) mapped to cataloged model keys."""
    out: dict[str, str] = {}
    for m in json.loads((DATA_DIR / "models.json").read_text(encoding="utf-8"))["models"]:
        if m["vendor"] != "Supermicro":
            continue
        sku = m["model"].removeprefix("Supermicro ")
        out[sku.upper()] = m["model"]
        out[sku.split("-", 1)[1].upper()] = m["model"]  # bare SuperServer / A+ name of the same SKU
        for alias in m.get("aliases", []):
            out[alias.upper()] = m["model"]
    return out


def _supermicro_model(text: str) -> str | None:
    known = _supermicro_ids()
    found = {f"{p.upper()}-{sku.upper()}" for p, sku in _SMC_PREFIXED_RE.findall(text)}
    unprefixed = _SMC_PREFIXED_RE.sub(" ", text)
    found.update(sku.upper() for sku in _SMC_BARE_RE.findall(unprefixed))
    keys = {known.get(sku, f"Supermicro {sku}") for sku in found}
    return keys.pop() if len(keys) == 1 else None


def normalize_model(vendor: str | None, model: str | None, title: str) -> str | None:
    """Vendor-qualified model key; Dell keeps its bare PowerEdge model (R640) for continuity.

    Unknown vendors and system-less listings (chassis/motherboard only, or
    several different system IDs) yield None instead of a guessed model.
    """
    if vendor is None:
        return None
    if vendor == "Supermicro":
        return _supermicro_model(f"{model or ''} {title}")
    rest = " ".join(_VENDOR_PREFIX_RE.sub("", model or "").split())
    if not rest:
        return None
    if vendor == "Dell":
        return rest.upper().replace("XD", "xd")
    if vendor == "HPE":
        rest = _HPE_GEN_RE.sub(lambda m: f"Gen{m.group(1)}", rest)
        if rest.endswith("Gen10") and _HPE_PLUS_RE.search(title):
            rest += " Plus"
    elif vendor == "Lenovo" and _LENOVO_BASE_RE.match(rest.upper()):
        rest = rest.upper()
        if version := re.search(rf"\b{rest}\s*V([2-9])\b", title, re.I):
            rest = f"{rest} V{version.group(1)}"
    return f"{vendor} {rest}"

# ---------------------------------------------------------------------------
# Text patterns
# ---------------------------------------------------------------------------
_I = re.IGNORECASE

OPTION_RE = re.compile(
    r"\b(?:options?|optional|upgrades?|upgradable|upgradeable|configurations?\s+available|configurable|"
    r"configure|choose|choices?|select\s+(?:from|your)|add[- ]?ons?|upon\s+request|variations?|"
    r"can\s+be\s+(?:added|upgraded|configured)|we\s+also\s+(?:have|offer|sell)|additional\s+cost|"
    r"for\s+an?\s+(?:extra|additional)|available\s+(?:in|with|for|upon))\b",
    _I,
)
CAPACITY_RE = re.compile(r"\b(?:up\s+to|max(?:imum)?|supports?|capacity|expandable|slots?|capable)\b", _I)
EXCLUDED_HEAD_RE = re.compile(
    r"^\s*(?:items?\s+)?(?:not\s+included|excluded|exclusions|does\s+not\s+(?:come\s+with|include)|"
    r"what(?:(?:'|’)?s|\s+is)\s+not\s+included|will\s+not\s+(?:come\s+with|include)|not\s+come\s+with|"
    r"missing(?:\s+items?)?)\s*(?::|-|–)?\s*(?P<rest>.*)$",
    _I,
)
HEADING_RE = re.compile(r"^[^:]{1,60}:\s*$")
SECTION_HEAD_RE = re.compile(r"^\s*(?:specifications?|features?|configuration|details|included\s+items)\s*:?\s*$", _I)
INCLUSION_RE = re.compile(r"\b(?:include[sd]?|installed|has|have|contains?|features?|ships?\s+with|comes?\s+with)\b", _I)
NARRATIVE_RE = re.compile(
    r"^\s*(?:(?:this|the|our|your)\s+(?:server|system|unit|machine|configuration)\b|(?:we|it)\b)", _I
)

RAM_WORD = r"(?:ram|memory|dimms?|rdimms?|lrdimms?)"
NEG_RAM_RE = re.compile(
    rf"\b(?:no|without|zero|missing)\s+{RAM_WORD}\b"
    rf"|\b{RAM_WORD}\s*(?:[:\-–]\s*)?(?:none|not\s+included|n/?a|0\s*gb|removed)\b"
    rf"|\b{RAM_WORD}\s*[:\-–]\s*no\b"
    rf"|\b{RAM_WORD}\b[^.;]{{0,25}}\bnot\s+included\b"
    rf"|\b0\s*gb\s*(?:of\s+)?{RAM_WORD}\b"
    r"|\bno\s+ram\s*[/&,]",
    _I,
)
RAM_CONTEXT_RE = re.compile(r"\b(?:ram|memory|ddr[345]|dimms?|rdimms?|lrdimms?|ecc)\b", _I)
STORAGE_AFTER_RE = re.compile(r"^\s*(?:\)|\()?\s*(?:ssds?|hdds?|sas|sata|nvme|drives?|hard|cache|flash|boss|m\.2|sd\b|emmc|usb|storage|nv)", _I)
RAM_PRODUCT_RE = re.compile(r"\b(\d{1,2})\s*[x×]\s*(\d{1,4})\s*gb\b", _I)
RAM_TOTAL_RE = re.compile(r"\b(\d{1,4})\s*gb\b", _I)
PLAUSIBLE_RAM = {4, 8, 12, 16, 24, 32, 40, 48, 64, 80, 96, 112, 128, 160, 192, 224, 256, 288, 320,
                 384, 448, 512, 576, 640, 768, 1024, 1536, 2048, 3072}

STORAGE_WORD = r"(?:hdds?|hard\s*drives?|hard\s*disks?|drives?|disks?|ssds?|storage|hdd/ssd|ssd/hdd|media)"
DISKLESS_RE = re.compile(
    rf"\bno\s+{STORAGE_WORD}\b"
    rf"|\b{STORAGE_WORD}\s*(?:[:\-–]\s*)?(?:none|not\s+included|n/?a)\b"
    rf"|\bwithout\s+(?:any\s+)?{STORAGE_WORD}\b"
    rf"|\b{STORAGE_WORD}\b[^.;]{{0,25}}\bnot\s+included\b"
    r"|\bdiskless\b",
    _I,
)
INCLUDED_STORAGE_RE = re.compile(
    r"\b\d+\s*[x×]\s*\d+(?:\.\d+)?\s*(?:gb|tb)\b[^.;]{0,12}\b(?:sas|sata|ssds?|hdds?|nvme|hard\s*drives?|drives?)\b"
    r"|\b\d+(?:\.\d+)?\s*(?:gb|tb)\s*(?:sas|sata|ssds?|hdds?|nvme|m\.2|hard\s*drives?)\b"
    r"|\b(?:drives?|hdds?|ssds?|hard\s*drives?)\s+(?:are\s+)?included\b"
    r"|\bincludes?\s+[^.;]{0,30}\b(?:ssds?|hdds?|hard\s*drives?)\b",
    _I,
)
# Drive hardware parts that contain the word "drive" but say nothing about
# installed drives ("no drive trays" is not "no drives").
DRIVE_PART_RE = re.compile(
    r"\bdrives?\s+(?:back\s*-?\s*planes?|bays?|trays?|caddies|caddy|caddys|sleds?|carriers?|cages?|controllers?)\b", _I
)
STORAGE_WORD_RE = re.compile(rf"\b{STORAGE_WORD}\b", _I)
NEGATION_BEFORE_RE = re.compile(r"\b(?:no|not|without|zero|excluding)\s+(?:any\s+)?$", _I)
# Clause boundaries for deciding whether option wording governs a claim.
# Colons and commas do not end a clause ("Memory options: No RAM, 32GB").
CLAUSE_BREAK_RE = re.compile(r"[;!|()]|\.(?!\d)")

ACCESSORIES = (
    ("trays", re.compile(r"\b(?:drive\s+)?(?:trays?|caddies|caddys?|caddy|sleds?)\b", _I), "Drive trays/caddies not included"),
    ("rails", re.compile(r"\b(?:rails?|rail\s+kits?|slide\s+rails?)\b", _I), "Rack rails not included"),
    ("bezel", re.compile(r"\bbezels?\b", _I), "Front bezel not included"),
    ("power_cords", re.compile(r"\b(?:power\s+cords?|power\s+cables?|ac\s+cords?)\b", _I), "Power cords not included"),
    ("cma", re.compile(r"\bcable\s+management\s+arms?\b", _I), "Cable management arm not included"),
)

BACKPLANE_RE = re.compile(r"\bback\s*-?\s*planes?\b", _I)
CONTROLLER_RE = re.compile(
    r"\b(?:perc|raid\s+(?:controllers?|cards?)|(?:storage|disk|drive|sas|sata)\s+controllers?|hba(?:\s*-?\s*\d{3})?|"
    r"h[3-8][3-5][05]p?)\b",
    _I,
)

ESSENTIALS = {
    "psu": (
        re.compile(r"\b(?:psus?|power\s+suppl(?:y|ies))\b", _I),
        re.compile(
            r"\b\d\s*[x×]\s*\d{3,4}\s*w\b|\b\d{3,4}\s*w(?:att)?\b[^.;]{0,30}\b(?:psus?|power\s+suppl)"
            r"|\b(?:psus?|power\s+suppl\w*)\b[^.;]{0,20}\b\d{3,4}\s*w\b|\b(?:dual|redundant|2x?)\s+(?:psus?|power\s+suppl)",
            _I,
        ),
    ),
    "cooling": (
        re.compile(r"\b(?:heat\s*sinks?|fans?|cooling)\b", _I),
        re.compile(
            r"\bheat\s*sinks?\b|\b\d+\s*[x×]?\s*(?:cooling\s+)?fans?\b|\bfans?\s+(?:are\s+)?(?:included|installed)\b"
            r"|\bcooling\s+fans?\b|\bfan\s+modules?\b",
            _I,
        ),
    ),
    "nic": (
        re.compile(r"\b(?:nics?|network\s+(?:card|adapter|daughter\s*card)s?|rndc|ndc)\b", _I),
        re.compile(
            r"\b(?:nics?|network\s+daughter|rndc|ndc|ethernet|sfp\+?|sfp28|connectx|cx-?\d{3}\w*|x520|x540|x550|x710|"
            r"i350|bcm57\d+|qlogic|(?:\d\s*[x×]\s*)?(?:1|10|25|40)\s*(?:gbe|gbps|gbit|gb\s+(?:nic|ethernet|sfp\+?|network)))\b",
            _I,
        ),
    ),
    # Any non-negated mention of a backplane or storage controller is present
    # evidence. Onboard SATA may drive the bays without an add-in controller,
    # so a missing controller is a review condition, not a missing essential.
    "backplane": (BACKPLANE_RE, BACKPLANE_RE),
    "controller": (CONTROLLER_RE, CONTROLLER_RE),
}
ESSENTIAL_NEGATIVE = {
    key: re.compile(
        rf"\b(?:no|without)\s+(?:(?:any|additional|installed|supplied|included|working)\s+){{0,2}}{mention.pattern}"
        rf"|{mention.pattern}\s*(?:[:\-–]\s*)?(?:none|not\s+included|n/?a|missing|removed)\b"
        rf"|{mention.pattern}[^.;]{{0,25}}\bnot\s+included\b",
        _I,
    )
    for key, (mention, _present) in ESSENTIALS.items()
}
# Diagnostic status statements do not assert absent (or included) components.
STATUS_NOUN = r"(?:errors?|issues?|problems?|faults?|warnings?|alerts?|noise|codes?)"
STATUS_COMPONENT = (
    rf"(?:{RAM_WORD}|{STORAGE_WORD}|cpus?|processors?|"
    + "|".join(mention.pattern for mention, _ in ESSENTIALS.values()) + ")"
)
STATUS_CLAIM_RE = re.compile(
    rf"\b(?:no|without|zero)\s+(?:any\s+)?{STATUS_COMPONENT}\s+{STATUS_NOUN}\b"
    rf"|{STATUS_COMPONENT}\s*[:\-–]\s*(?:no|without|zero)\s+(?:any\s+)?{STATUS_NOUN}\b",
    _I,
)

# Split component lists before matching whole names. This avoids exponential
# token ambiguity and short aliases swallowing multiword names.
COMPONENT_TOKEN_RE = re.compile(
    rf"(?:{DRIVE_PART_RE.pattern}|{STATUS_COMPONENT}|"
    + "|".join(pattern.pattern for _, pattern, _ in ACCESSORIES) + ")", _I
)
COMPONENT_SEPARATOR_RE = re.compile(r"[,/&]|\b(?:and|or)\b", _I)
UNKNOWN_COMPONENT_RE = re.compile(r"[A-Z][A-Z0-9]{1,7}")
DIAGNOSTIC_COMPONENT_RE = re.compile(rf"(?P<component>.+?)(?:[\s-]+related)?\s+{STATUS_NOUN}", _I)
NEGATED_COMPONENT_LIST_RE = re.compile(
    r"\b(?:no|without|missing|zero)\s+(?P<parts>[^;.!|()]+)", _I
)

CPU_PATTERNS = (
    re.compile(
        r"(?:(?P<n>\d)\s*[x×]\s*|\((?P<p>\d)\)\s*|\b(?P<w>dual|single|two|one)\s+)?(?:intel\s+)?(?:xeon\s*)?(?:®\s*)?"
        r"\b(?P<tier>platinum|gold|silver|bronze)\s*(?P<num>\d{4}[a-z]{0,2})\b",
        _I,
    ),
    re.compile(
        r"(?:(?P<n>\d)\s*[x×]\s*|\((?P<p>\d)\)\s*|\b(?P<w>dual|single|two|one)\s+)?(?:amd\s+)?\bepyc\s*(?:™\s*)?(?P<num>\d{4}[a-z]?)\b",
        _I,
    ),
    re.compile(
        r"(?:(?P<n>\d)\s*[x×]\s*|\((?P<p>\d)\)\s*|\b(?P<w>dual|single|two|one)\s+)?(?:intel\s+)?(?:xeon\s*)?"
        r"\b(?P<e5>e[357]-\d{4}[a-z]?)\s*(?P<v>v\d)?\b",
        _I,
    ),
    re.compile(r"(?:(?P<n>\d)\s*[x×]\s*)?(?:intel\s+)?(?:xeon\s*)?\b(?P<e2>e-2\d{3}[a-z]{0,2})\b", _I),
)
WORD_COUNTS = {"dual": 2, "two": 2, "single": 1, "one": 1}
NEG_CPU_RE = re.compile(r"\bno\s+(?:cpus?|processors?)\b|\b(?:cpus?|processors?)\s*(?:[:\-–]\s*)?(?:none|not\s+included)\b|\bwithout\s+(?:cpus?|processors?)\b", _I)

BAY_PATTERNS = (
    re.compile(r"\b(?P<n>\d{1,2})\s*[x×]?\s*(?P<size>2\.5|3\.5)\s*(?:\"|″|”|''|-?\s*in(?:ch)?)?\s*(?:hot[- ]?swap(?:pable)?\s+)?(?:drive\s+)?(?:bays?|sff|lff|drive)", _I),
    re.compile(r"\b(?P<n>\d{1,2})\s*-?\s*(?P<size>sff|lff)\b", _I),
    re.compile(r"\b(?P<n>\d{1,2})\s*[x×]?\s*-?\s*bays?\b(?:\s*(?P<size>sff|lff|2\.5|3\.5))?", _I),
)
SIZE_ONLY_RE = re.compile(r"\b(?P<size>sff|lff)\b|(?P<inch>2\.5|3\.5)\s*(?:\"|″|”|-?\s*inch)", _I)

DAMAGE_RE = re.compile(
    r"\b(?:damaged?|cracked|broken|bent|dent(?:ed|s)?|for\s+parts|parts\s+only|not\s+working|faulty|defective|"
    r"(?:doesn(?:'|’)?t|does\s+not|won(?:'|’)?t|will\s+not)\s+(?:power|boot|post|turn\s+on))\b",
    _I,
)
NEG_DAMAGE_RE = re.compile(
    r"\b(?:no|not|without|zero|never|free\s+of)\b"
    r"(?:(?:\s+(?:have|has|had|any|known|visible|physical|cosmetic|significant|major|minor|structural|"
    r"noticeable|evidence|signs?|of|or|and|other|scratches?|scratched|scuffs?|scuffed|wear|blemishes?|"
    r"dents?|dented|cracks?|cracked|damages?|damaged|bent|broken))|[\s,/&\-])*$",
    _I,
)
COSMETIC_RE = re.compile(r"\b(?:scratch(?:es|ed)?|scuffs?|scuffed|cosmetic|wear|blemish(?:es)?)\b", _I)
TESTED_RE = re.compile(r"\b(?:tested|fully\s+functional|working|passed|diagnostics|epsa)\b", _I)
BAREBONES_RE = re.compile(r"\bbare\s*-?\s*bones?\b", _I)
MODEL_RE = re.compile(r"\b(R\d{3,4}(?:xd|xa|xs)?)\b", _I)


def clean_text(text: str, limit: int = MAX_EVIDENCE_TEXT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def normalize_cpu_key(cpu_model: str | None) -> str | None:
    if not cpu_model:
        return None
    return " ".join(cpu_model.lower().replace("-", " ").split())


def parse_cpu(text: str) -> list[tuple[str, int | None]]:
    """Return explicit CPU mentions as (normalized model, explicit count or None).

    One entry per model and explicit count: a count fills an earlier countless
    mention, while different explicit counts for the same model are all kept
    so callers see the conflict instead of the first count winning.
    """
    found: list[tuple[int, str, int | None]] = []
    for pattern in CPU_PATTERNS:
        for m in pattern.finditer(text):
            groups = m.groupdict()
            if groups.get("tier"):
                model = f"{groups['tier'].title()} {groups['num'].upper()}"
            elif groups.get("e5"):
                model = groups["e5"].upper() + (f" {groups['v'].lower()}" if groups.get("v") else "")
            elif groups.get("e2"):
                model = groups["e2"].upper()
            else:
                model = f"EPYC {groups['num'].upper()}"
            count = None
            if groups.get("n"):
                count = int(groups["n"])
            elif groups.get("p"):
                count = int(groups["p"])
            elif groups.get("w"):
                count = WORD_COUNTS[groups["w"].lower()]
            found.append((m.start(), model, count))
    found.sort()
    out: list[tuple[str, int | None]] = []
    for _, model, count in found:
        same = [i for i, (existing, _) in enumerate(out) if existing == model]
        if not same:
            out.append((model, count))
        elif count is None or any(out[i][1] == count for i in same):
            continue
        elif (countless := next((i for i in same if out[i][1] is None), None)) is not None:
            out[countless] = (model, count)
        else:
            out.append((model, count))
    return out


def parse_index_cpu(raw: str | None) -> tuple[str | None, int | None]:
    """Index CPU field such as ``2× Gold 6150`` or ``EPYC 7401P``."""
    if not raw:
        return None, None
    m = re.match(r"^\s*(\d+)\s*[×x]\s*(.+?)\s*$", raw)
    if m:
        return m.group(2), int(m.group(1))
    return raw.strip(), None


def _ram_values(segment: str) -> list[int]:
    values: list[int] = []
    consumed: list[tuple[int, int]] = []
    products: list[int] = []
    for m in RAM_PRODUCT_RE.finditer(segment):
        if STORAGE_AFTER_RE.match(segment[m.end():]):
            continue
        products.append(int(m.group(1)) * int(m.group(2)))
        consumed.append(m.span())
    for m in RAM_TOTAL_RE.finditer(segment):
        if any(a <= m.start() < b for a, b in consumed):
            continue
        if STORAGE_AFTER_RE.match(segment[m.end():]):
            continue
        value = int(m.group(1))
        if value in PLAUSIBLE_RAM:
            values.append(value)
    if values:
        return values[:1]
    return [sum(products)] if products else []


def _bays(segment: str) -> list[tuple[int, str | None]]:
    out = []
    for pattern in BAY_PATTERNS:
        for m in pattern.finditer(segment):
            n = int(m.group("n"))
            if not 1 <= n <= 32:
                continue
            size = (m.groupdict().get("size") or "").lower()
            size_norm = "SFF" if size in {"sff", "2.5"} else "LFF" if size in {"lff", "3.5"} else None
            out.append((n, size_norm))
        if out:
            break
    return out


def _sizes(segment: str) -> list[str]:
    sizes = []
    for m in SIZE_ONLY_RE.finditer(segment):
        token = (m.group("size") or m.group("inch") or "").lower()
        sizes.append("SFF" if token in {"sff", "2.5"} else "LFF")
    return sizes


def _damage(segment: str) -> bool:
    for m in DAMAGE_RE.finditer(segment):
        prefix = re.split(r"[.;!?]|\b(?:but|however|although|except|yet)\b", segment[:m.start()], flags=_I)[-1]
        if not NEG_DAMAGE_RE.search(prefix):
            return True
    return False


def _expand_component_exclusions(match: re.Match[str]) -> str:
    """Normalize complete, plain negative lists; retain ambiguous claims verbatim."""
    parts = [part.strip() for part in COMPONENT_SEPARATOR_RE.split(match.group("parts")) if part.strip()]
    if len(parts) < 2:
        return match.group()
    def plain(part: str) -> bool:
        return bool(COMPONENT_TOKEN_RE.fullmatch(part) or UNKNOWN_COMPONENT_RE.fullmatch(part))

    if not all(plain(part) for part in parts[:-1]):
        return match.group()
    diagnostic = DIAGNOSTIC_COMPONENT_RE.fullmatch(parts[-1])
    if diagnostic and COMPONENT_TOKEN_RE.fullmatch(diagnostic["component"]):
        return " "
    if not plain(parts[-1]):
        return match.group()
    return "no " + ", no ".join(parts)


def _optional_at(segment: str, pos: int) -> bool:
    """True when option wording precedes ``pos`` within its clause (an offered choice)."""
    start = 0
    for m in CLAUSE_BREAK_RE.finditer(segment, 0, pos):
        start = m.end()
    return bool(OPTION_RE.search(segment, start, pos))


def _definite(pattern: re.Pattern[str], segment: str, *, option_heading: bool, option_inline: bool) -> bool:
    """A negative claim that is stated, not offered as one option among others.

    Items listed under an options heading are choices. Inline option wording
    only governs claims that follow it in the same clause, so "No RAM
    installed; upgrade options available" still states zero installed RAM.
    """
    if option_heading:
        return False
    return any(not option_inline or not _optional_at(segment, m.start()) for m in pattern.finditer(segment))


def _included_storage(segment: str) -> bool:
    """Positive included-drive wording that is not itself negated ("No drives included")."""
    negated = [m.span() for m in DISKLESS_RE.finditer(segment)]
    for m in INCLUDED_STORAGE_RE.finditer(segment):
        if any(a < m.end() and m.start() < b for a, b in negated):
            continue
        if NEGATION_BEFORE_RE.search(segment[max(0, m.start() - 30): m.start()]):
            continue
        return True
    return False


@dataclass
class Claims:
    """Explicit claims extracted from a set of text segments."""

    ram: list[tuple[int, str]] = field(default_factory=list)
    diskless: list[str] = field(default_factory=list)
    included: list[str] = field(default_factory=list)
    exclusions: dict[str, str] = field(default_factory=dict)
    cpus: list[tuple[str, int | None, str]] = field(default_factory=list)
    cpu_missing: list[str] = field(default_factory=list)
    essentials: dict[str, dict[str, list[str]]] = field(
        default_factory=lambda: {k: {"present": [], "missing": []} for k in ESSENTIALS}
    )
    bays: list[tuple[int, str | None, str]] = field(default_factory=list)
    sizes: list[tuple[str, str]] = field(default_factory=list)
    damage: list[str] = field(default_factory=list)
    cosmetic: list[str] = field(default_factory=list)
    tested: list[str] = field(default_factory=list)
    barebones: list[str] = field(default_factory=list)


def extract_claims(segments: list[str], *, contextual: bool, context_ends: set[int] | None = None) -> Claims:
    """Extract explicit claims.

    ``contextual`` enables heading context (e.g. a "Not included:" heading
    followed by a list) used for seller descriptions; titles are single
    segments without context.
    """
    claims = Claims()
    context: str | None = None
    context_left = 0
    for index, raw in enumerate(segments):
        if context_ends and index in context_ends:
            context, context_left = None, 0
        segment = clean_text(raw, 1000)
        if not segment:
            continue
        seg_context = context if context_left > 0 else None
        context_left = max(0, context_left - 1)
        if contextual:
            head = EXCLUDED_HEAD_RE.match(segment)
            if head and len(segment) <= 160:
                rest = head.group("rest").strip()
                if not rest:
                    context, context_left = "excluded", 8
                    continue
                segment, seg_context = rest, "excluded"
            elif HEADING_RE.match(segment) or SECTION_HEAD_RE.fullmatch(segment):
                context = "options" if OPTION_RE.search(segment) else None
                context_left = 8 if context else 0
                continue
            elif seg_context == "excluded" and (INCLUSION_RE.search(segment) or NARRATIVE_RE.search(segment)):
                context, context_left, seg_context = None, 0, None
        option_heading = seg_context == "options"
        option_inline = bool(OPTION_RE.search(segment))
        option_like = option_heading or option_inline
        text = clean_text(segment)
        segment = NEGATED_COMPONENT_LIST_RE.sub(_expand_component_exclusions, segment)
        segment = STATUS_CLAIM_RE.sub(" ", segment)
        # "drive trays/bays/backplane" describe chassis parts, not installed drives.
        storage_segment = DRIVE_PART_RE.sub(" ", segment)

        if seg_context == "excluded":
            if RAM_CONTEXT_RE.search(segment):
                claims.ram.append((0, f"Not included: {text}"))
            if STORAGE_WORD_RE.search(storage_segment):
                claims.diskless.append(f"Not included: {text}")
            for key, pattern, label in ACCESSORIES:
                if pattern.search(segment):
                    claims.exclusions[key] = label
            for key, (mention, _present) in ESSENTIALS.items():
                if mention.search(segment):
                    claims.essentials[key]["missing"].append(f"Not included: {text}")
            if re.search(r"\b(?:cpus?|processors?)\b", segment, _I):
                claims.cpu_missing.append(f"Not included: {text}")
            continue

        if BAREBONES_RE.search(segment):
            claims.barebones.append(text)
        if _damage(segment):
            claims.damage.append(text)
        if COSMETIC_RE.search(segment):
            claims.cosmetic.append(text)
        if TESTED_RE.search(segment):
            claims.tested.append(text)

        # Accessories are explicit exclusions regardless of option wording.
        for key, pattern, label in ACCESSORIES:
            for m in pattern.finditer(segment):
                before = segment[max(0, m.start() - 45): m.start()]
                after = segment[m.end(): m.end() + 30]
                if re.search(r"\b(?:no|without)\b[^.;:]*$", before, _I) or re.search(r"^[^.;:]*\bnot\s+included\b", after, _I):
                    claims.exclusions[key] = label

        # Negative hardware claims survive option wording elsewhere in the
        # segment; option wording suppresses only positive (optional) specs.
        definite = partial(_definite, option_heading=option_heading, option_inline=option_inline)

        if definite(NEG_RAM_RE, segment):
            claims.ram.append((0, text))
        elif not option_like and (RAM_CONTEXT_RE.search(segment) or not contextual) and not CAPACITY_RE.search(segment):
            for value in _ram_values(segment):
                claims.ram.append((value, text))

        # Diskless and included wording are recorded independently so a mixed
        # statement ("No HDD; includes 480GB SSD") becomes a conflict.
        if definite(DISKLESS_RE, storage_segment):
            claims.diskless.append(text)
        if not option_like and not CAPACITY_RE.search(storage_segment) and _included_storage(storage_segment):
            claims.included.append(text)

        if definite(NEG_CPU_RE, segment):
            claims.cpu_missing.append(text)
        elif not option_like:
            for model, count in parse_cpu(segment):
                claims.cpus.append((model, count, text))

        for key, (mention, present) in ESSENTIALS.items():
            if not mention.search(segment) and not present.search(segment):
                continue
            if definite(ESSENTIAL_NEGATIVE[key], segment):
                claims.essentials[key]["missing"].append(text)
            elif not option_like and present.search(segment):
                claims.essentials[key]["present"].append(text)

        if option_like:
            continue
        if not CAPACITY_RE.search(segment):
            for n, size in _bays(segment):
                claims.bays.append((n, size, text))
        for size in _sizes(segment):
            claims.sizes.append((size, text))
    return claims


def summarize_claims(claims: Claims) -> dict[str, Any]:
    """Flatten claims into the public ``parse_description`` dict."""
    ram_values = sorted({v for v, _ in claims.ram})
    storage_state = "unknown"
    storage_conflict = bool(claims.diskless and claims.included)
    if claims.diskless and not claims.included:
        storage_state = "diskless"
    elif claims.included and not claims.diskless:
        storage_state = "included"
    cpu_models = sorted({m for m, _, _ in claims.cpus})
    cpu_counts = sorted({c for _, c, _ in claims.cpus if c is not None})
    bays = sorted({n for n, _, _ in claims.bays})
    sizes = sorted({s for s, _ in claims.sizes} | {s for _, s, _ in claims.bays if s})

    def essential(key: str) -> tuple[str | None, str | None]:
        entry = claims.essentials[key]
        if entry["missing"]:
            return "missing", entry["missing"][0]
        if entry["present"]:
            return "present", entry["present"][0]
        return None, None

    psu, psu_text = essential("psu")
    cooling, cooling_text = essential("cooling")
    nic, nic_text = essential("nic")
    backplane, backplane_text = essential("backplane")
    controller, controller_text = essential("controller")
    return {
        "ram_gb": min(ram_values) if ram_values else None,
        "ram_conflict": len(ram_values) > 1,
        "ram_text": claims.ram[0][1] if claims.ram else None,
        "ram_claims": [{"value": v, "text": t} for v, t in claims.ram[:6]],
        "storage_state": storage_state,
        "storage_conflict": storage_conflict,
        "storage_text": (claims.diskless or claims.included or [None])[0],
        "exclusions": sorted(claims.exclusions.values()),
        "cpu_model": cpu_models[0] if len(cpu_models) == 1 else None,
        "cpu_models": cpu_models,
        "cpu_count": cpu_counts[0] if len(cpu_counts) == 1 else None,
        "cpu_counts": cpu_counts,
        "cpu_conflict": len(cpu_models) > 1 or len(cpu_counts) > 1,
        "cpu_missing": bool(claims.cpu_missing),
        "cpu_text": claims.cpus[0][2] if claims.cpus else (claims.cpu_missing[0] if claims.cpu_missing else None),
        "psu": psu,
        "psu_text": psu_text,
        "cooling": cooling,
        "cooling_text": cooling_text,
        "nic": nic,
        "nic_text": nic_text,
        "backplane": backplane,
        "backplane_text": backplane_text,
        "controller": controller,
        "controller_text": controller_text,
        "drive_bays": bays[0] if len(bays) == 1 else None,
        "bays_conflict": len(bays) > 1,
        "drive_size": sizes[0] if len(sizes) == 1 else None,
        "size_conflict": len(sizes) > 1,
        "bays_text": claims.bays[0][2] if claims.bays else (claims.sizes[0][1] if claims.sizes else None),
        "damage": True if claims.damage else None,
        "damage_text": claims.damage[0] if claims.damage else None,
        "cosmetic_text": claims.cosmetic[0] if claims.cosmetic else None,
        "tested_text": claims.tested[0] if claims.tested else None,
        "barebones": bool(claims.barebones),
    }


def parse_title(title: str) -> dict[str, Any]:
    return summarize_claims(extract_claims([title], contextual=False))


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


@dataclass
class Resolved:
    value: Any
    state: str
    evidence: list[dict[str, Any]]


def _ev(field_name: str, state: str, text: str, url: str | None) -> dict[str, Any]:
    return {"field": field_name, "state": state, "text": clean_text(text), "source_url": url}


def _resolve_numeric(name: str, candidates: list[tuple[int, str, str, str | None]], exact: bool) -> Resolved:
    """candidates: (value, origin, text, url); origin 'seller'|'variant' or 'title'|'index'."""
    if not candidates:
        return Resolved(None, "unknown", [_ev(name, "unknown", f"No explicit {name.replace('_', ' ')} claim found", None)])
    values = {c[0] for c in candidates}
    if len(values) > 1:
        texts = "; ".join(f"{origin}: {text}" for _, origin, text, _ in candidates[:4])
        return Resolved(min(values), "conflicting", [_ev(name, "conflicting", f"Sources disagree ({texts})", candidates[0][3])])
    value = candidates[0][0]
    strong = any(origin in ("seller", "variant") for _, origin, _, _ in candidates)
    state = "confirmed" if strong and exact else "inferred"
    evidence = [_ev(name, state, f"{origin}: {text}", url) for _, origin, text, url in candidates[:3]]
    return Resolved(value, state, evidence)


RAM_LABEL_RE = re.compile(r"memory|\bram\b|\bdimms?\b", _I)
CPU_LABEL_RE = re.compile(r"cpu|processor", _I)
BAY_LABEL_RE = re.compile(r"\bbays?\b|backplane|chassis|form\s*factor|\bsff\b|\blff\b", _I)
COMPONENT_LABEL_RE = re.compile(
    r"power\s+suppl|\bpsus?\b|\bnics?\b|network|ethernet|controller|\braid\b|\bperc\b|\bhba\b|cooling|\bfans?\b|heat\s*sinks?", _I
)
STORAGE_LABEL_RE = re.compile(r"drive|storage|hdd|ssd|disk|nvme", _I)
# Labels that select something other than installed hardware.
BENIGN_LABEL_RE = re.compile(
    r"warranty|colou?r|condition|quantity|\bqty\b|\brails?\b|bezel|\bcords?\b|cable\s+management|licen[cs]e|operating\s+system|\bos\b",
    _I,
)
NONE_VALUE_RE = re.compile(r"\s*(?:none|no|0\s*gb)\s*", _I)
STORAGE_CAPACITY_RE = re.compile(
    r"\b(?:\d+\s*[x×]\s*)?(?:ssds?|hdds?|nvme|sas|sata|storage)\s*[:\-]?\s*\d+(?:\.\d+)?\s*(?:gb|tb)\b"
    r"|\b(?:\d+\s*[x×]\s*)?\d+(?:\.\d+)?\s*(?:gb|tb)\s*(?:ssds?|hdds?|nvme|sas|sata|hard\s*drives?)\b",
    _I,
)


@dataclass
class VariantFacts:
    """Hardware selected by one eBay variation.

    ``*_unresolved`` holds the reason a variation label may change that
    dimension but could not be interpreted; resolution then reports the
    dimension unknown instead of inheriting title/index/seller values.
    """

    ram: int | None = None
    ram_unresolved: str | None = None
    cpu: tuple[str, int | None] | None = None
    cpu_missing: bool = False
    cpu_unresolved: str | None = None
    storage: str | None = None
    storage_conflict: str | None = None
    storage_unresolved: str | None = None
    bays: int | None = None
    size: str | None = None
    bays_unresolved: str | None = None
    size_unresolved: str | None = None
    component_unresolved: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def cpu_touched(self) -> bool:
        return self.cpu is not None or self.cpu_missing or self.cpu_unresolved is not None


def interpret_variant(labels: dict[str, str]) -> VariantFacts:
    facts = VariantFacts()
    for label, value in labels.items():
        shown = f"Variation {label}: {value}"
        value = STATUS_CLAIM_RE.sub(" ", value)
        ram_label = bool(RAM_LABEL_RE.search(label))
        cpu_label = bool(CPU_LABEL_RE.search(label))
        bay_label = bool(BAY_LABEL_RE.search(label))
        storage_label = bool(STORAGE_LABEL_RE.search(label)) and (
            not bay_label or bool(re.search(r"storage|hdd|ssd|disk|nvme", label, _I))
        )
        component_label = bool(COMPONENT_LABEL_RE.search(label))
        if ram_label:
            memory_text = STORAGE_CAPACITY_RE.sub(" ", value)
            products = [m.span() for m in RAM_PRODUCT_RE.finditer(memory_text)]
            capacities = {
                int(m.group(1)) for m in RAM_TOTAL_RE.finditer(memory_text)
                if not any(a <= m.start() < b for a, b in products)
            }
            parsed = parse_title(memory_text)
            if parsed["ram_conflict"] or len(capacities) > 1:
                facts.ram_unresolved = f"{shown} states conflicting memory amounts"
            elif parsed["ram_gb"] is not None:
                facts.ram = parsed["ram_gb"]
            elif NONE_VALUE_RE.fullmatch(value):
                facts.ram = 0
            else:
                facts.ram_unresolved = f"{shown} not interpreted as installed RAM"
        if cpu_label:
            cpus = parse_cpu(value)
            models = {normalize_cpu_key(m) for m, _ in cpus}
            counts = {c for _, c in cpus if c is not None}
            if NEG_CPU_RE.search(value) or NONE_VALUE_RE.fullmatch(value):
                facts.cpu_missing = True
            elif len(models) == 1 and len(counts) <= 1:
                facts.cpu = (cpus[0][0], next(iter(counts), None))
            else:
                facts.cpu_unresolved = f"{shown} not interpreted as one CPU model and count"
        if bay_label:
            found = _bays(value)
            counts = {n for n, _ in found}
            sizes = {s for _, s in found if s} | set(_sizes(value))
            if len(counts) == 1:
                facts.bays = counts.pop()
            else:
                facts.bays_unresolved = f"{shown} not interpreted as one drive bay count"
            if len(sizes) == 1:
                facts.size = sizes.pop()
            else:
                facts.size_unresolved = f"{shown} not interpreted as one drive size"
        if component_label:
            facts.component_unresolved = f"{shown} selects a component that is not verified"
        if storage_label:
            text = NEGATED_COMPONENT_LIST_RE.sub(_expand_component_exclusions, value)
            text = DRIVE_PART_RE.sub(" ", STATUS_CLAIM_RE.sub(" ", text))
            diskless = bool(DISKLESS_RE.search(text) or NONE_VALUE_RE.fullmatch(text))
            included = bool(STORAGE_CAPACITY_RE.search(text) or _included_storage(text))
            if not ram_label:
                included = included or bool(re.search(r"\d+(?:\.\d+)?\s*(?:gb|tb)\b", DISKLESS_RE.sub(" ", text), _I))
            if diskless and included:
                facts.storage_conflict = f"{shown} both excludes and includes drives"
            elif diskless or included:
                facts.storage = "diskless" if diskless else "included"
            else:
                facts.storage_unresolved = f"{shown} not interpreted"
        if not any((ram_label, cpu_label, bay_label, storage_label, component_label)):
            if BENIGN_LABEL_RE.search(label):
                continue
            reason = f"{shown} may change hardware; not interpreted"
            facts.ram_unresolved = facts.ram_unresolved or reason
            facts.cpu_unresolved = facts.cpu_unresolved or reason
            facts.storage_unresolved = facts.storage_unresolved or reason
            facts.bays_unresolved = facts.bays_unresolved or reason
            facts.size_unresolved = facts.size_unresolved or reason
            facts.component_unresolved = facts.component_unresolved or reason
            facts.warnings.append(reason)
            continue
        facts.warnings.extend(
            r for r in (facts.ram_unresolved, facts.cpu_unresolved, facts.storage_unresolved, facts.storage_conflict,
                        facts.bays_unresolved, facts.size_unresolved, facts.component_unresolved)
            if r and r.startswith(shown) and r not in facts.warnings
        )
    return facts


# Retailer installed-configuration text: one claim per line, " | " item or sentence.
RETAIL_SEGMENT_RE = re.compile(r"[\r\n]+|\s+\|\s+|\.\s+(?=[A-Z])")


def _retail_claims(retail: dict[str, Any]) -> dict[str, Any] | None:
    """Claims stated by the retailer as the installed configuration (never capacity or option text)."""
    text = retail.get("installed_text") or ""
    segments = [s for s in RETAIL_SEGMENT_RE.split(text) if s and s.strip()]
    if not segments:
        return None
    return summarize_claims(extract_claims(segments, contextual=True))


def build_configurations(
    record: dict[str, Any],
    catalog: dict[str, Any] | None,
    description: dict[str, Any] | None,
    page: dict[str, Any] | None,
    *,
    retail_status: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return (configurations, listing_updates) for one marketplace listing or retailer product.

    ``record`` is the normalized index record (JSON-safe; decimals as str).
    ``description`` is a stored ``parse_description`` result plus metadata.
    ``page`` is a stored eBay page check result.

    ``retail_status`` (latest retailer acquisition outcome) selects retailer mode: ``record`` is a
    normalized retailer observation with its ``retail`` dict, ``description``/``page`` must be None,
    hardware claims come from the retailer's explicit installed-configuration text and native
    variants, and only a concrete preconfigured variant with its own numeric quote is an exact price.
    """
    retail: dict[str, Any] | None = None
    if retail_status is not None:
        retail = record.get("retail")
        if not isinstance(retail, dict) or description is not None or page is not None:
            raise ValueError("retailer configurations need a retail observation and no eBay description/page")
    item_url = record["source_url"]
    index_url = record.get("index_url")  # vendor index page or retailer catalog this observation came from
    # Retailer claims are cited to the retailer's own product page.
    desc_url = item_url if retail is not None else f"https://itm.ebaydesc.com/itmdesc/{record['item_id']}"
    source_name = "Retailer listing" if retail is not None else "Index"
    title = record["title"]
    title_claims = parse_title(title)
    if retail is not None:
        desc = _retail_claims(retail)
    else:
        desc = description if description and description.get("status") == "verified" else None
    page_ok = page if page and page.get("status") == "verified" else None

    model = record.get("model")
    socket_capacity = catalog.get("socket_capacity") if catalog else None

    base_price = Decimal(record["item_price"]) if record.get("item_price") is not None else None
    base_ship = Decimal(record["shipping_price"]) if record.get("shipping_price") is not None else None
    # Warnings are tagged with the fact they describe ("price", "ram_gb", "psu", ...) so a manual
    # override can replace exactly the warnings of the facts it asserts (``warning_fields``).
    warnings: list[tuple[str, str]] = []
    price_kind = "base"
    if retail is not None:
        shipping_context = (
            "Retailer shipping quote; destination not confirmed; tax excluded"
            if base_ship is not None
            else "Shipping not quoted by retailer; total unknown"
        )
    else:
        shipping_context = (
            "Index shipping quote; destination not confirmed; tax excluded"
            if base_ship is not None
            else "Shipping not quoted by index; total unknown"
        )
    variants: list[dict[str, Any]] = []
    if retail is not None:
        # Only actually available native variants are offers; unavailable variants, quote-only and
        # unavailable products yield no configuration (existing rows are deactivated by the caller).
        if retail.get("kind") != "quote_only" and retail.get("available") is True:
            variants = [
                {"variant_id": str(v["variant_id"]), "labels": dict(v.get("labels") or {}), "price": v.get("price"),
                 "label": clean_text(v["label"], 200) if isinstance(v.get("label"), str) and v["label"].strip() else None,
                 "paired": v.get("paired") is True and retail.get("kind") == "configured"}
                for v in retail.get("variants") or [] if v.get("available") is True
            ]
    elif page_ok:
        if page_ok.get("has_variations"):
            variants = page_ok.get("variations") or []
            price_kind = "range" if page_ok.get("price_kind") == "range" else "base"
            if not variants:
                warnings.append(("price", "Listing has variations that could not be read; price is the displayed base/range price, configuration unverified"))
        else:
            price_kind = "exact" if page_ok.get("price") is not None else "base"
            if page_ok.get("price") is not None:
                base_price = Decimal(page_ok["price"])
        # The current page quote supersedes the index quote; a page quote that
        # is not numeric means the total is unknown, never the stale index value.
        if page_ok.get("shipping") is not None:
            base_ship = Decimal(page_ok["shipping"])
            shipping_context = "eBay page shipping quote for page default location; destination not confirmed; tax excluded"
        elif page_ok.get("shipping_text"):
            base_ship = None
            shipping_context = (
                f"eBay page shipping not numeric ({clean_text(page_ok['shipping_text'], 80)}); "
                "index shipping quote not used; total unknown"
            )
    else:
        warnings.append(("price", "Index starting price; listing variations and selected configuration not verified"))
    if base_ship is None:
        warnings.append(("shipping", "Shipping unknown; quoted total unavailable (not treated as free)"))

    def not_applied(field_name: str, reason: str, what: str) -> Resolved:
        return Resolved(None, "unknown", [_ev(field_name, "unknown", f"{reason}; {what} not applied to this variation", item_url)])

    def cpu_resolution(facts: VariantFacts, exact: bool) -> tuple[Resolved, Resolved]:
        if facts.cpu_unresolved:
            return (not_applied("cpu_model", facts.cpu_unresolved, "title/index/seller CPU"),
                    not_applied("cpu_count", facts.cpu_unresolved, "title/index/seller CPU count"))
        idx_model, idx_count = parse_index_cpu(record.get("cpu_raw"))
        models: list[tuple[str, str, str, str | None]] = []
        counts: list[tuple[int, str, str, str | None]] = []
        seller_count_conflict = False
        if facts.cpu:
            models.append((facts.cpu[0], "variant", "Selected variation", item_url))
            if facts.cpu[1] is not None:
                counts.append((facts.cpu[1], "variant", "Selected variation", item_url))
        elif not facts.cpu_missing:
            if desc and desc.get("cpu_models"):
                for m in desc["cpu_models"]:
                    models.append((m, "seller", desc.get("cpu_text") or "", desc_url))
                seller_counts = desc.get("cpu_counts")
                if seller_counts is None:
                    # Stored results predating ``cpu_counts`` only flag the conflict.
                    seller_counts = [desc["cpu_count"]] if desc.get("cpu_count") is not None else []
                    seller_count_conflict = bool(desc.get("cpu_conflict")) and len(desc["cpu_models"]) <= 1
                for c in seller_counts:
                    counts.append((c, "seller", desc.get("cpu_text") or "", desc_url))
            for m, c in parse_cpu(title):
                models.append((m, "title", title, item_url))
                if c is not None:
                    counts.append((c, "title", title, item_url))
            if idx_model:
                models.append((idx_model, "index", f"{source_name} CPU field: {record.get('cpu_raw')}", index_url))
            if idx_count is not None:
                counts.append((idx_count, "index", f"{source_name} CPU field: {record.get('cpu_raw')}", index_url))
        if not models:
            model_res = Resolved(None, "unknown", [_ev("cpu_model", "unknown", "No CPU model stated", None)])
        else:
            distinct = {normalize_cpu_key(m) for m, *_ in models}
            seller_models = {normalize_cpu_key(m) for m, origin, *_ in models if origin in ("seller", "variant")}
            if len(distinct) > 1:
                model_res = Resolved(None, "conflicting", [_ev("cpu_model", "conflicting", "CPU models disagree: " + ", ".join(sorted({m for m, *_ in models})), models[0][3])])
            else:
                state = "confirmed" if seller_models and exact else "inferred"
                model_res = Resolved(models[0][0], state, [_ev("cpu_model", state, f"{o}: {t}", u) for _, o, t, u in models[:3]])
        if seller_count_conflict:
            count_res = Resolved(None, "conflicting", [_ev("cpu_count", "conflicting", f"Seller description states different CPU counts: {desc.get('cpu_text') or ''}", desc_url)])
        elif counts:
            count_res = _resolve_numeric("cpu_count", counts, exact)
            if count_res.state == "conflicting":
                count_res.value = None
        elif socket_capacity == 1 and model_res.value:
            count_res = Resolved(1, "inferred", [_ev("cpu_count", "inferred", f"Single-socket {model} chassis with a listed CPU ({model_res.value})", catalog.get("source_url") if catalog else None)])
        else:
            count_res = Resolved(None, "unknown", [_ev("cpu_count", "unknown", "Installed CPU count not stated; not inferred from chassis capacity", None)])
        if count_res.value is not None and socket_capacity is not None and count_res.value > socket_capacity:
            count_res = Resolved(None, "conflicting", [_ev("cpu_count", "conflicting", f"Stated {count_res.value} CPUs exceeds {socket_capacity}-socket chassis", None)])
        return model_res, count_res

    def ram_resolution(facts: VariantFacts, exact: bool) -> Resolved:
        if facts.ram_unresolved:
            return not_applied("ram_gb", facts.ram_unresolved, "title/index/seller RAM")
        if facts.ram is not None:
            return Resolved(facts.ram, "confirmed", [_ev("ram_gb", "confirmed", f"Selected variation memory: {facts.ram}GB", item_url)])
        candidates: list[tuple[int, str, str, str | None]] = []
        if desc and desc.get("ram_gb") is not None:
            for claim in desc.get("ram_claims") or [{"value": desc["ram_gb"], "text": desc.get("ram_text") or ""}]:
                candidates.append((claim["value"], "seller", claim["text"], desc_url))
        if title_claims["ram_gb"] is not None:
            for claim in title_claims["ram_claims"]:
                candidates.append((claim["value"], "title", title, item_url))
        if record.get("no_ram_flag"):
            candidates.append((0, "index", f"{source_name} flag: No RAM", index_url))
        elif record.get("source_ram_gb") is not None:
            candidates.append((record["source_ram_gb"], "index", f"{source_name} RAM field: {record['source_ram_gb']}GB", index_url))
        res = _resolve_numeric("ram_gb", candidates, exact)
        if res.state == "inferred" and not exact and res.value:
            res.evidence.append(_ev("ram_gb", "inferred", "Title/index RAM may describe a different variation than the listed base price", item_url))
        return res

    def bays_resolution(facts: VariantFacts) -> tuple[Resolved, Resolved]:
        if facts.bays_unresolved:
            bay_res = not_applied("drive_bays", facts.bays_unresolved, "title/seller bay count")
        elif facts.bays is not None:
            bay_res = Resolved(facts.bays, "confirmed", [_ev("drive_bays", "confirmed", f"Selected variation bays: {facts.bays}", item_url)])
        elif desc and desc.get("bays_conflict"):
            bay_res = Resolved(None, "conflicting", [_ev("drive_bays", "conflicting", f"Seller bay counts disagree: {desc.get('bays_text') or ''}", desc_url)])
        elif title_claims["bays_conflict"]:
            bay_res = Resolved(None, "conflicting", [_ev("drive_bays", "conflicting", f"Title bay counts disagree: {title}", item_url)])
        else:
            bays: list[tuple[int, str, str, str | None]] = []
            if desc and desc.get("drive_bays") is not None:
                bays.append((desc["drive_bays"], "seller", desc.get("bays_text") or "", desc_url))
            if title_claims["drive_bays"] is not None:
                bays.append((title_claims["drive_bays"], "title", title, item_url))
            bay_res = _resolve_numeric("drive_bays", bays, True)

        if facts.size_unresolved:
            return bay_res, not_applied("drive_size", facts.size_unresolved, "title/seller drive size")
        if facts.size:
            return bay_res, Resolved(facts.size, "confirmed", [_ev("drive_size", "confirmed", f"Selected variation drive size: {facts.size}", item_url)])
        if desc and desc.get("size_conflict"):
            return bay_res, Resolved(None, "conflicting", [_ev("drive_size", "conflicting", f"Seller SFF/LFF claims disagree: {desc.get('bays_text') or ''}", desc_url)])
        if title_claims["size_conflict"]:
            return bay_res, Resolved(None, "conflicting", [_ev("drive_size", "conflicting", f"Title SFF/LFF claims disagree: {title}", item_url)])
        sizes: list[tuple[str, str, str, str | None]] = []
        if desc and desc.get("drive_size"):
            sizes.append((desc["drive_size"], "seller", desc.get("bays_text") or "", desc_url))
        if title_claims["drive_size"]:
            sizes.append((title_claims["drive_size"], "title", title, item_url))
        if not sizes:
            size_res = Resolved(None, "unknown", [_ev("drive_size", "unknown", "Drive size not stated", None)])
        elif len({s for s, *_ in sizes}) > 1:
            size_res = Resolved(None, "conflicting", [_ev("drive_size", "conflicting", "SFF/LFF claims disagree", sizes[0][3])])
        else:
            state = "confirmed" if any(o == "seller" for _, o, _, _ in sizes) else "inferred"
            size_res = Resolved(sizes[0][0], state, [_ev("drive_size", state, f"{o}: {t}", u) for _, o, t, u in sizes[:2]])
        return bay_res, size_res

    def storage_resolution(facts: VariantFacts, exact: bool) -> Resolved:
        if facts.storage_conflict:
            return Resolved("unknown", "conflicting", [_ev("storage", "conflicting", facts.storage_conflict, item_url)])
        if facts.storage_unresolved:
            return Resolved("unknown", "unknown", [_ev("storage", "unknown", f"{facts.storage_unresolved}; title/seller storage not applied to this variation", item_url)])
        if facts.storage:
            return Resolved(facts.storage, "confirmed", [_ev("storage", "confirmed", f"Selected variation storage: {facts.storage}", item_url)])
        if desc and desc.get("storage_conflict"):
            return Resolved("unknown", "conflicting", [_ev("storage", "conflicting", "Seller description both includes and excludes drives", desc_url)])
        if title_claims["storage_conflict"]:
            return Resolved("unknown", "conflicting", [_ev("storage", "conflicting", f"Title both includes and excludes drives: {title}", item_url)])
        cands: list[tuple[str, str, str, str | None]] = []
        if desc and desc.get("storage_state") in ("diskless", "included"):
            cands.append((desc["storage_state"], "seller", desc.get("storage_text") or "", desc_url))
        if title_claims["storage_state"] in ("diskless", "included"):
            cands.append((title_claims["storage_state"], "title", title, item_url))
        elif title_claims["barebones"]:
            cands.append(("diskless", "title", f"Barebones: {title}", item_url))
        if not cands:
            return Resolved("unknown", "unknown", [_ev("storage", "unknown", "Included storage not stated", None)])
        if len({c[0] for c in cands}) > 1:
            return Resolved("unknown", "conflicting", [_ev("storage", "conflicting", "; ".join(f"{o}: {c}" for c, o, _, _ in cands), cands[0][3])])
        state = "confirmed" if any(o == "seller" for _, o, _, _ in cands) and exact else "inferred"
        return Resolved(cands[0][0], state, [_ev("storage", state, f"{o}: {t}", u) for _, o, t, u in cands[:2]])

    def claim_source(key: str) -> tuple[str | None, str | None, str | None]:
        """(state, text, url) for an essential: missing claims first, then seller, then title."""
        seller_state = desc.get(key) if desc else None
        title_state = title_claims.get(key)
        if seller_state == "missing":
            return "missing", desc.get(f"{key}_text"), desc_url
        if title_state == "missing":
            return "missing", title, item_url
        if seller_state == "present":
            return "confirmed", desc.get(f"{key}_text"), desc_url
        if title_state == "present":
            return "present", title, item_url
        return None, None, None

    def essentials_resolution(
        cpu_model: Resolved, ram: Resolved, bay_res: Resolved, facts: VariantFacts
    ) -> tuple[dict[str, str], list[dict[str, Any]], list[tuple[str, str]], list[tuple[str, str]]]:
        states: dict[str, str] = {}
        evidence: list[dict[str, Any]] = []
        missing: list[tuple[str, str]] = []
        notes: list[tuple[str, str]] = []
        cpu_stated_missing = facts.cpu_missing or (
            facts.cpu is None and bool((desc and desc.get("cpu_missing")) or title_claims["cpu_missing"])
        )
        if cpu_stated_missing:
            states["cpu"] = "missing"
            missing.append(("cpu_included", "CPU stated as not included"))
        else:
            states["cpu"] = "confirmed" if cpu_model.state == "confirmed" else ("present" if cpu_model.value else "unknown")
        if ram.value == 0 and ram.state in CERTAIN:
            states["ram"] = "missing"
            missing.append(("ram_gb", "No RAM installed"))
        else:
            states["ram"] = "confirmed" if ram.state == "confirmed" and ram.value else "unknown"
        for key, label in (("psu", "Power supplies"), ("cooling", "Cooling (fans/heatsinks)"), ("nic", "Network adapter")):
            state, text, url = claim_source(key)
            if state == "missing":
                states[key] = "missing"
                missing.append((key, f"{label} not included"))
                evidence.append(_ev(key, "confirmed", f"Missing: {text}", url))
            elif state == "confirmed":
                states[key] = "confirmed"
                evidence.append(_ev(key, "confirmed", f"seller: {text}", url))
            elif state == "present":
                states[key] = "present"
                evidence.append(_ev(key, "inferred", f"title: {text}", url))
            else:
                states[key] = "unknown"
                evidence.append(_ev(key, "unknown", f"{label} not stated", None))

        # Drive path: an explicitly missing backplane leaves the bays unusable.
        # A missing add-in controller needs review only: onboard SATA may
        # still drive the bays, so it is not inferred to be non-functional.
        backplane, bp_text, bp_url = claim_source("backplane")
        controller, ctl_text, ctl_url = claim_source("controller")
        if backplane == "missing":
            states["drive_path"] = "missing"
            missing.append(("drive_path", "Drive backplane not included"))
            evidence.append(_ev("drive_path", "confirmed", f"Missing backplane: {bp_text}", bp_url))
        elif controller == "missing":
            states["drive_path"] = "review"
            notes.append(("drive_path", "Storage/RAID controller stated as not included; drive connectivity relies on onboard controller support (not verified)"))
            evidence.append(_ev("drive_path", "unknown", f"Controller not included: {ctl_text}", ctl_url))
        elif backplane == "confirmed":
            states["drive_path"] = "confirmed"
            evidence.append(_ev("drive_path", "confirmed", f"seller: {bp_text}", bp_url))
        elif bay_res.state == "confirmed" and bay_res.value:
            states["drive_path"] = "confirmed"
            evidence.append(_ev("drive_path", "confirmed", f"Seller-stated drive bays: {bay_res.value}", desc_url if desc else item_url))
        elif backplane == "present" or controller in ("confirmed", "present") or bay_res.value:
            states["drive_path"] = "present"
            evidence.append(_ev("drive_path", "inferred", f"Drive bays/controller listed; backplane not confirmed: {bp_text or ctl_text or title}", bp_url or ctl_url or item_url))
        else:
            states["drive_path"] = "unknown"
            evidence.append(_ev("drive_path", "unknown", "Drive backplane/controller not stated", None))
        if facts.component_unresolved:
            notes.append(("variant", facts.component_unresolved))
        return states, evidence, missing, notes

    exclusions = sorted(set((desc or {}).get("exclusions") or []) | set(title_claims["exclusions"]))

    damage_text = (desc or {}).get("damage_text") or title_claims.get("damage_text")
    condition = (page_ok or {}).get("condition") or record.get("condition")
    damage: bool | None = True if damage_text else None
    if condition and re.search(r"for\s+parts|not\s+working", condition, _I):
        damage, damage_text = True, f"Condition: {condition}"
    cosmetic = (desc or {}).get("cosmetic_text")

    def build(variant: dict[str, Any] | None) -> dict[str, Any]:
        # A retailer variant pairs its price with its hardware only as a concrete preconfigured option.
        paired = variant is not None and variant.get("price") is not None and (
            retail is None or variant.get("paired") is True)
        exact = (price_kind == "exact" or variant is not None) if retail is None else paired
        labels = (variant or {}).get("labels") or {}
        facts = interpret_variant(labels)
        cpu_model, cpu_count = cpu_resolution(facts, exact)
        ram = ram_resolution(facts, exact)
        storage = storage_resolution(facts, exact)
        bay_res, size_res = bays_resolution(facts)
        states, ess_evidence, missing, notes = essentials_resolution(cpu_model, ram, bay_res, facts)
        # Explicitly missing essentials outrank damage: both are recorded
        # (``damage`` stays True) but "missing" is the stronger contradiction.
        if missing:
            hardware_state = "missing"
        elif damage:
            hardware_state = "damaged"
        elif not facts.component_unresolved and all(
            states[k] == "confirmed" for k in ("cpu", "ram", "psu", "cooling", "nic", "drive_path")
        ):
            hardware_state = "confirmed"
        else:
            hardware_state = "review"

        item_price = Decimal(variant["price"]) if variant and variant.get("price") is not None else base_price
        ship = base_ship
        kind = "exact" if paired else price_kind
        if item_price is None:
            kind = "unknown"
        # Only the explicitly paired index CPU/count can qualify its core total.
        # A seller/title count or a selected CPU must not borrow another CPU's cores.
        index_model, index_count = parse_index_cpu(record.get("cpu_raw"))
        total_cores = None
        if (cpu_count.value and cpu_count.state in CERTAIN and cpu_model.state in CERTAIN
                and index_count == cpu_count.value
                and normalize_cpu_key(index_model) == normalize_cpu_key(cpu_model.value)
                and record.get("total_cores") and not facts.cpu_touched):
            if record["total_cores"] % cpu_count.value == 0:
                total_cores = record["total_cores"]

        tagged: list[tuple[str, str]] = list(warnings) + [("variant", w) for w in facts.warnings]
        if paired:
            tagged = [(f, w) for f, w in tagged if not w.startswith("Index starting price")]
        elif retail is not None:
            tagged.insert(0, ("price", "Retailer starting/base price; the configuration actually ordered is not "
                                       "priced by this quote"))
        if ram.state == "conflicting":
            tagged.append(("ram_gb", "RAM claims conflict; the lowest stated amount is used and cannot satisfy a confirmed match"))
        if ram.value == 0:
            tagged.append(("ram_gb", "No RAM installed in this configuration"))
        if cpu_count.state == "unknown":
            tagged.append(("cpu_count", "Installed CPU count unknown"))
        elif cpu_count.state == "conflicting":
            tagged.append(("cpu_count", "CPU count claims conflict"))
        if storage.state == "unknown":
            tagged.append(("storage_state", "Included storage not stated"))
        elif storage.state == "conflicting":
            tagged.append(("storage_state", "Storage claims conflict; not treated as diskless"))
        for key in ("psu", "cooling", "nic"):
            if states[key] == "unknown":
                tagged.append((key, f"{dict(psu='PSU', cooling='Cooling', nic='NIC')[key]} not confirmed"))
        if states["drive_path"] == "unknown":
            tagged.append(("drive_path", "Drive backplane not confirmed"))
        tagged.extend(notes)
        if cosmetic:
            tagged.append(("other", f"Cosmetic note: {clean_text(cosmetic, 120)}"))
        if damage_text:
            tagged.append(("damage", f"Damage disclosed: {clean_text(damage_text, 160)}"))
        if (desc or {}).get("tested_text"):
            tagged.append(("other", "Seller testing claim is not an independent component verification"))
        tagged.extend(missing)
        if bay_res.state == "unknown":
            tagged.append(("drive_bays", "Drive bay count not stated"))
        elif bay_res.state == "conflicting":
            tagged.append(("drive_bays", "Drive bay claims conflict"))
        if size_res.state == "conflicting":
            tagged.append(("drive_size", "SFF/LFF claims conflict"))
        warning_fields: dict[str, list[str]] = {}
        for name, text in tagged:
            if text not in warning_fields.setdefault(name, []):
                warning_fields[name].append(text)

        evidence = (
            cpu_model.evidence + cpu_count.evidence + ram.evidence + bay_res.evidence + size_res.evidence
            + storage.evidence + ess_evidence
        )
        if retail is not None:
            price_text = (f"Retailer quote {item_price} for this preconfigured option" if kind == "exact" else
                          f"Retailer starting/base price {item_price}; configuration-specific price not quoted")
        else:
            price_text = (f"Selected variation price {item_price}" if variant else
                          f"eBay page price {item_price}" if kind == "exact" else
                          f"{'Page range' if kind == 'range' else 'Index/base'} price {item_price}; selected configuration not verified")
        evidence.append(_ev("price", "confirmed" if kind == "exact" else "inferred" if kind in ("base", "range") else "unknown",
                            price_text, item_url if variant or kind == "exact" or retail is not None else index_url))
        evidence.append(_ev("shipping", "inferred" if ship is not None else "unknown", shipping_context,
                            item_url if page_ok or retail is not None else index_url))
        if exclusions:
            evidence.append(_ev("accessories", "confirmed", "; ".join(exclusions), desc_url if desc and desc.get("exclusions") else item_url))
        if damage_text:
            evidence.append(_ev("damage", "confirmed", damage_text, desc_url if (desc or {}).get("damage_text") else item_url))
        return {
            "variant_key": (variant or {}).get("variant_id") or "",
            "variant_id": (variant or {}).get("variant_id"),
            # Hardware labels when named; otherwise the retailer's own native option title (display only,
            # never interpreted as installed hardware).
            "variant_label": ", ".join(f"{k}: {v}" for k, v in labels.items()) or (variant or {}).get("label"),
            "model": model,
            "cpu_model": cpu_model.value,
            "cpu_key": normalize_cpu_key(cpu_model.value),
            "cpu_model_state": cpu_model.state,
            "cpu_count": cpu_count.value,
            "cpu_count_state": cpu_count.state,
            "total_cores": total_cores,
            "ram_gb": ram.value,
            "ram_state": ram.state,
            "drive_bays": bay_res.value if bay_res.state != "conflicting" else None,
            "bays_state": bay_res.state,
            "drive_size": size_res.value,
            "size_state": size_res.state,
            "storage_state": storage.value if storage.value in ("diskless", "included") else "unknown",
            "storage_conf": storage.state,
            "hardware_state": hardware_state,
            "damage": damage,
            "price_kind": kind,
            "item_price": item_price,
            "shipping_price": ship,
            "currency": "USD",
            "shipping_context": shipping_context,
            "evidence": evidence,
            "warnings": list(dict.fromkeys(w for _, w in tagged)),
            "exclusions": exclusions,
            # Structured per-essential states (cpu, ram, psu, cooling, nic, drive_path), persisted by the
            # collector, and the fact each warning describes (not persisted; used by manual overrides).
            "essential_states": dict(states),
            "warning_fields": warning_fields,
        }

    if variants or retail is not None:
        configs = [build(v) for v in variants]
    else:
        configs = [build(None)]

    detail_status = "never"
    statuses = [s for s in ((description or {}).get("status"), (page or {}).get("status")) if s]
    if retail is not None:
        # A retailer product endpoint observation is a source detail check, never a page verification.
        detail_status = {"blocked": "blocked", "failed": "failed"}.get(
            retail_status, "partial" if retail.get("method") == "retailer_product" else "never")
    elif page_ok:
        detail_status = "verified"
    elif "verified" in statuses:
        detail_status = "partial"
    elif "blocked" in statuses:
        detail_status = "blocked"
    elif "failed" in statuses:
        detail_status = "failed"
    listing = {
        "detail_status": detail_status,
        "seller": (page_ok or {}).get("seller"),
        "condition": condition,
        "ended": bool((page or {}).get("ended")),
        "ended_text": (page or {}).get("ended_text"),
    }
    return configs, listing
