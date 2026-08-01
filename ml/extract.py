"""Extract what is actually being sold, from a listing's title.

The gap this fills, measured rather than assumed. `epid` identifies the
product model exactly, and CLIP retrieves things that look alike, and neither
can see what is in the box. Inside one Switch OLED epid sit a bare "Tablet
Only" and a "Tablet Charge And Dock And HDMI Cord" bundle at the same $159.99,
across a $127.99 to $255.00 range. Across the corpus:

  lots       "Lot of 50 SK Hynix 64GB" at $113,000 in the same category as
             single sticks. Removing 2% of the RAM category drops its mean 28%.
  defects    graphics cards flagged for-parts/cracked median $151 against
             $420 for clean ones.
  bundling   consoles: bare $129.99, unstated $174.99, with-extras $190.00.

None of it is in eBay's structured aspects, which carry Brand, Model, Color,
MPN and Storage Capacity and nothing about what is included.

Rule-based on purpose, not a model. The vocabulary is small and stable, the
signals are sparse (0.6% to 4.2% of titles each), there are no labels, and a
regex that fires on "for parts" can be audited in a way a decision boundary
cannot. See docs/decisions/0012-variant-extraction.md.

High precision, low recall, deliberately. Most listings say nothing about
completeness and come back None, which means *unstated*, never "complete".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --- completeness -----------------------------------------------------------

BARE = "bare"
COMPLETE = "complete"
BUNDLE = "bundle"

# "console only", "tablet-only", "body only", "unit only", and the inverted
# "only console". The noun list is deliberately short: a generic "X only"
# matches things like "1 owner only" and "local pickup only" that say nothing
# about what is included.
_BARE_RE = re.compile(
    r"\b(console|tablet|handheld|unit|body|head|adapter|card|cpu|gpu|drive)"
    r"\s*[-:]?\s*only\b|\bonly\s+(console|tablet|handheld|unit|body)\b",
    re.IGNORECASE,
)

# An explicit bundle, or extras beyond the item itself.
_BUNDLE_RE = re.compile(r"\bbundle\b|\blot\s+includes\b|\bcombo\b", re.IGNORECASE)

# Things that are accessories to the main item rather than the item itself.
# One shared vocabulary, used three ways below.
_ACCESSORY = (
    r"box|case|charger|charging|cable|cord|dock|controller|joy[\s-]?cons?|"
    r"game|sd\s*card|memory\s*card|adapter|manual|accessor\w*|bag|strap|stand|"
    r"remote|battery|screen\s*protector|hdmi|power\s*supply|grip|carrying"
)
_ACCESSORY_RE = re.compile(rf"\b(?:{_ACCESSORY})\b", re.IGNORECASE)

# "with", "w/", "includes", but only when an accessory actually follows. A
# bare "with" is common and meaningless: "works with PC", "compatible with
# Switch". An earlier version also accepted any digit after "with", which
# classified "Gaming PC works with 4K monitors" as a bundle. Precision matters
# more than recall here, so the accessory vocabulary is the whole gate.
_INCLUDES_RE = re.compile(
    rf"\b(?:w/|w\s*/|with|inc\.?|incl\.?|includes|including)\s+(?=[^,]*\b(?:{_ACCESSORY})\b)",
    re.IGNORECASE,
)

# "Console - White + 256GB SD Card". The plus form is common on eBay and
# unambiguous when an accessory follows it.
_PLUS_RE = re.compile(rf"\+\s*[^,+]*\b(?:{_ACCESSORY})\b", re.IGNORECASE)

# Sellers often just list the contents without any joining word at all:
# "Tablet Charge And Dock And HDMI Cord". Two or more distinct accessory
# nouns is a decent signal that the title is enumerating what is in the box.
# One is not: "Console - no dock" and "Switch case" would both false-positive.
MIN_IMPLIED_ACCESSORIES = 2

# --- defects ----------------------------------------------------------------

_DEFECT_RE = re.compile(
    r"\bfor\s+parts\b|\bparts\s+only\b|\bnot\s+work\w*\b|\bdoes\s*n[o']?t\s+work\b|"
    r"\bas[\s-]is\b|\bbroken\b|\bcracked?\b|\bchipped\b|\bdamaged?\b|\bfaulty\b|"
    r"\bdefect\w*\b|\bfor\s+repair\b|\bneeds?\s+repair\b|\bspares?\s+or\s+repair\b|"
    r"\bunteste\w+\b|\bdead\b|\bwon'?t\s+(?:power|turn|boot)\b|"
    # Found on real RTX 3090 listings sitting at half price inside an
    # otherwise-clean model group: hardware that is intact but does not work.
    r"\bno\s+(?:power|display|video|output|signal|boot|post)\b|"
    r"\bdoes\s*n[o']?t\s+(?:display|post|boot|power)\b|"
    # "Gaming Laptop ... Bad Motherboard" surfaced as a 58% discount in the
    # first real deal scan. A named broken component is as clear a defect
    # signal as "for parts" and was simply missing from the vocabulary.
    r"\bbad\s+(?:motherboard|mobo|board|screen|display|battery|port|slot|ram|"
    r"memory|fan|pcb|lcd|digitizer)\b|"
    # A named component explicitly absent. "RTX 5090 *MISSING CORE*" at $199
    # and "Zephyrus M16 NO MEM NO HDD" at $400 both surfaced as large fake
    # discounts, comped against complete units. An item missing its core part
    # is not a cheap version of the whole, it is a different thing.
    r"\bmissing\s+(?:core|vram|die|board|pcb)\b|\bno\s+(?:core|vram|die)\b",
    re.IGNORECASE,
)

# A machine sold without some of its components. NOT a defect: an HP Z8
# workstation listed "NO GPU" and a laptop listed "NO MEM NO HDD" are working
# machines in a reduced configuration, and calling them broken flagged real
# $1,800-$4,900 listings. They are `bare` completeness, which already means
# "less than the full package" and already keeps them out of comp sets built
# for complete units.
#
# The distinction that matters: a *machine* missing a component is a
# configuration; a *component* missing its own core (a GPU with no die) is
# broken, and stays in the defect vocabulary above.
_MISSING_COMPONENT_RE = re.compile(
    r"\b(?:no|without|missing)\s+"
    r"(?:gpu|graphics?\s*card|cpu|processor|mem(?:ory)?|ram|hdd|ssd|drive|"
    r"storage|os|battery|charger|psu|power\s*supply)\b",
    re.IGNORECASE,
)

# A seller writing READ / READ DESCRIPTION is flagging a caveat without saying
# what. Recorded as a signal, deliberately NOT treated as a defect: it is a
# warning that something is unusual, not evidence of what.
_READ_RE = re.compile(r"\bread\s*(?:description|desc|details|carefully|first|below|!+)?\b",
                      re.IGNORECASE)

# Captured but not yet used to downgrade completeness. Getting negation scope
# right in title-case fragments ("no charger" vs "no scratches, charger
# included") is harder than everything else here and is not needed to capture
# most of the value. See the Consequences section of ADR 0012.
_NEGATION_RE = re.compile(r"\b(?:no|without|missing|lacks?)\s+(\w+(?:\s+\w+)?)", re.IGNORECASE)

# --- lots -------------------------------------------------------------------

# "Lot of 50", "LOT OF 10 -", "*LOT OF 2*", "5-pack", "pack of 3".
#
# Note what is deliberately ABSENT: any "Nx" or "xN" quantity form. It is the
# obvious way to write a multipack and it is unusable on this corpus, because
# in PC hardware an x next to a number is almost never a count. Measured
# against the real titles, those patterns matched 1,789 listings (14.1%) and
# essentially all of them were false: "RX 6700 XT" -> "6700 x", "Gen 4.0 x 4"
# -> PCIe lanes, "Ryzen 5 7600X" -> a CPU model, "VENTUS 3X PLUS" -> a product
# line, "PCIe 4.0 x16" -> lane count. A genuine "2x RTX 3090" is lost as a
# result, which is the right trade: a false lot silently deletes a valid comp,
# and this corpus is overwhelmingly PC parts.
_LOT_COUNT_RE = re.compile(
    r"\blot\s+of\s+(\d{1,4})\b|\b(\d{1,4})[\s-]*pack\b|\bpack\s+of\s+(\d{1,4})\b"
    r"|\b(\d{1,4})\s*(?:pcs|pieces|units)\b"
    r"|\blot\s*\(\s*(\d{1,4})\s*\)",
    re.IGNORECASE,
)
# "lot" as a noun, NOT the English quantifier. The negative lookahead is the
# whole point: "Lot of 12" and "Record Lot" are lots, while "lots of
# character" and "a lot of storage space" are ordinary descriptions that
# happen to contain the word. That distinction barely arises in a corpus of
# PC parts and arises constantly in clothing and furniture, so it only shows
# up when the saved searches broaden. A false lot silently deletes a valid
# comp, so precision wins over catching every unnumbered "Vinyl Record Lot".
_LOT_WORD_RE = re.compile(
    r"\bjob\s*lot\b|\bbulk\b|\bwholesale\b|\bpallet\b|\blot\b(?!\s+of\s+[a-z])",
    re.IGNORECASE,
)

# A lot needs at least two units to be a lot. "x1" and "Lot of 1" are single
# items described oddly, and excluding them from comps would lose real data.
MIN_LOT_SIZE = 2

# --- sealed / new -----------------------------------------------------------

_SEALED_RE = re.compile(
    r"\bsealed\b|\bbrand\s+new\b|\bbnib\b|\bnib\b|\bnew\s+in\s+box\b|\bunopened\b",
    re.IGNORECASE,
)

# --- accessories pretending to be products ----------------------------------
#
# The worst comp poisoner found so far, and it was invisible until listings
# were grouped by a key that should have made them comparable. Grouping
# graphics cards by chipset gave rtx-3090 a price spread of 1428x, and the low
# end was entirely parts *for* the card rather than the card: a $6.61 manual,
# a $34.99 backplate, an $88 heatsink assembly, a $187 NVLink bridge, a $199
# case. They match on model string and on image (a photo of a GPU cooler looks
# like a GPU), so neither epid nor CLIP rejects them, and they sit at 2-20% of
# the real price. See docs/decisions/0013-spec-extraction.md.
_ACCESSORY_NOUN = (
    r"backplate|back\s?plate|heat\s?sink|cooler|shroud|bracket|water\s?block|"
    r"nvlink|bridge|riser|mount|manual|guide|sticker|decal|screw|thermal\s?pad|"
    r"cooling\s?fan|fan\s?assembly|replacement\s+part|empty\s+box|box\s+only|"
    r"paste|standoff|adapter\s+plate|dust\s+cover|anti[\s-]?sag"
)
_ACCESSORY_NOUN_RE = re.compile(rf"\b(?:{_ACCESSORY_NOUN})\b", re.IGNORECASE)

# Nouns that are never the product when the title also names a specific model.
# These need no "for"/"only" gate, and requiring one was a real miss: the deal
# scanner's entire top-8 was accessories reading as 93-96% discounts, and not
# one of them said "for", "replacement" or "assembly". They just said what they
# were: "*EMPTY BOX* RTX 5070", "GPU Cooling Fan", "Cooler Heatsinks Fans + 2
# Backplates", "Shell ONLY". A mispriced accessory looks exactly like a huge
# discount, so these are the errors a deal ranking surfaces first.
# Note what is NOT here: "retail box" and "original box". A card advertised
# "with Original Box" is a real $4,750 card whose seller kept the packaging,
# and treating that as an accessory listing flagged several of the most
# expensive genuine items in the corpus. The genuinely accessory phrasing is
# "Retail Box ONLY", which _ANY_ACCESSORY_ONLY_RE already covers.
_STRONG_ACCESSORY = (
    # heat sinks were dropped from this list by accident when the box terms
    # came out, and the deal feed surfaced it within minutes: a "Video
    # Heatsink Fan" at $79.99 ranked as a 91% discount against a $900 card.
    r"backplates?|back\s?plates?|heat\s?sinks?|shrouds?|water\s?blocks?|waterblocks?|"
    r"nvlink|cooling\s?fans?|fan\s?assembly|thermal\s?pads?|empty\s+box|shell|"
    # "Replacement Fans (Set of 3)". Deliberately NOT bare "fan": "3 fan" and
    # "triple fan" describe a real card's own cooler and are extremely common
    # in genuine GPU titles.
    r"replacement\s+fans?|spare\s+fans?|cooling\s+system|"
    # A donor board exists to be stripped for parts, which is definitionally
    # not the product. "RTX 5090 PCB Donor Board" ranked as a 98% discount.
    r"donor|pcb\s+only|core\s+only|die\s+only"
)
# An accessory noun preceded by one of these is INCLUDED with the product,
# not the product itself. This is the third time the same shape has appeared:
# "no GPU", "case", and now "waterblock" all mean opposite things depending on
# what precedes them. Precision here comes from finding the discriminator, not
# from lengthening the vocabulary.
# Searched anywhere in the preceding window, NOT anchored to its end:
# sellers put words between the joiner and the noun ("With EKWB Founders
# Waterblock"), and an end-anchored version silently never matched, which
# flagged a $4,999 graphics card as a part.
_INCLUSION_PREFIX_RE = re.compile(
    r"\b(?:with|w/|w\s*/|includes?|including|comes?\s+with|plus)\b|\+",
    re.IGNORECASE,
)

_STRONG_ACCESSORY_RE = re.compile(rf"\b(?:{_STRONG_ACCESSORY})\b", re.IGNORECASE)

# "X ONLY" where X is any accessory noun. Kept separate from the vocabulary
# above because "box only" appears in both, and an earlier version required a
# *second* "only" after it, so "Retail Box ONLY" never matched.
_ACCESSORY_ONLY_VOCAB = (
    rf"{_STRONG_ACCESSORY}|box|case|cover|packaging|manual|guide|cable|bracket"
)
# Both word orders. Sellers write "Heatsink ONLY" and "ONLY Cooling System"
# interchangeably, and matching only the first let the second straight
# through: a "RTX 5090 Only Cooling System" ranked as a 99% discount against
# a $4,600 card.
_ANY_ACCESSORY_ONLY_RE = re.compile(
    rf"\b(?:{_ACCESSORY_ONLY_VOCAB})\b[^,]{{0,24}}\bonly\b"
    rf"|\bonly\b[^,]{{0,24}}\b(?:{_ACCESSORY_ONLY_VOCAB})\b",
    re.IGNORECASE,
)
# "for" is the precision gate. An accessory noun alone appears in 2.9% of
# titles, most of them legitimately ("comes with cable"); paired with "for" it
# drops to 1.0% and every sampled instance was a genuine accessory listing.
# "for" alone missed "3-Fan Heatsink Cooler Assembly GPU Replacement", which
# names no product it is for and is unmistakably a part. "assembly" and
# "replacement" beside an accessory noun are the same claim in different words.
_FOR_RE = re.compile(r"\bfor\b|\breplacement\b|\bassembly\b", re.IGNORECASE)
# "Heatsink ONLY", "guide books ONLY": the same claim without a "for".
_ACCESSORY_ONLY_RE = re.compile(rf"\b(?:{_ACCESSORY_NOUN})\b[^,]{{0,30}}\bonly\b", re.IGNORECASE)
# The seller saying outright that the product is not included.
_NOT_THE_PRODUCT_RE = re.compile(
    r"\bno\s+(?:graphics?\s+card|gpu|video\s+card|console|phone|device|pcb|board)\b",
    re.IGNORECASE,
)
# Nouns that are an accessory when they are the *subject* of the title and
# ordinary inclusions when mentioned later. "CASE for GIGABYTE AORUS RTX 3090"
# is a case; "Nintendo Switch with carrying case" is a console. Anchoring to
# the start is what separates them, and it is more precise than adding these
# to the general vocabulary and hoping the "for" gate holds.
_SUBJECT_ACCESSORY_RE = re.compile(
    r"^\W*(?:case|box|cover|skin|sleeve|bag|shell|stand|holder|dock)\b", re.IGNORECASE
)

# --- specs ------------------------------------------------------------------

# Largest capacity mentioned, normalized to GB. Titles list several ("64GB RAM
# 1TB SSD"), and the largest is the one describing the item being sold far more
# often than not. TB is converted so 1TB and 1024GB compare.
_CAPACITY_RE = re.compile(r"\b(\d{1,5})\s*(GB|TB)\b", re.IGNORECASE)
# Guards against model numbers being read as capacities. Nothing in this corpus
# legitimately has 100TB, and "PC5-38400" style codes are excluded by requiring
# the GB/TB suffix anyway.
MAX_PLAUSIBLE_CAPACITY_GB = 100_000

# Drives write it several ways: "PCIe 4.0", "PCIe Gen 4", and bare "Gen 3.0".
# The bare form is safe because "13th Gen Intel" puts a word after Gen, not a
# digit, so requiring a digit immediately after excludes CPU generations.
_GENERATION_RE = re.compile(
    r"\b(DDR[345])\b|\bPCIe?\s*(?:Gen\s*)?([345])(?:\.0)?\b|\bGen\s*([345])(?:\.0)?\b",
    re.IGNORECASE,
)

# Memory and drives both have a form factor that moves price independently of
# capacity: 32GB DDR4 laptop memory medians $110.99 against $149.99 desktop.
# Form factor and generation are memory/storage concepts. Requiring one of
# these words keeps them from firing on unrelated categories, which matters
# as soon as the saved searches cover anything but PC parts.
_COMPONENT_CONTEXT_RE = re.compile(
    r"\b(?:RAM|memory|DIMM|SODIMM|SSD|HDD|NVMe|SATA|M\.?2|drive|"
    r"DDR[345]|PC[45]-\d|storage|hard\s?disk)\b",
    re.IGNORECASE,
)

_FORM_FACTOR_RE = (
    ("server", re.compile(r"\b(?:ECC|RDIMM|LRDIMM|registered|server)\b", re.IGNORECASE)),
    ("laptop", re.compile(r"\b(?:SODIMM|SO-DIMM|laptop|notebook)\b", re.IGNORECASE)),
    ("m.2", re.compile(r"\bM\.?2\b|\b22\d{2}\b", re.IGNORECASE)),
    # No trailing \b after the quote: a word boundary between '"' and a space
    # never matches (both are non-word characters), so `2.5" SATA SSD` was
    # silently missed while `2.5 inch` worked.
    ("2.5in", re.compile(r"\b2\.5\s*(?:\"|''|inch\b|in\b)", re.IGNORECASE)),
    ("desktop", re.compile(r"\b(?:UDIMM|DIMM|desktop)\b", re.IGNORECASE)),
)

# For graphics cards and processors the model *is* the spec, and it is highly
# regular. Normalized to a lowercase key so "RTX 4080 SUPER", "rtx-4080 super"
# and "RTX4080 Super" collapse together.
# The [^\w\s]* tolerates a trademark symbol between the family and the number.
# "ROG Strix GeForce RTX(tm) 4080" stored model_key as NULL because of that one
# character, which then passed the "unstated" comp filter and put a $1,200
# RTX 4080 into the comp set for an RTX 5070 Ti asking $49.99. One symbol
# silently disabling the most selective filter in the pipeline, on 18 listings.
_GPU_MODEL_RE = re.compile(
    r"\b(RTX|GTX|RX)[^\w\s]*\s*-?\s*(\d{3,4})\s*(Ti\s*SUPER|Ti|XTX|XT|SUPER)?\b",
    re.IGNORECASE,
)
_CPU_MODEL_RE = re.compile(
    r"\b(?:Ryzen\s+\d\s+)?(\d{4,5}[A-Z]{0,3})\b(?=.*\b(?:Ryzen|Core|i[3579]|Threadripper)\b)"
    r"|\b(i[3579])[\s-](\d{4,5}[A-Z]{0,2})\b",
    re.IGNORECASE,
)


# "no GPU" means two opposite things depending on what is being sold. On a
# graphics-card listing it means the box is empty and this is not the product.
# On a computer it means a working machine sold without a card, which is very
# much the product: real examples flagged wrongly by an earlier version include
# a $4,854 HP Z8 workstation and a $999 Ryzen barebones build. Whole-machine
# words are the discriminator.
_WHOLE_MACHINE_RE = re.compile(
    r"\b(?:pc|computer|workstation|desktop|barebones|tower|server|laptop|"
    r"notebook|rig|build|system)\b",
    re.IGNORECASE,
)


# eBay already classifies accessories, and its taxonomy is far more reliable
# than any title vocabulary. "Alphacool Eisblock Aurora Acryl GPX-N RTX 4090"
# is unrecognisable as a cooler from its title unless you know Alphacool makes
# coolers, and eBay files it under "Water Cooling" without being asked.
#
# Substrings rather than exact names so this keeps working as saved searches
# broaden: eBay has a parallel accessory category for essentially every
# product type ("Cases, Covers & Skins", "Chargers & Charging Docks", "Cell
# Phone & Smartphone Parts"), and they follow these naming patterns.
_ACCESSORY_CATEGORY_TOKENS = (
    "cooling",
    "parts",
    "accessor",
    "cases, covers",
    "covers & skins",
    "charger",
    "attachment",
    "cable",
    "adapter",
    "mount",
    "bracket",
    "tools",
)
# eBay's own name for a multi-item listing.
_LOT_CATEGORY_TOKENS = ("mixed lots", "bulk lots")

# --- multi-variant listings -------------------------------------------------
#
# One eBay listing offering several configurations shows the price of the
# CHEAPEST one, so its price and its title do not describe the same item. That
# manufactures fake bargains: an "iPhone 14 128GB 256GB - All Colors" at
# $259.99 against a $650 estimate is not a deal, it is the 128GB entry price
# beside a title mentioning bigger variants. 860 listings (6.4%) are like this,
# and they surface at the top of a deal ranking because that is where the
# largest apparent discounts live.
_VARIANT_WORDS_RE = re.compile(
    r"\ball\s+colou?rs?\b"
    r"|\b(?:choose|pick|select)\b[^,]{0,24}\b(?:colou?r|storage|capacity|size|model|variant)\b"
    r"|\byour\s+choice\b|\ball\s+sizes\b|\bmultiple\s+(?:sizes|colou?rs|options)\b",
    re.IGNORECASE,
)

# Categories where one listing legitimately names several DIFFERENT components'
# capacities: "Gaming PC 16GB RAM 512GB SSD RTX 4060 8GB" is three numbers and
# one machine. Counting capacities there flagged 191 real listings as variants.
_MULTI_COMPONENT_CATEGORIES = (
    "pc desktops",
    "laptops",
    "netbooks",
    "computer servers",
    "motherboard & cpu",
    "all-in-ones",
)
# Categories with exactly one capacity dimension, where two capacities in one
# title can only mean two variants on offer.
_SINGLE_CAPACITY_CATEGORIES = ("cell phones", "smartphones", "consoles", "tablets")

# Three or more distinct capacities is a variant list anywhere except a
# multi-component machine. Two is only conclusive where there is one capacity
# dimension: "Intel 32GB/1024GB NVMe SSD" is a hybrid Optane drive, not a
# choice between 32GB and 1TB.
MIN_CAPACITIES_FOR_VARIANT_LIST = 3


def _distinct_capacities(title: str) -> set[int]:
    found: set[int] = set()
    for match in _CAPACITY_RE.finditer(title):
        try:
            value = int(match.group(1))
        except ValueError:  # pragma: no cover - regex captures digits only
            continue
        if match.group(2).upper() == "TB":
            value *= 1024
        if 0 < value <= MAX_PLAUSIBLE_CAPACITY_GB:
            found.add(value)
    return found


def _is_multi_variant(title: str, category: str | None) -> tuple[bool, str | None]:
    """Does one listing offer several configurations at a from-price?"""
    words = _VARIANT_WORDS_RE.search(title)
    if words:
        return True, words.group(0).strip()

    lowered = (category or "").lower()
    if any(token in lowered for token in _MULTI_COMPONENT_CATEGORIES):
        return False, None

    capacities = _distinct_capacities(title)
    if len(capacities) >= MIN_CAPACITIES_FOR_VARIANT_LIST:
        return True, f"{len(capacities)} capacities listed"
    if len(capacities) == 2 and any(t in lowered for t in _SINGLE_CAPACITY_CATEGORIES):
        return True, f"{len(capacities)} capacities in a single-capacity category"
    return False, None


def _category_says_accessory(category: str | None) -> str | None:
    if not category:
        return None
    lowered = category.lower()
    for token in _ACCESSORY_CATEGORY_TOKENS:
        if token in lowered:
            return f"category:{category}"
    return None


def _is_accessory(title: str, category: str | None = None) -> tuple[bool, str | None]:
    """Is this a part FOR the product rather than the product?"""
    from_category = _category_says_accessory(category)
    if from_category:
        return True, from_category

    not_product = _NOT_THE_PRODUCT_RE.search(title)
    # The category is checked as well as the title because sellers of complete
    # machines often do not say so in words: "HP Z8 G4 W10 GOLD 5120 14C
    # 2.2GHZ 256GB 16TB SATA 512GB NVME NO GPU" is a $4,854 workstation whose
    # title contains no whole-machine word at all, and only its category
    # ("PC Desktops & All-In-Ones") reveals what it is.
    context = f"{title} {category or ''}"
    if not_product and not _WHOLE_MACHINE_RE.search(context):
        return True, not_product.group(0).strip()

    only = _ANY_ACCESSORY_ONLY_RE.search(title) or _ACCESSORY_ONLY_RE.search(title)
    if only:
        return True, only.group(0).strip()

    # A strong accessory noun beside a named model: the title says both what
    # product it relates to and that it is a part of it. No gate word needed,
    # and requiring one missed most real cases.
    #
    # Unless the noun is INCLUDED rather than offered. "RTX 5090 Founders
    # Edition With EKWB Waterblock" is a $4,999 graphics card that comes with
    # a waterblock; "GPU Cooling Fan" is a fan. The preceding word decides,
    # and getting this backwards flagged real $1,800-$5,000 cards as parts.
    if _model_key(title) or _FOR_RE.search(title):
        for strong in _STRONG_ACCESSORY_RE.finditer(title):
            # 32 rather than 22: sellers put a brand between the joiner and
            # the noun, and "GPU with EK Quantum Vector Water Block" needs 23
            # characters of context to see the "with" at all.
            preceding = title[max(0, strong.start() - 32) : strong.start()]
            if not _INCLUSION_PREFIX_RE.search(preceding):
                return True, strong.group(0).strip()

    noun = _ACCESSORY_NOUN_RE.search(title)
    if noun and _FOR_RE.search(title):
        return True, noun.group(0).strip()

    subject = _SUBJECT_ACCESSORY_RE.search(title)
    if subject and _FOR_RE.search(title):
        return True, subject.group(0).strip()

    return False, None


def _capacity_gb(title: str) -> int | None:
    """Largest plausible capacity in the title, normalized to GB."""
    best: int | None = None
    for match in _CAPACITY_RE.finditer(title):
        try:
            value = int(match.group(1))
        except ValueError:  # pragma: no cover - regex captures digits only
            continue
        if match.group(2).upper() == "TB":
            value *= 1024
        if value > MAX_PLAUSIBLE_CAPACITY_GB or value <= 0:
            continue
        best = value if best is None else max(best, value)
    return best


def _generation(title: str) -> str | None:
    match = _GENERATION_RE.search(title)
    if not match:
        return None
    if match.group(1):
        return match.group(1).upper()
    return f"PCIE{match.group(2) or match.group(3)}"


def _form_factor(title: str) -> str | None:
    """Form factor, but only for the things that have one.

    Gated on memory/storage context because every one of these words means
    something else elsewhere: a "2.5 inch heel" is not a 2.5-inch drive, and
    "Case Logic Laptop Backpack" is a bag, not laptop memory. Neither would
    break anything (the comp filter keeps unstated rows either way), but both
    are wrong, and they only appear once the saved searches move beyond PC
    parts. See docs/decisions/0013-spec-extraction.md.
    """
    if not _COMPONENT_CONTEXT_RE.search(title):
        return None
    # Ordered most-specific first: an ECC server module is also a DIMM, and
    # "server" is the more informative answer.
    for name, pattern in _FORM_FACTOR_RE:
        if pattern.search(title):
            return name
    return None


def _model_key(title: str) -> str | None:
    """A normalized chipset identifier, where the model is the spec.

    Graphics cards group cleanly by this: rtx-4090 medians $2,775 against
    rtx-4070-super at $652. Returns None outside the categories the vocabulary
    covers, which is most of the corpus and is the honest answer.
    """
    gpu = _GPU_MODEL_RE.search(title)
    if gpu:
        family, number, suffix = gpu.group(1), gpu.group(2), gpu.group(3)
        parts = [family.lower(), number]
        if suffix:
            parts.append(re.sub(r"\s+", "", suffix).lower())
        return "-".join(parts)
    return None


@dataclass
class Variant:
    """What a listing is actually offering, as far as its title admits.

    completeness is None when the title says nothing, which is the common case
    and means *unstated*. It must never be read as "complete": treating silence
    as a bundle is exactly the error that puts bare units in a comp set for
    complete ones.
    """

    lot_size: int | None = None
    completeness: str | None = None
    has_defect: bool = False
    # A part or accessory FOR the product, not the product. Matches on model
    # string and on image, so neither epid nor CLIP rejects it, while sitting
    # at 2-20% of the real price. See docs/decisions/0013-spec-extraction.md.
    is_accessory: bool = False
    # One listing offering several configurations shows the CHEAPEST one's
    # price, so price and title describe different items. Excluded from comps
    # and from deal scanning, since it manufactures fake bargains.
    price_is_from: bool = False
    capacity_gb: int | None = None
    spec_generation: str | None = None
    form_factor: str | None = None
    model_key: str | None = None
    signals: dict = field(default_factory=dict)

    @property
    def is_lot(self) -> bool:
        return self.lot_size is not None and self.lot_size >= MIN_LOT_SIZE

    @property
    def usable_as_comp(self) -> bool:
        """Whether this listing can stand as a comparable at all.

        Filters, not weights, and the distinction is the point (same reasoning
        as 0007's split): a lot of 50, a for-parts unit and a replacement
        heatsink are not noisy measurements of a working single item's value,
        they are measurements of something else. Averaging them in produces a
        number describing neither.
        """
        return (
            not self.is_lot
            and not self.has_defect
            and not self.is_accessory
            and not self.price_is_from
        )


def _lot_size(title: str) -> tuple[int | None, str | None]:
    match = _LOT_COUNT_RE.search(title)
    if match:
        raw = next(g for g in match.groups() if g)
        try:
            size = int(raw)
        except ValueError:  # pragma: no cover - the regex only captures digits
            return None, None
        if size >= MIN_LOT_SIZE:
            return size, match.group(0).strip()
        return None, None

    # "Bulk lot of RAM" with no number: known to be a lot, size unknown. Report
    # it as a lot at the minimum size rather than as a single item, since the
    # exclusion matters far more than the count does.
    word = _LOT_WORD_RE.search(title)
    if word:
        return MIN_LOT_SIZE, word.group(0).strip()
    return None, None


def _completeness(title: str) -> tuple[str | None, dict]:
    signals: dict = {}

    bare = _BARE_RE.search(title) or _MISSING_COMPONENT_RE.search(title)
    bundle = _BUNDLE_RE.search(title)
    includes = _INCLUDES_RE.search(title) or _PLUS_RE.search(title)
    accessories = {m.group(0).lower().replace("-", " ") for m in _ACCESSORY_RE.finditer(title)}

    if bare:
        signals["bare_match"] = bare.group(0).strip()
    if bundle:
        signals["bundle_match"] = bundle.group(0).strip()
    if includes:
        signals["includes_match"] = includes.group(0).strip()
    if accessories:
        signals["accessories"] = sorted(accessories)[:6]

    # "Console only, no charger" alongside "bundle" is contradictory; the
    # explicit bare marker wins because it is the more specific claim and the
    # one a seller is least likely to write by accident.
    if bare:
        return BARE, signals
    if bundle:
        return BUNDLE, signals
    if includes:
        return COMPLETE, signals
    if len(accessories) >= MIN_IMPLIED_ACCESSORIES:
        # No joining word, but the title enumerates several accessories, which
        # is how a lot of sellers describe a full box.
        signals["implied_by_accessory_count"] = True
        return COMPLETE, signals
    return None, signals


def extract_variant(
    title: str, aspects: dict | None = None, category: str | None = None
) -> Variant:
    """Classify a listing from its title.

    aspects is consulted as a fallback for capacity, which eBay carries on 99%
    of phones and 0.3% of graphics cards. category disambiguates "no GPU",
    which means an empty box on a graphics-card listing and a working machine
    on a workstation."""
    title = title or ""
    signals: dict = {}

    lot_size, lot_match = _lot_size(title)
    if lot_size is None and category and any(
        token in category.lower() for token in _LOT_CATEGORY_TOKENS
    ):
        # eBay's own "Mixed Lots" category, which says outright what a title
        # sometimes does not.
        lot_size, lot_match = MIN_LOT_SIZE, f"category:{category}"
    if lot_match:
        signals["lot_match"] = lot_match

    completeness, completeness_signals = _completeness(title)
    signals.update(completeness_signals)

    defect = _DEFECT_RE.search(title)
    if defect:
        signals["defect_match"] = defect.group(0).strip()

    if _READ_RE.search(title):
        # A caveat exists, unspecified. Recorded, not acted on.
        signals["seller_flagged_read"] = True

    negations = [m.group(0).strip() for m in _NEGATION_RE.finditer(title)]
    if negations:
        signals["negations"] = negations[:4]

    if _SEALED_RE.search(title) or (aspects or {}).get("Condition", "").lower() in {"new", "sealed"}:
        signals["sealed"] = True

    accessory, accessory_match = _is_accessory(title, category)
    if accessory_match:
        signals["accessory_match"] = accessory_match

    price_is_from, variant_match = _is_multi_variant(title, category)
    if variant_match:
        signals["multi_variant_match"] = variant_match

    capacity = _capacity_gb(title)
    if capacity is None:
        # eBay's structured aspects carry capacity reliably for consumer goods
        # and almost never for components (99% on phones, 0.3% on graphics
        # cards), so this is a fallback rather than the primary source.
        for key in ("Storage Capacity", "Capacity", "RAM Size", "Total Capacity"):
            raw = (aspects or {}).get(key)
            if raw:
                capacity = _capacity_gb(str(raw))
                if capacity is not None:
                    signals["capacity_from_aspects"] = key
                    break
    if capacity is not None:
        signals["capacity_gb"] = capacity

    generation = _generation(title)
    if generation:
        signals["generation"] = generation

    form_factor = _form_factor(title)
    if form_factor:
        signals["form_factor"] = form_factor

    model_key = _model_key(title)
    if model_key:
        signals["model_key"] = model_key

    return Variant(
        lot_size=lot_size,
        completeness=completeness,
        has_defect=bool(defect),
        is_accessory=accessory,
        price_is_from=price_is_from,
        capacity_gb=capacity,
        spec_generation=generation,
        form_factor=form_factor,
        model_key=model_key,
        signals=signals,
    )
