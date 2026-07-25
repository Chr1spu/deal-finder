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
