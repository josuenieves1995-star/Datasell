"""Pull clothing attributes (brand, size, color, ...) out of a listing.

eBay item pages have "item specifics" (label/value pairs) which are the most
reliable source. Search-result cards only have a title, so we also parse the
title with keyword rules. Specifics always win over title guesses.
"""

from __future__ import annotations

import re

# Ordered: more specific phrases first so "denim jacket" beats "jacket"/"denim".
CATEGORIES: list[tuple[str, list[str]]] = [
    ("Jeans", ["jeans", "jean", "selvedge", "501", "505", "levi's 5"]),
    ("Jacket", ["denim jacket", "trucker jacket", "jacket", "bomber", "windbreaker", "parka", "anorak", "blazer", "vest", "gilet"]),
    ("Coat", ["coat", "overcoat", "trench", "peacoat", "pea coat"]),
    ("Hoodie", ["hoodie", "hooded sweatshirt", "pullover hood", "zip hood"]),
    ("Sweatshirt", ["sweatshirt", "crewneck", "crew neck sweat"]),
    ("Sweater", ["sweater", "cardigan", "knit", "jumper", "pullover"]),
    ("Dress", ["dress", "gown", "sundress"]),
    ("Skirt", ["skirt"]),
    ("Shorts", ["shorts"]),
    ("Pants", ["pants", "trousers", "chinos", "joggers", "cargo", "slacks", "leggings", "overalls"]),
    ("T-Shirt", ["t-shirt", "t shirt", "tshirt", "tee", "single stitch"]),
    ("Shirt", ["button down", "button-down", "button up", "flannel", "oxford", "polo", "shirt", "blouse"]),
    ("Top", ["tank top", "tank", "crop top", "top", "camisole", "bodysuit"]),
    ("Activewear", ["track jacket", "tracksuit", "jersey", "sports bra", "athletic"]),
    ("Suit", ["suit"]),
    ("Shoes", ["sneakers", "sneaker", "shoes", "shoe", "boots", "boot", "sandals", "loafers", "heels", "trainers"]),
    ("Hat", ["hat", "cap", "beanie", "snapback"]),
    ("Bag", ["bag", "purse", "handbag", "backpack", "tote"]),
    ("Accessories", ["belt", "scarf", "gloves", "necktie", "bow tie", "socks", "sunglasses"]),
]

COLORS = [
    "black", "white", "gray", "grey", "charcoal", "navy", "blue", "light blue", "dark blue",
    "red", "burgundy", "maroon", "pink", "purple", "lavender", "green", "olive", "khaki",
    "tan", "beige", "cream", "ivory", "brown", "camel", "yellow", "mustard", "orange",
    "gold", "silver", "teal", "turquoise", "multicolor", "multi-color", "tie dye", "tie-dye",
    "camo", "plaid", "striped", "floral",
]

MATERIALS = [
    "cotton", "wool", "merino", "cashmere", "silk", "linen", "denim", "leather", "suede",
    "polyester", "nylon", "rayon", "viscose", "spandex", "elastane", "fleece", "corduroy",
    "velvet", "acrylic", "goose down", "gore-tex", "goretex", "canvas", "flannel", "twill",
]

# Common resale brands. Specifics usually give the brand; this is a fallback
# for search cards. Multi-word / punctuated names come first.
KNOWN_BRANDS = [
    "The North Face", "Ralph Lauren", "Polo Ralph Lauren", "Tommy Hilfiger", "Calvin Klein",
    "Abercrombie & Fitch", "American Eagle", "Banana Republic", "J.Crew", "J Crew",
    "Brooks Brothers", "Eddie Bauer", "L.L. Bean", "LL Bean", "Carhartt", "Patagonia",
    "Levi's", "Levis", "Wrangler", "Lee", "Nike", "Adidas", "Champion", "Supreme", "Stussy",
    "Harley Davidson", "Harley-Davidson", "Gap", "Old Navy", "Lululemon", "Columbia",
    "Arc'teryx", "Arcteryx", "Filson", "Pendleton", "Woolrich", "Dickies", "Ben Davis",
    "Gucci", "Prada", "Burberry", "Louis Vuitton", "Chanel", "Dior", "Versace", "Balenciaga",
    "Saint Laurent", "Yves Saint Laurent", "Hermes", "Coach", "Michael Kors", "Kate Spade",
    "Free People", "Anthropologie", "Madewell", "Zara", "H&M", "Uniqlo", "Everlane",
    "Reformation", "Aritzia", "Vince", "Theory", "Eileen Fisher", "Talbots", "Ann Taylor",
    "Lands' End", "Lands End", "Under Armour", "Puma", "Reebok", "New Balance", "Converse",
    "Vans", "Dr. Martens", "Doc Martens", "Timberland", "Red Wing", "Frye", "Ugg",
    "Barbour", "Belstaff", "Stone Island", "Moncler", "Canada Goose", "Fear of God",
    "Off-White", "Bape", "A Bathing Ape", "Kith", "Palace", "Diesel", "True Religion",
    "Lucky Brand", "7 For All Mankind", "AG Adriano Goldschmied", "Hollister", "Express",
    "Guess", "Fila", "Starter", "Tommy Bahama", "Vineyard Vines", "Lacoste", "Fred Perry",
    "Hugo Boss", "Armani", "Giorgio Armani", "Allsaints", "AllSaints", "Rag & Bone",
    "Acne Studios", "Comme des Garcons", "Issey Miyake", "Yohji Yamamoto", "Kapital",
]

LETTER_SIZES = ["XXS", "XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL", "4XL", "5XL", "OS"]
SIZE_WORDS = {
    "extra small": "XS", "small": "S", "medium": "M", "large": "L",
    "extra large": "XL", "x-large": "XL", "xx-large": "XXL", "one size": "OS",
}

DEPARTMENTS = [
    ("Women", [r"\bwomen'?s\b", r"\bwomens\b", r"\bladies\b", r"\bwmns\b", r"\bjuniors?\b"]),
    ("Men", [r"\bmen'?s\b", r"\bmens\b", r"\bmale\b"]),
    ("Kids", [r"\bkids?\b", r"\bboys?\b", r"\bgirls?\b", r"\btoddler\b", r"\byouth\b", r"\bbaby\b"]),
    ("Unisex", [r"\bunisex\b"]),
]

VINTAGE_PATTERNS = [
    r"\bvintage\b", r"\bvtg\b", r"\b(?:19)?[5-9]0'?s\b", r"\by2k\b", r"\bsingle stitch\b",
    r"\bmade in usa\b", r"\bdeadstock\b",
]

ERA_PATTERNS = [
    (r"\by2k\b|\b2000'?s\b", "2000s"),
    (r"\b(?:19)?90'?s\b", "1990s"),
    (r"\b(?:19)?80'?s\b", "1980s"),
    (r"\b(?:19)?70'?s\b", "1970s"),
    (r"\b(?:19)?60'?s\b", "1960s"),
    (r"\b(?:19)?50'?s\b", "1950s"),
]

# Map of normalized item-specific label -> our field name.
SPECIFIC_FIELDS = {
    "brand": "brand",
    "size": "size",
    "size (men's)": "size",
    "size (women's)": "size",
    "us shoe size": "size",
    "us shoe size (men's)": "size",
    "us shoe size (women's)": "size",
    "waist size": "waist",
    "inseam": "inseam",
    "color": "color",
    "main color": "color",
    "material": "material",
    "outer shell material": "material",
    "fabric type": "material",
    "department": "department",
    "type": "type",
    "style": "style",
    "condition": "condition",
    "size type": "size_type",
    "vintage": "vintage",
    "decade": "era",
    "pattern": "pattern",
    "fit": "fit",
}


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def _word_search(words: list[str], text: str) -> str | None:
    low = text.lower()
    for w in words:
        if re.search(r"(?<![a-z0-9])" + re.escape(w.lower()) + r"(?![a-z0-9])", low):
            return w
    return None


def guess_category(text: str) -> str | None:
    low = text.lower()
    best: tuple[int, str] | None = None
    # Pick the category whose keyword appears LAST in the title — titles like
    # "Nike Vintage Denim Jacket" put the garment noun at the end.
    for cat, words in CATEGORIES:
        for w in words:
            for m in re.finditer(r"(?<![a-z0-9])" + re.escape(w) + r"(?![a-z0-9])", low):
                pos = m.end()
                if best is None or pos > best[0]:
                    best = (pos, cat)
    return best[1] if best else None


def guess_brand(title: str) -> str | None:
    found = _word_search(KNOWN_BRANDS, title)
    if not found:
        return None
    canonical = {
        "levis": "Levi's", "j crew": "J.Crew", "ll bean": "L.L. Bean", "lands end": "Lands' End",
        "harley-davidson": "Harley Davidson", "arcteryx": "Arc'teryx", "doc martens": "Dr. Martens",
        "allsaints": "AllSaints", "a bathing ape": "Bape", "goretex": "Gore-Tex",
        "polo ralph lauren": "Ralph Lauren", "yves saint laurent": "Saint Laurent",
    }
    return canonical.get(found.lower(), found)


def guess_size(title: str) -> str | None:
    t = title
    # Waist x inseam, e.g. "32x30", "W32 L30", "32 x 34"
    m = re.search(r"\bW\s?(\d{2})\s*[xX/ ]?\s*L\s?(\d{2})\b", t)
    if m:
        return f"{m.group(1)}x{m.group(2)}"
    m = re.search(r"\b(\d{2})\s?[xX]\s?(\d{2})\b", t)
    if m and 22 <= int(m.group(1)) <= 60:
        return f"{m.group(1)}x{m.group(2)}"
    # "Size M", "Sz XL", "Size 10"
    m = re.search(r"\b(?:size|sz|sze)\.?\s*:?\s*([0-9]{1,2}(?:\.5)?|[A-Za-z0-9]{1,4})\b", t, re.I)
    if m:
        val = m.group(1).upper()
        if val in LETTER_SIZES or re.fullmatch(r"\d{1,2}(\.5)?", val):
            return val
        if val in ("XXXXL",):
            return "4XL"
    # Spelled out: "Men's Large"
    low = t.lower()
    for word in sorted(SIZE_WORDS, key=len, reverse=True):
        if re.search(r"(?<![a-z-])" + re.escape(word) + r"(?![a-z])", low):
            return SIZE_WORDS[word]
    # Standalone letter size token, e.g. "Nike Hoodie Black XL"
    for tok in re.findall(r"\b([0-9]?X{0,3}[SML]|[2-5]XL|XXS|OS)\b(?![.'&])", t):
        if tok in LETTER_SIZES:
            return tok
    return None


def guess_department(text: str) -> str | None:
    low = text.lower()
    for dept, pats in DEPARTMENTS:
        if any(re.search(p, low) for p in pats):
            return dept
    return None


def guess_era(text: str) -> str | None:
    low = text.lower()
    for pat, era in ERA_PATTERNS:
        if re.search(pat, low):
            return era
    return None


def is_vintage(text: str) -> bool:
    low = text.lower()
    return any(re.search(p, low) for p in VINTAGE_PATTERNS)


def normalize_condition(cond: str | None) -> str | None:
    if not cond:
        return None
    c = cond.lower()
    if "new with tags" in c or c.strip() in ("brand new", "new"):
        return "New with tags"
    if "new without tags" in c:
        return "New without tags"
    if "new with defects" in c:
        return "New with defects"
    if "pre-owned" in c or "preowned" in c or "used" in c:
        return "Pre-owned"
    if "parts" in c:
        return "For parts"
    return _norm(cond).split(":")[0] or None


def normalize_specifics(specifics: dict[str, str]) -> dict[str, str]:
    """Map raw eBay item specifics to our field names."""
    out: dict[str, str] = {}
    for label, value in specifics.items():
        key = SPECIFIC_FIELDS.get(_norm(label).lower().rstrip(":"))
        val = _norm(value)
        if key and val and key not in out:
            out[key] = val
    return out


def extract_attributes(title: str, specifics: dict[str, str] | None = None,
                       condition: str | None = None) -> dict:
    """Return clothing attributes for a listing.

    `specifics` values take priority; the title fills in anything missing.
    """
    spec = normalize_specifics(specifics or {})
    title = _norm(title)
    blob = " ".join([title, spec.get("type", ""), spec.get("style", "")])

    size = spec.get("size")
    if not size and spec.get("waist"):
        size = spec["waist"] + (f"x{spec['inseam']}" if spec.get("inseam") else "")
    size = size or guess_size(title)

    brand = spec.get("brand")
    if brand and brand.lower() in ("unbranded", "unknown", "n/a", "none", "-"):
        brand = None
    brand = brand or guess_brand(title)

    vintage_spec = spec.get("vintage", "").lower()
    vintage = vintage_spec == "yes" or (vintage_spec != "no" and is_vintage(title))

    return {
        "brand": brand,
        "category": guess_category(spec.get("type", "")) or guess_category(blob),
        "department": spec.get("department") or guess_department(title),
        "size": size,
        "color": spec.get("color") or (lambda c: c.title() if c else None)(_word_search(COLORS, title)),
        "material": spec.get("material") or (lambda m: m.title() if m else None)(_word_search(MATERIALS, title)),
        "style": spec.get("style") or spec.get("fit"),
        "condition": normalize_condition(condition or spec.get("condition")),
        "vintage": vintage,
        "era": spec.get("era") or guess_era(title),
    }
