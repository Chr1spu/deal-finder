"""Variant extraction: what is actually being sold.

Cases are real titles from the corpus wherever possible, because the bug that
made this necessary in the first place was a hand-built fixture agreeing with
a hand-built assumption (see ADR 0011). Several of these were genuine false
positives found only by running the extractor across all 12,678 listings.
"""

import pytest

from ml.extract import BARE, BUNDLE, COMPLETE, extract_variant

# ------------------------------------------------------------------- lots


@pytest.mark.parametrize(
    "title,size",
    [
        ("Lot of 50 SK Hynix 64GB- 2Rx8 PC5-5600B-RA0-1010-XT", 50),
        ("LOT OF 10 - HP Z2 G9 Mini Workstation i9-14900K", 10),
        ("*LOT OF 2* EVGA GeForce RTX 3090 FTW3 Ultra 24GB", 2),
        ("5-PACK - HEATSINKS/SHROUDS from MSI Gaming RTX 5090", 5),
        ("Pack of 4 Corsair Vengeance DDR4", 4),
    ],
)
def test_explicit_lots_are_detected_with_their_size(title, size):
    variant = extract_variant(title)
    assert variant.lot_size == size
    assert variant.is_lot is True
    assert variant.usable_as_comp is False


def test_a_lot_without_a_number_is_still_a_lot():
    """Exclusion matters far more than the count does."""
    variant = extract_variant("Nintendo Switch Lot (3): Mario Party, Luigi's Mansion")
    assert variant.is_lot is True
    assert variant.usable_as_comp is False


@pytest.mark.parametrize(
    "title",
    [
        # Every one of these was a real false positive from an earlier version
        # that accepted "Nx" / "xN" quantity forms. In PC hardware an x beside
        # a number is almost never a count, and those patterns misfired on
        # 1,789 listings (14.1% of the corpus).
        "PowerColor Hellhound AMD Radeon RX 7900 XTX OC 24GB GDDR6",
        "Micron 2280mm 2400 1TB M.2 NVMe Gen 4.0 x 4 SSD",
        "Crucial P310 4TB Gen4 x4 M.2 2280 NVMe SSD",
        "MSI NVIDIA GeForce RTX 3080 VENTUS 3X PLUS 10GB GDDR6X",
        "AMD Ryzen 5 7600X R5 7600X AM5 CPU Processor 4.7 GHz",
        "Dell NVIDIA GeForce RTX 3080 10GB GDDR6X PCIe 4.0 x16",
        "Lot of 2  16gb GB DDR4-2666S Laptop RAM (32 GB)",  # genuine, sanity anchor
    ][:-1],
)
def test_model_numbers_are_not_mistaken_for_quantities(title):
    assert extract_variant(title).is_lot is False


def test_a_lot_of_one_is_not_a_lot():
    """Excluding it would lose a real comp for no reason."""
    assert extract_variant("Lot of 1 Nintendo Switch").is_lot is False


# --------------------------------------------------------------- defects


@pytest.mark.parametrize(
    "title",
    [
        "RTX 3080 Ti FOR PARTS not working no power",
        "LOT OF 12 SK Hynix 64GB DDR5 ECC RDIMM Server Ram - DAMAGED/AS-IS",
        "Nintendo Switch OLED - Fair Used Cond - Chipped Plastic - Works READ",
        "GeForce RTX 3090 - cracked PCB, spares or repair",
        "iPhone 13 broken screen as-is",
    ],
)
def test_defects_are_detected_and_excluded(title):
    variant = extract_variant(title)
    assert variant.has_defect is True
    assert variant.usable_as_comp is False


def test_read_is_recorded_as_a_signal_but_is_not_a_defect():
    """A seller writing READ is flagging that something is unusual without
    saying what. Recording it is useful; treating it as damage is a guess."""
    variant = extract_variant("Nintendo Switch OLED Console Bundle - White READ")
    assert variant.signals.get("seller_flagged_read") is True
    assert variant.has_defect is False
    assert variant.usable_as_comp is True


# ---------------------------------------------------------- completeness


def test_bare_units_are_detected():
    for title in (
        "Nintendo Switch OLED Model HEG-001 64GB Console - Console ONLY",
        "Nintendo Switch OLED Model HEG-001 Tablet Only 64GB",
        "Nintendo - Switch OLED Handheld Console - 64GB Model: HEG-001 Tablet ONLY",
    ):
        assert extract_variant(title).completeness == BARE


def test_bundles_are_detected():
    variant = extract_variant("Nintendo Switch OLED Console Bundle - 64GB W/256GB SD Card")
    assert variant.completeness == BUNDLE


@pytest.mark.parametrize(
    "title",
    [
        "Nintendo Switch OLED HEG-001 Console w/ White Joy-Cons",
        "Nintendo Switch HEG-001 OLED 64GB Handheld Console - White + 256GB SD Card",
        "Nintendo Switch OLED Model HEG-001 Tablet Charge And Dock And HDMI Cord",
    ],
)
def test_listings_that_enumerate_contents_are_complete(title):
    assert extract_variant(title).completeness == COMPLETE


@pytest.mark.parametrize(
    "title",
    [
        # The false positive that an earlier version produced: "with" followed
        # by any digit was accepted, so "4K monitors" read as an accessory.
        "Gaming PC works with 4K monitors",
        "iPhone 13 - local pickup only",
        "Apple iPhone 15 Pro 256GB Black Smartphone Triple Camera",
        "AMD Ryzen 9 7900X 12-Core Processor",
    ],
)
def test_titles_that_state_nothing_stay_unstated(title):
    """None means UNSTATED and must never be read as 'complete'. 89% of the
    corpus lands here, so a wrong default would mis-file most of it."""
    assert extract_variant(title).completeness is None


def test_bare_wins_over_a_contradictory_bundle_marker():
    """'Console only' is the more specific claim and the one a seller is least
    likely to write by accident."""
    variant = extract_variant("Switch Console Only - bundle box included, no joy-cons")
    assert variant.completeness == BARE


# ------------------------------------------------------- comp eligibility


def test_an_ordinary_listing_is_usable_as_a_comp():
    variant = extract_variant("Nintendo Switch OLED Model HEG-001 64GB White")
    assert variant.usable_as_comp is True
    assert variant.lot_size is None
    assert variant.has_defect is False


def test_signals_record_why_a_classification_happened():
    """Nothing should be opaque: a listing classified oddly has to be traceable
    to the token that did it, which is what makes the rules maintainable."""
    variant = extract_variant("LOT OF 10 - SK hynix 16GB DDR4 cracked")
    assert "lot_match" in variant.signals
    assert "defect_match" in variant.signals
    assert variant.signals["defect_match"].lower() == "cracked"


def test_an_empty_title_does_not_crash():
    variant = extract_variant("")
    assert variant.usable_as_comp is True
    assert variant.completeness is None


# ------------------------------------- accessories pretending to be products


@pytest.mark.parametrize(
    "title",
    [
        # All real titles from the rtx-3090 group, whose 1428x price spread
        # was caused entirely by these. They match on model string AND on
        # image, so neither epid nor CLIP rejects them.
        "NVIDA GeForce RTX3090 Quick start guide and support guide books ONLY",
        "Backplate Replacement for ASUS ROG STRIX RTX 3090 GAMING White OC",
        "Aorus RTX 3090 Extreme original box include inside box . No graphics card",
        "ASUS TUF RTX 3090 3-Fan Heatsink Cooler Assembly GPU Replacement Used",
        "NVIDIA NVLink Bridge 2-Slot 900-53651 for RTX 3090 A5000 A5500",
        "CASE for GIGABYTE AORUS RTX 3090 AI Box GV-N3090IXEB-32GD",
        "EK-Quantum Vector N+ Water block + Backplate for RTX 3090",
        "(FAN AND HEAT SINK ONLY) MSI GeForce RTX 5090 32G VENTUS 3X OC",
    ],
)
def test_accessories_are_flagged_and_excluded(title):
    variant = extract_variant(title)
    assert variant.is_accessory is True
    assert variant.usable_as_comp is False


@pytest.mark.parametrize(
    "title",
    [
        # The precision risk: legitimate listings that mention an accessory.
        "EVGA GeForce RTX 3090 24GB GDDR6X Graphics Card (24G-P5-3987-KR)",
        "MSI GeForce RTX 4080 SUPER 16GB GDDR6X Gaming X Slim",
        "Nintendo Switch OLED Console w/ dock and cables",
        "Corsair Vengeance 32GB DDR5 6000MHz Desktop Memory",
    ],
)
def test_real_products_are_not_mistaken_for_accessories(title):
    assert extract_variant(title).is_accessory is False


def test_non_functional_hardware_counts_as_a_defect():
    """Found on RTX 3090 listings sitting at half price inside an otherwise
    clean model group: intact hardware that does not work."""
    for title in (
        "ZOTAC GeForce RTX 3090 Trinity OC 24GB Graphics Card NO DISPLAY",
        "Gigabyte GeForce RTX 3090 VISION OC 24GB - No Display Output",
        "RTX 4070 does not post",
    ):
        variant = extract_variant(title)
        assert variant.has_defect is True
        assert variant.usable_as_comp is False


# ------------------------------------------------------------------- specs


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Corsair Vengeance 32GB (2x16GB) DDR5 6000MHz", 32),
        ("Crucial P3 1TB M.2 NVMe Gen 3.0 x 4 SSD", 1024),
        ("Apple iPhone 15 Pro 256GB Fully Unlocked", 256),
        ("Transcend 4TB M2 PCIe NVMe Gen 4 SSD", 4096),
        # Largest wins: titles list several and the biggest is usually the item.
        ("Gaming PC 64GB RAM 2TB SSD RTX 4070", 2048),
    ],
)
def test_capacity_is_normalized_to_gigabytes(title, expected):
    assert extract_variant(title).capacity_gb == expected


def test_capacity_is_absent_when_the_title_says_nothing():
    assert extract_variant("AMD Ryzen 9 7900X 12-Core Processor").capacity_gb is None


def test_capacity_falls_back_to_aspects():
    """eBay's aspects carry capacity on 99% of phones and 0.3% of graphics
    cards, so this is a fallback, not the primary source."""
    variant = extract_variant("Apple iPhone 15 Pro Unlocked", {"Storage Capacity": "512 GB"})
    assert variant.capacity_gb == 512
    assert variant.signals.get("capacity_from_aspects") == "Storage Capacity"


# --------------------------------------------------------------- memory kits


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Corsair Vengeance 2x16GB DDR5 6000MHz", 32),
        ("G.Skill Trident Z5 DDR5 4 x 8GB", 32),
        ("Crucial 16GB x 2 DDR4 SODIMM Laptop Memory", 32),
        ("Kingston Fury 2 x 32GB DDR5 RAM", 64),
        # The pair that motivated the rule: one product, two spellings, and
        # before this they landed in different capacity buckets (32 and 16).
        ("Corsair Vengeance 32GB (2x16GB) DDR5 6000MHz", 32),
        ("Corsair Vengeance 2x16GB DDR5 6000MHz CL30 memory", 32),
    ],
)
def test_a_kit_is_priced_as_its_total_capacity(title, expected):
    """A 2x16GB kit is 32GB of memory. Reading it as 16 put it among single
    sticks at half the price, which is the suspected source of DDR5 desktop
    memory keeping a 19.1x spread after every other filter."""
    assert extract_variant(title).capacity_gb == expected


def test_a_kit_is_not_a_lot():
    """The distinction the whole rule rests on. A lot is several items that
    could be sold separately and is excluded from comps; a matched kit is one
    product, and stays a usable comparable."""
    variant = extract_variant("Corsair Vengeance 2x16GB DDR5 6000MHz")

    assert variant.is_lot is False
    assert variant.usable_as_comp is True
    assert variant.signals["kit_modules"] == 2
    assert variant.signals["kit_total_gb"] == 32


def test_module_count_is_recorded_for_titles_that_state_a_total():
    """Both spellings agree on capacity now, so the module count is what still
    distinguishes a 2x16GB kit from a single 32GB stick."""
    variant = extract_variant("Corsair Vengeance 32GB (2x16GB) DDR5")

    assert variant.capacity_gb == 32
    assert variant.signals["kit_modules"] == 2


def test_a_single_module_is_not_a_kit():
    """"1x8GB" is one stick written oddly, the same call MIN_LOT_SIZE makes.

    Its capacity stays unstated, because _CAPACITY_RE needs a word boundary
    before the number and "x8GB" has none. That predates this rule and is left
    alone deliberately: widening the capacity pattern is a corpus-wide change
    and this one is not.
    """
    variant = extract_variant("Samsung 1x8GB DDR4 2666 DIMM")

    assert "kit_modules" not in variant.signals
    assert variant.capacity_gb is None


def test_a_plainly_written_single_stick_is_unaffected():
    variant = extract_variant("Samsung 8GB DDR4 2666 DIMM")

    assert variant.capacity_gb == 8
    assert "kit_modules" not in variant.signals


def test_two_graphics_cards_are_not_a_memory_kit():
    """The gate that makes the rule safe. "24GB x 2" is two cards, and
    totalling it to 48 would file the listing under a capacity no card has,
    leaving it with no comps at all rather than merely mispriced."""
    variant = extract_variant("MSI RTX 3090 24GB x 2 SLI pair")

    assert variant.capacity_gb == 24
    assert "kit_modules" not in variant.signals


@pytest.mark.parametrize(
    "title,expected",
    [
        # The same titles that killed the generic "Nx" quantity forms. A
        # capacity unit right after the second number is what separates a kit
        # from a PCIe lane count, a product line and a CPU model.
        ("Dell NVIDIA GeForce RTX 3080 10GB GDDR6X PCIe 4.0 x16", 10),
        ("Crucial P310 4TB Gen4 x4 M.2 2280 NVMe SSD", 4096),
        ("Micron 2280mm 2400 1TB M.2 NVMe Gen 4.0 x 4 SSD", 1024),
        ("MSI NVIDIA GeForce RTX 3080 VENTUS 3X PLUS 10GB GDDR6X", 10),
        ("PowerColor Hellhound AMD Radeon RX 7900 XTX OC 24GB GDDR6", 24),
    ],
)
def test_lane_counts_and_model_names_are_not_kits(title, expected):
    assert extract_variant(title).capacity_gb == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Corsair Vengeance 32GB DDR5 6000MHz", "DDR5"),
        ("SK hynix 32GB DDR4 3200 ECC RDIMM", "DDR4"),
        ("Crucial P3 1TB M.2 NVMe Gen 3.0 x 4 SSD", "PCIE3"),
        ("Samsung 990 PRO PCIe 4.0 NVMe SSD", "PCIE4"),
    ],
)
def test_generation_is_extracted(title, expected):
    assert extract_variant(title).spec_generation == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("SK hynix 32GB DDR4 3200 ECC RDIMM Server RAM", "server"),
        ("Crucial 32GB DDR4 SODIMM Laptop Memory", "laptop"),
        ("Corsair Vengeance 32GB DDR5 DIMM Desktop", "desktop"),
        ("Crucial P3 1TB M.2 2280 NVMe SSD", "m.2"),
    ],
)
def test_form_factor_is_extracted(title, expected):
    """32GB DDR4 laptop memory medians $110.99 against $149.99 desktop, so
    this separates real price tiers rather than cosmetic ones."""
    assert extract_variant(title).form_factor == expected


@pytest.mark.parametrize(
    "title,expected",
    [
        ("MSI GeForce RTX 4080 SUPER 16GB Gaming X Slim", "rtx-4080-super"),
        ("EVGA GeForce RTX 3090 24GB GDDR6X", "rtx-3090"),
        ("PowerColor Radeon RX 6800 XT 16GB Red Dragon", "rx-6800-xt"),
        ("ASRock Radeon RX 7900 XTX Phantom", "rx-7900-xtx"),
        ("Zotac GeForce GTX 1660 Ti 6GB", "gtx-1660-ti"),
        ("MSI GeForce RTX4070 Ventus 3X OC 12GB", "rtx-4070"),
    ],
)
def test_gpu_model_key_normalizes_spelling_variants(title, expected):
    """Where the model IS the spec. Grouping by this gives a clean price
    ladder: rtx-4090 $2,775 down to rtx-4070-super $652."""
    assert extract_variant(title).model_key == expected


def test_model_key_is_none_outside_the_covered_categories():
    """Most of the corpus, and the honest answer rather than a guess."""
    assert extract_variant("Corsair Vengeance 32GB DDR5 Desktop Memory").model_key is None


# ------------------------------------------- categories the corpus lacks yet
#
# The extraction vocabulary was written against a corpus of PC parts, consoles
# and phones. These tests exist because the rules must degrade to "unstated"
# on unfamiliar categories rather than produce confident wrong answers: a
# false lot or a false defect EXCLUDES a listing from comps entirely, which is
# far worse than declining to classify it.


@pytest.mark.parametrize(
    "title",
    [
        "Nike Air Max 90 White Size 10 Mens Sneakers",
        "Vintage Levis 501 Denim Jacket Large - great condition",
        "Herman Miller Aeron Chair Size B - local pickup",
        "LEGO Star Wars Millennium Falcon 75192 - sealed",
        "Rolex Submariner 116610LN Box and Papers",
        "Persian Rug 5x8 ft Hand Knotted",
    ],
)
def test_unfamiliar_categories_are_left_alone(title):
    """No flags, no specs, and above all still usable as a comp."""
    variant = extract_variant(title)
    assert variant.usable_as_comp is True
    assert variant.is_lot is False
    assert variant.has_defect is False
    assert variant.is_accessory is False


@pytest.mark.parametrize(
    "title",
    [
        # Idiomatic English that happens to contain "lot". Rare in PC-parts
        # titles, common in clothing and furniture, and it used to classify
        # both of these as two-item lots and drop them from every comp set.
        "Vintage Leather Jacket - lots of character, great patina",
        "Antique Oak Dresser - a lot of storage space",
    ],
)
def test_the_english_quantifier_is_not_a_lot(title):
    assert extract_variant(title).is_lot is False


@pytest.mark.parametrize(
    "title,size",
    [
        ("Lot of 12 Vintage Vinyl Records Rock 70s", 12),
        ("Nintendo Switch Lot (3): Mario Party, Luigi's Mansion", 3),
        ("Bulk lot of DDR4 memory", 2),
    ],
)
def test_genuine_lots_survive_the_quantifier_fix(title, size):
    assert extract_variant(title).lot_size == size


@pytest.mark.parametrize(
    "title",
    [
        # "2.5 inch" is a drive size and also a heel height; "laptop" is a form
        # factor and also half of a brand name. Both were firing on the wrong
        # one until form factor was gated on memory/storage context.
        "Womens Black Heels 2.5 inch Size 7 Leather",
        "Case Logic Laptop Backpack Black",
    ],
)
def test_form_factor_does_not_fire_outside_components(title):
    assert extract_variant(title).form_factor is None


@pytest.mark.parametrize(
    "title,expected",
    [
        # Same words, real components. A quote mark after 2.5 used to defeat a
        # trailing word boundary, so `2.5" SATA` was silently missed while
        # `2.5 inch` worked.
        ('Samsung 870 EVO 2TB 2.5" SATA SSD', "2.5in"),
        ("Seagate 2TB 2.5 inch SATA Hard Drive", "2.5in"),
        ("Crucial 32GB DDR4 SODIMM Laptop Memory", "laptop"),
    ],
)
def test_form_factor_still_fires_on_real_components(title, expected):
    assert extract_variant(title).form_factor == expected


def test_generic_signals_transfer_to_unfamiliar_categories():
    """The vocabulary that is genuinely category-independent should keep
    working: "body only" is real camera terminology, "for parts" is universal,
    and a numbered lot is a lot whatever it contains."""
    assert extract_variant("Canon EOS R6 Mirrorless Camera Body Only").completeness == BARE
    assert extract_variant("Dyson V11 Vacuum - no battery, unit only").completeness == BARE
    assert extract_variant("Fender Stratocaster - cracked neck for parts").has_defect is True
    assert extract_variant("Lot of 12 Vintage Vinyl Records").is_lot is True

# --------------------------------------- stripped boards and missing cores


@pytest.mark.parametrize(
    "title",
    [
        # All real titles, all led the deal feed at 96-97% discounts against
        # complete cards. A bare board names the model it came from, so it
        # matches on model string and photo alike and neither epid nor CLIP
        # rejects it. The vocabulary had "pcb only" but sellers write "PCB
        # Board For X" far more often.
        "PCB Board For ZOTAC GAMING GeForce RTX 4090 Trinity",
        "PCB For  TUF RTX4080SUPER 16G",
        "PCB Board For Asus Tuf RTX 4080 GAMING No Chip",
    ],
)
def test_stripped_boards_are_accessories(title):
    variant = extract_variant(title, None, "Graphics/Video Cards")
    assert variant.is_accessory is True
    assert variant.usable_as_comp is False


def test_a_component_missing_its_core_is_a_defect():
    """The other word order. The defect vocabulary reads "MISSING CORE";
    sellers equally write "GPU AND MEMORY MISSING", which matched nothing and
    ranked a stripped RTX 4090 at $74.97 against a $2,719 estimate."""
    variant = extract_variant(
        "GPU AND MEMORY MISSING - MSI RTX 4090 Gaming X Slim", None, "Graphics/Video Cards"
    )
    assert variant.has_defect is True
    assert variant.usable_as_comp is False


@pytest.mark.parametrize(
    "title,category",
    [
        # A MACHINE missing a component is a working reduced configuration,
        # not damage. Reading these as defects flagged real $1,800-$4,900
        # listings, which is the whole reason the rule is category-gated.
        ("HP Z8 G4 Workstation Xeon 64GB No GPU", "Desktops & All-In-Ones"),
        ("Dell Precision 7920 Tower - NO GPU, NO OS", "Desktops & All-In-Ones"),
        # And a real card that merely mentions a board or a box.
        ("MSI RTX 4090 Gaming X Trio with original box and PCB shroud", "Graphics/Video Cards"),
        ("EVGA RTX 3080 FTW3 Ultra with upgraded PCB thermal pads", "Graphics/Video Cards"),
        ("ASUS ROG Strix RTX 4090 OC 24GB Graphics Card", "Graphics/Video Cards"),
    ],
)
def test_real_items_survive_the_stripped_board_rules(title, category):
    variant = extract_variant(title, None, category)
    assert variant.usable_as_comp is True, variant.signals

# ------------------------- vocabulary the deal feed found, one gap at a time

GPU_CATEGORY = "Graphics/Video Cards"


@pytest.mark.parametrize(
    "title",
    [
        # Every one led the deal feed. The vocabulary already covered the same
        # claim in a different wording, which is the recurring shape of these
        # misses: sellers say one thing several ways and the regex knows one.
        # "not working" was covered, the adjective "Non-Functional" was not.
        "ASUS TUF Gaming OC GeForce RTX 4080 Super 16GB - GPU - Non-Functional Device",
        "RTX 5090 PALIT GameRock with box, PCB, RGB REMOTE & App control(non-functional!)",
        "NONFUNCTIONAL - BX8071514900K CoreTM i914900K Gaming Desktop Processor",
        # "spares or repair" was covered, "parts or repair" was not.
        "MSI GeForce RTX 3090 SUPRIM X 24G Graphics Card Only *PARTS OR REPAIR*",
        "NVIDIA GeForce RTX 3080 Graphics Card - VRAM Issue / Parts or Repair",
    ],
)
def test_defects_the_feed_surfaced(title):
    variant = extract_variant(title, None, GPU_CATEGORY)
    assert variant.has_defect is True
    assert variant.usable_as_comp is False


def test_a_trim_piece_is_an_accessory():
    """$28.50 against real cards. A part number plus a compatibility list is
    never the product, but the discriminator that generalises is the noun."""
    variant = extract_variant(
        "DH0011 For RX6600XT RX6700XT Magic Eagle RX6800 gaming graphics card bezel blank",
        None,
        GPU_CATEGORY,
    )
    assert variant.is_accessory is True
    assert variant.usable_as_comp is False


@pytest.mark.parametrize(
    "title,category",
    [
        ("MSI RTX 4090 Gaming X Trio fully functional, tested", GPU_CATEGORY),
        ("EVGA RTX 3080 FTW3 with RGB bezel and original box", GPU_CATEGORY),
        ("Apple iPhone 15 Pro Max 256GB Unlocked", "Cell Phones & Smartphones"),
    ],
)
def test_the_new_vocabulary_does_not_catch_real_items(title, category):
    """"fully functional" contains "functional", and a real card can mention
    its own bezel. Both were checked against the whole corpus, not just here:
    11 listings changed, all of them genuinely broken or parts."""
    assert extract_variant(title, None, category).usable_as_comp is True

# ------------------------- variant lists written with one shared unit


@pytest.mark.parametrize(
    "title",
    [
        # The exact titles that sat near the top of the deal feed on
        # 2026-08-10, priced at their cheapest variant.
        "Apple iPhone 5C UNlocked 8/16/32GB BLUE/GREEN/PINK/WHITE/ YELLOW - New",
        "Apple iPhone 17 Pro Max, 256/512GB - Unlocked - Refurbished",
        "Apple iPhone 15 Plus 128/256/512GB - Unlocked - Refurbished",
        "Apple iPhone Air 256/512GB/1TB - Unlocked - Used Premium",
    ],
)
def test_a_slash_list_sharing_one_unit_is_a_variant_list(title):
    """"8/16/32GB" is three capacities, not one.

    _CAPACITY_RE needs the unit adjacent to each number, so this read as the
    single capacity 32 and _is_multi_variant never saw a list to count. Same
    defect as the kit rule's, in a second place.
    """
    variant = extract_variant(title, None, "Cell Phones & Smartphones")
    assert variant.price_is_from is True
    assert variant.usable_as_comp is False


@pytest.mark.parametrize(
    "title,category",
    [
        # A product name beside a capacity, not a list of capacities. The
        # power-of-two guard is the only thing separating these from the
        # cases above, and without it both read as two-capacity listings.
        ("Apple iPhone 5 / 16GB Factory UNlocked Silver", "Cell Phones & Smartphones"),
        ("MSI GeForce RTX 5080 / 32GB Gaming Graphics Card", "Graphics/Video Cards"),
        (
            "Skytech Gaming Legacy 4 PC - Ryzen 9 9950x3d / RTX 5080 / 32GB / 2TB",
            "PC Desktops & All-In-Ones",
        ),
    ],
)
def test_a_model_number_beside_a_capacity_is_not_a_variant_list(title, category):
    assert extract_variant(title, None, category).price_is_from is False


def test_the_shared_unit_rule_reads_every_capacity_in_the_list():
    from ml.extract import _shared_unit_capacities

    assert _shared_unit_capacities("8/16/32GB") == {8, 16, 32}
    assert _shared_unit_capacities("256/512GB") == {256, 512}
    # 5 is not a power of two, so the whole expression is refused rather than
    # partially trusted.
    assert _shared_unit_capacities("iPhone 5 / 16GB") == set()
    assert _shared_unit_capacities("RTX 5080 / 32GB") == set()


# ------------------------------- colours offered, where a colour list can only
# ------------------------------- mean several items


def test_three_slash_joined_colours_on_a_phone_is_a_variant_list():
    variant = extract_variant(
        "Apple iPhone 5 / 16GB Factory UNlocked Black/White/Gold New Battery",
        None,
        "Cell Phones & Smartphones",
    )
    assert variant.price_is_from is True
    assert "colours offered" in str(variant.signals)


@pytest.mark.parametrize(
    "title,category",
    [
        # One console with multicoloured controllers, $486. Three colours, one
        # product, which is why the rule is restricted to phone categories.
        (
            "Nintendo Switch 2 Console Wi-Fi HDMI Black/Blue/Orange Joy-Cons",
            "Video Game Consoles",
        ),
        # Two-tone objects. 322 listings match at two colours, so two is not
        # enough anywhere.
        ("CyberPowerPC White/Black RGB Gaming Tower Desktop", "PC Desktops & All-In-Ones"),
        ("NZXT H510 Gaming Desktop Mid Tower White/Black Tempered Glass", "PC Desktops & All-In-Ones"),
        # One iPhone whose colour name happens to contain a slash.
        ("Apple iPhone 16 Pro 256GB AT&T Dessert Titanium / Gold", "Cell Phones & Smartphones"),
    ],
)
def test_a_multicoloured_product_is_not_a_variant_list(title, category):
    assert extract_variant(title, None, category).price_is_from is False


# --------------------------------- a machine's drives are not a memory kit


@pytest.mark.parametrize(
    "title,category,expected",
    [
        # Measured 2026-08-10: 15 of the 56 listings the kit rule changed were
        # whole machines, where _capacity_gb takes the max and the SSD total
        # outranks the RAM figure a buyer would compare.
        ("MSI Titan 18 HX Core i9-14900HX RTX 4090 128GB RAM 2x2TB SSD", "PC Laptops & Netbooks", 128),
        ("Razer Blade 16 rtx 5090 32GB Ram 2x2TB SSD Pristine", "PC Laptops & Netbooks", 32),
        ("HP Z8 G4 W10 PLATINUM 8156 4C 3.6GHZ 128GB 3 X 16TB SAS", "PC Desktops & All-In-Ones", 16384),
    ],
)
def test_a_machines_drives_are_not_totalled_as_a_kit(title, category, expected):
    """The kit totals would be 4096, 4096 and 49152.

    What comes back instead is the largest capacity the title states outright,
    which for a machine is whichever component the seller wrote largest. That
    is a weaker answer than a component listing gets and deliberately so: a
    machine has several capacities and no single one describes it, so the
    honest options are a plausible stated number or nothing. Inventing a total
    across its parts is the one answer that is definitely wrong.
    """
    assert extract_variant(title, None, category).capacity_gb == expected


def test_a_kit_is_still_totalled_for_actual_memory():
    variant = extract_variant("Corsair Vengeance 2x16GB DDR5 6000MHz", None, "Memory (RAM)")
    assert variant.capacity_gb == 32


# ------------------------------- eBay's own accessory categories, second pass


@pytest.mark.parametrize(
    "category",
    [
        "Bags, Skins & Travel Cases",
        "Original Game Cases & Boxes",
        "Memory Cards & Expansion Packs",
    ],
)
def test_accessory_categories_the_taxonomy_already_names(category):
    variant = extract_variant("Nintendo Switch OLED", None, category)
    assert variant.is_accessory is True
    assert variant.usable_as_comp is False


@pytest.mark.parametrize(
    "category",
    [
        # A game is a different product, not an accessory, and the hard
        # category filter in ml/similar.py already keeps it out of console
        # comp sets. Flagging it would only destroy the ability to value a
        # captured game.
        "Video Games",
        # Eight of ten listings here are genuine heatsinks, but two are real
        # graphics cards a seller filed in the wrong place. A rule that
        # excludes listings cannot run at a 20% false-positive rate.
        "CPU Fans & Heat Sinks",
        # eGPU enclosures are products in their own right.
        "Laptop Docking Stations",
        "Graphics/Video Cards",
    ],
)
def test_product_categories_are_not_treated_as_accessory_categories(category):
    assert extract_variant("Nintendo Switch OLED", None, category).is_accessory is False


# ------------------------------- cracks, and the word used to deny one


@pytest.mark.parametrize(
    "title",
    [
        # Real titles that reached the deal feed with has_defect false, because
        # `\bcracked?\b` binds the `?` to the `d` and never matched the bare
        # word sellers actually write.
        "iphone 15 Plus DOA 256GB Icloud Off | Crack Back Crack Front",
        "Apple iPhone 16 Pro Max - 256 GB - Black Titanium (Unlocked)**CRACK FRONT**",
        "Apple iPhone 15 Pro Max 256GB Black Unlocked. Cracks on back",
        "iPhone 16 Pro Max - 256GB - Desert Titanium (Unlocked) [Back Crack] [90% BH]",
        "Nintendo Switch Lite 32GB Pink/White Screen Crack w/CasE READ DESC",
        "iPhone 17 Pro - 256GB - Orange - Unlocked - Aftermarket Screen w/ Small Crack",
    ],
)
def test_a_bare_crack_is_a_defect(title):
    assert extract_variant(title, None, "Cell Phones & Smartphones").has_defect is True


def test_cracked_still_works():
    """The form that always matched must keep matching."""
    assert extract_variant("RTX 3090 cracked PCB").has_defect is True


@pytest.mark.parametrize(
    "title",
    [
        # A seller volunteering that the item is undamaged. The word naming
        # the defect is the same word used to deny it, exactly like "with
        # Original Box" on a $4,750 graphics card.
        "DEMO Apple iPhone 17 Pro Max 256GB - no cracks - locked - READ",
        "Apple iPhone 15 Pro Max 256GB - Unlocked - no chips or cracks",
        "iPhone 14 Pro 128GB Crack Free Excellent Condition",
        "Apple iPhone 13 256GB without cracks or scratches",
    ],
)
def test_denying_a_crack_is_not_a_defect(title):
    assert extract_variant(title, None, "Cell Phones & Smartphones").has_defect is False


def test_dead_on_arrival_in_both_spellings():
    """The vocabulary carried "dead" and never learned the abbreviation."""
    assert extract_variant("iphone 15 Plus DOA 256GB Icloud Off").has_defect is True
    assert extract_variant("RTX 4090 dead on arrival, selling as-is").has_defect is True


def test_doa_does_not_fire_inside_a_longer_word():
    assert extract_variant("Nintendo Switch DOAX VenusVacation game").has_defect is False


# ------------------------- a component missing a part, in the third word order


@pytest.mark.parametrize(
    "title",
    [
        # Put a $59 card at the top of the deal feed against a $2,699.99
        # estimate. _DEFECT_RE reads "MISSING CORE", _MISSING_SUFFIX_RE reads
        # "GPU AND MEMORY MISSING", and neither reads this.
        "ASUS TUF Gaming GeForce RTX 4090 OC NO RAM/GPU, READ",
        "AMD Ryzen 9 3900X Fan w/ Cords Original Box NO CPU! Mount",
        "Intel i9-13900K/KS Special Edition Box (NO CPU)",
        "i5-14600k empty box, No CPU",
        "i9-12900K empty box *No Processor*",
    ],
)
def test_a_component_missing_a_part_is_a_defect(title):
    assert extract_variant(title, None, "Graphics/Video Cards").has_defect is True


def test_a_machine_missing_a_part_is_still_a_configuration_not_a_defect():
    """The whole point of the discriminator, and the corpus contains the case:
    an $800 gaming PC a seller filed under a component category."""
    title = "gaming computer desktop nvidia graphics rtx 3060 no ram"
    assert extract_variant(title, None, "Graphics/Video Cards").has_defect is False
    # And in its own category, unchanged.
    assert extract_variant(title, None, "PC Desktops & All-In-Ones").has_defect is False


@pytest.mark.parametrize(
    "title,category",
    [
        # Real listings an earlier version of this rule flagged wrongly.
        ("HP Z8 G4 Workstation Xeon Gold 128GB NO GPU", "PC Desktops & All-In-Ones"),
        ("Ryzen barebones build tower, no GPU no OS", "PC Desktops & All-In-Ones"),
        ("ASUS ROG Zephyrus M16 laptop NO MEM NO HDD", "PC Laptops & Netbooks"),
    ],
)
def test_whole_machines_keep_reading_as_bare_rather_than_broken(title, category):
    variant = extract_variant(title, None, category)
    assert variant.has_defect is False
    assert variant.completeness == BARE


# --------------------------- model keys beyond graphics cards


@pytest.mark.parametrize(
    "title,expected",
    [
        # The pattern existed and was never called; its lookahead also wanted a
        # CPU word AFTER the number, which is the wrong way round.
        ("AMD Ryzen 9 5900X 12-Core Processor", "ryzen-5900x"),
        ("AMD Ryzen 7 7800X3D 8-Core AM5 Socket CPU", "ryzen-7800x3d"),
        ("Intel Core i9-13900K LGA 1700 Processor", "i9-13900k"),
        ("Intel Core i5 12600K CPU", "i5-12600k"),
        ("Intel Core i9 14900KS Special Edition Processor", "i9-14900ks"),
    ],
)
def test_cpu_model_keys(title, expected):
    assert extract_variant(title).model_key == expected


def test_a_four_digit_number_without_cpu_context_is_not_a_cpu():
    """Context first, then the number. A bare four-digit number is a year, a
    wattage, or a model of something else entirely."""
    assert extract_variant("Corsair RM1000e 1000W Power Supply").model_key is None
    assert extract_variant("Samsung 990 PRO 2TB NVMe SSD").model_key is None


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Nintendo Switch OLED Console Gaming System HEG-001 White", "switch-oled"),
        ("Nintendo Switch Lite Handheld Console - Legend of Zelda", "switch-lite"),
        ("Nintendo Switch 2 Console Blue Red Joy-Cons w/ Dock", "switch-2"),
        ("Nintendo Switch Console HAC-001 Joy Con Dock Grip", "switch"),
        ("Sony PlayStation 5 Disc Edition Console", "ps5"),
        ("Microsoft Xbox Series X 1TB Console", "xbox-series-x"),
    ],
)
def test_console_model_keys(title, expected):
    assert extract_variant(title).model_key == expected


def test_a_qualified_console_never_collapses_into_the_bare_one():
    """"Nintendo Switch OLED" contains "Nintendo Switch". If the bare form
    matched first, a $200 OLED and a $120 Lite would both file as `switch`,
    which is a $150 product."""
    keys = {
        extract_variant(t).model_key
        for t in (
            "Nintendo Switch",
            "Nintendo Switch OLED",
            "Nintendo Switch Lite",
            "Nintendo Switch 2",
        )
    }
    assert keys == {"switch", "switch-oled", "switch-lite", "switch-2"}


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Apple iPhone 15 Pro Max 256GB Blue Titanium Unlocked", "iphone-15-promax"),
        ("Apple iPhone 16 Pro 128GB Black", "iphone-16-pro"),
        ("Apple iPhone 15 Plus 128GB Unlocked", "iphone-15-plus"),
        # The largest unkeyed group before this pattern learned the "e" models.
        ("Apple iPhone 16e 128GB Black SIM-Free", "iphone-16e"),
        ("Apple iPhone Air 256GB Unlocked", "iphone-air"),
        ("Apple iPhone XR 64GB Blue Unlocked", "iphone-xr"),
        ("Samsung Galaxy S24 Ultra 512GB", "galaxy-s24-ultra"),
        ("Google Pixel 9 Pro XL 256GB", "pixel-9-proxl"),
    ],
)
def test_phone_model_keys(title, expected):
    assert extract_variant(title).model_key == expected


def test_an_e_model_never_collapses_into_the_numbered_one():
    """Same ordering hazard as the consoles: "iPhone 16e" contains "iPhone 16",
    and they are different products at different prices."""
    assert extract_variant("Apple iPhone 16e 128GB").model_key == "iphone-16e"
    assert extract_variant("Apple iPhone 16 128GB").model_key == "iphone-16"


def test_a_machine_is_keyed_by_its_graphics_card_not_its_processor():
    """Measured: keying PC Desktops by GPU takes their price spread from 5.08x
    to 2.0x. The card is the dominant price driver in a gaming machine, so GPU
    is tried before CPU and the order is load-bearing rather than arbitrary."""
    variant = extract_variant(
        "Gaming PC Ryzen 9 7950X RTX 4090 32GB DDR5 2TB NVMe",
        None,
        "PC Desktops & All-In-Ones",
    )
    assert variant.model_key == "rtx-4090"


def test_categories_priced_by_capacity_still_have_no_model_key():
    """Memory and storage are priced by capacity and generation, which have
    their own fields. None remains the honest answer there."""
    for title in (
        "Corsair Vengeance 32GB DDR5 6000MHz Desktop Memory",
        "Samsung 990 PRO 2TB M.2 NVMe Internal SSD",
        "ASUS ROG STRIX B650-A Gaming WiFi Motherboard",
    ):
        assert extract_variant(title).model_key is None


# --------------------------------- empty packaging, without the word "only"


@pytest.mark.parametrize(
    "title",
    [
        # Found by the ADR 0019 re-audit: two of these sat at $20.89 and
        # $31.50 inside a PC Desktops comp group whose median is $1,300.
        "Intel Core i9 12900K 12th/13th Generation CPU  EMPTU Box Wafer NO CPU INCLUDED",
        "RARE Intel Core i9 12900K 12th/13th Generation CPU Box Wafer NO CPU INCLUDED",
        "Intel Wafer Original Box Packaging i7 i9 13900K 14900K NO CPU NO CPU",
    ],
)
def test_empty_packaging_is_an_accessory(title):
    """One of them reads "EMPTU Box", so the rule cannot lean on "empty"."""
    assert extract_variant(title, None, "PC Desktops & All-In-Ones").is_accessory is True


@pytest.mark.parametrize(
    "title,category",
    [
        # A machine sold without a processor is an ordinary product, and this
        # is why the rule needs BOTH halves rather than just the absence.
        ("HP Z2 Tower G9 Barebones Workstation, no CPU", "PC Desktops & All-In-Ones"),
        ("ASUS ROG Motherboard Bundle, CPU not included", "Motherboards"),
        # A real card whose seller kept the packaging. The probe that killed a
        # bare trailing-"box" rule on 2026-08-07 found these.
        ("ASUS ROG STRIX RTX 4090 24GB OC with Original Box", "Graphics/Video Cards"),
        ("Apple iPhone 17 Pro Max 256GB Silver Unlocked Open Box", "Cell Phones & Smartphones"),
    ],
)
def test_a_product_sold_without_a_part_is_not_empty_packaging(title, category):
    assert extract_variant(title, None, category).is_accessory is False


# ------------------- eBay states the condition; nothing was reading it


@pytest.mark.parametrize(
    "condition",
    [
        "For parts or not working",
        # eBay returns condition in the seller's language, like category names.
        "Per parti di ricambio o non funzionante",
        "Als Ersatzteil / defekt",
    ],
)
def test_ebays_own_condition_settles_a_defect(condition):
    """1,668 listings carry a for-parts condition and 485 had has_defect false,
    because their titles never said so. Found by asking why a "PNY RTX 4090
    Verto 24gb" sat in the deal feed at $107.87 against $2,699.99 with no
    signal in its title: its condition column said so the whole time."""
    variant = extract_variant("PNY RTX 4090 Verto 24gb", None, "Graphics/Video Cards", condition)
    assert variant.has_defect is True
    assert variant.usable_as_comp is False


@pytest.mark.parametrize(
    "condition",
    [
        "Used",
        "Excellent - Refurbished",
        "Very Good",
        "Open box",
        "Gebraucht",
        "Certified - Refurbished",
        None,
    ],
)
def test_ordinary_conditions_are_not_defects(condition):
    assert extract_variant("PNY RTX 4090 Verto 24gb", None, None, condition).has_defect is False


def test_the_title_still_decides_when_the_condition_says_nothing():
    """The structured field is additional evidence, not a replacement: most
    listings are "Used" whatever their title admits."""
    variant = extract_variant("RTX 3090 cracked PCB, for parts", None, None, "Used")
    assert variant.has_defect is True


# ------------------- console accessories, visible only once consoles were keyed


@pytest.mark.parametrize(
    "title",
    [
        # Two of these went straight into the deal feed's top ten at 84% and
        # 79% once ADR 0022 gave consoles a model_key.
        "Nintendo Switch Dock Home Console Black TV Docking USB-C HDMI",
        "Nintendo Switch OLED Dock HEG-007 White/Black w/ AC Adapter",
        "Nintendo Switch 2 Charging Dock & AC Wall Adapter Cable",
        "Nintendo Switch 2 Dock Set, including the TV dock base",
    ],
)
def test_a_console_accessory_in_subject_position_is_an_accessory(title):
    """_SUBJECT_ACCESSORY_RE lists `dock` but anchors to the start of the
    title, and these start with the brand instead."""
    assert extract_variant(title, None, "Video Game Consoles").is_accessory is True


@pytest.mark.parametrize(
    "title",
    [
        # An inclusion, not the subject. The joining word is the discriminator.
        "Nintendo Switch 32GB Gray Console with Neon Red and Neon Blue Joy-Con",
        "Nintendo Switch 2 Black Handheld System w/ Pro Controller Used",
        "Nintendo Switch OLED w/carrying case-Console, joycons and case",
        # Names an accessory with no joining word at all, and is a $610
        # console. The bundle word is what saves it.
        "Nintendo Switch 2 Bundle 7 Games Pro Controller",
        "Nintendo Switch Console Bundle w/ 6 Games Pro Controller GameStop",
    ],
)
def test_a_console_that_merely_includes_an_accessory_is_still_a_console(title):
    assert extract_variant(title, None, "Video Game Consoles").is_accessory is False


@pytest.mark.parametrize(
    "title",
    [
        # "ONLY" here means "without games or extras", which is bare
        # completeness on a real $350-$400 console. A probe found 37 of these
        # and treating them as accessories would be the worst false positive
        # this vocabulary has produced.
        "Nintendo Switch 2 Console & Joy-Con Only",
        "Nintendo Switch 2 256GB Black Console Only w/ Joy-Con 2",
        "Nintendo Switch 2 CONSOLE ONLY + Charger",
        "Nintendo - Switch 2 256GB Console & Joycons ONLY",
    ],
)
def test_console_only_means_a_bare_console_not_an_accessory(title):
    variant = extract_variant(title, None, "Video Game Consoles")
    assert variant.is_accessory is False


# --------------- a platform is not a product, and a spelling gap in a block


def test_a_game_is_not_keyed_by_the_console_it_runs_on():
    """547 Video Games listings keyed as `switch`, so every Switch game shared
    one identity and a $7 budget title comped against $60 first-party ones,
    surfacing at an 82% discount. Naming a console in a games category names
    the platform, not the product."""
    for game in ("Pokemon Brilliant Diamond - Nintendo Switch", "MX vs ATV All Out - Nintendo Switch"):
        assert extract_variant(game, None, "Video Games").model_key is None
    # The hardware itself is unaffected.
    console = extract_variant("Nintendo Switch OLED Console", None, "Video Game Consoles")
    assert console.model_key == "switch-oled"


def test_a_water_cooling_block_is_an_accessory():
    r"""`water\s?block` never matched "Water Cooling Block" because a word sits
    between the two, and a $350 Bykski block comped against $1,499 cards."""
    variant = extract_variant(
        "Bykski RTX 3090 Ti HOF Water Cooling Block GPU Liquid Cooler",
        None,
        "Graphics/Video Cards",
    )
    assert variant.is_accessory is True


@pytest.mark.parametrize(
    "title",
    [
        # Eight real cards in the corpus are sold WITH a block. The inclusion
        # prefix is what separates them, which is why widening the spelling is
        # safe here and would not be on its own.
        "NVIDIA RTX 5090 Founders Edition With EKWB Founders Water Block",
        "NVIDIA RTX 3090FE w/ EKWB Water Block & Active Backplate",
        "GeForce RTX 3090 24GB GDDR6X Graphics Card with Waterblock",
        "EVGA GeForce RTX 3080 FTW3 Ultra Gaming 10GB + Bykski Full Cover Block",
    ],
)
def test_a_card_sold_with_a_block_is_still_a_card(title):
    assert extract_variant(title, None, "Graphics/Video Cards").is_accessory is False
