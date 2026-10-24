"""Hungarian fashion words in English, for an image-text model that reads only English.

Shoppers type Hungarian with or without accents, with case and plural endings
(``kabátokat``) and as compounds (``bőrkabát``). ``analyse`` maps each word to
its English sense with a small suffix stripper and a compound splitter, and
keeps every word it cannot map, so nothing the shopper typed is lost.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from functools import lru_cache

from .taxonomy import fold

# Garments: Hungarian lemma -> (English, taxonomy category or None).
GARMENTS: dict[str, tuple[str, str | None]] = {
    # Coats and jackets.
    "kabát": ("coat", "coats"),
    "télikabát": ("winter coat", "coats"),
    "ballonkabát": ("trench coat", "coats"),
    "trencskó": ("trench coat", "coats"),
    "esőkabát": ("raincoat", "coats"),
    "gyapjúkabát": ("wool coat", "coats"),
    "pehelykabát": ("down coat", "coats"),
    "tollkabát": ("down coat", "coats"),
    "pufikabát": ("puffer coat", "coats"),
    "vattakabát": ("padded coat", "coats"),
    "sídzseki": ("ski jacket", "jackets"),
    "bundakabát": ("fur coat", "coats"),
    "bunda": ("fur coat", "coats"),
    "parka": ("parka", "coats"),
    "dzseki": ("jacket", "jackets"),
    "bőrdzseki": ("leather jacket", "jackets"),
    "farmerdzseki": ("denim jacket", "jackets"),
    "széldzseki": ("windbreaker", "jackets"),
    "bomber": ("bomber jacket", "jackets"),
    "anorák": ("anorak", "jackets"),
    "blézer": ("blazer", "jackets"),
    "zakó": ("blazer", "jackets"),
    "mellény": ("vest", "jackets"),
    "pufimellény": ("puffer vest", "jackets"),
    "kosztüm": ("suit", None),
    "öltöny": ("suit", None),
    # Dresses and one-pieces.
    "ruha": ("dress", "dresses"),
    "estélyi": ("evening gown", "dresses"),
    "koktélruha": ("cocktail dress", "dresses"),
    "maxiruha": ("maxi dress", "dresses"),
    "miniruha": ("mini dress", "dresses"),
    "midiruha": ("midi dress", "dresses"),
    "ingruha": ("shirt dress", "dresses"),
    "kötöttruha": ("knitted dress", "dresses"),
    "menyasszonyi": ("wedding dress", "dresses"),
    "tunika": ("tunic", "tops"),
    "overál": ("jumpsuit", None),
    "kezeslábas": ("jumpsuit", None),
    "fürdőruha": ("swimsuit", None),
    "bikini": ("bikini", None),
    "pizsama": ("pyjamas", None),
    "hálóing": ("nightgown", None),
    "köntös": ("bathrobe", None),
    "fehérnemű": ("underwear", None),
    "melltartó": ("bra", None),
    "bugyi": ("panties", None),
    "alsónadrág": ("underpants", None),
    "boxeralsó": ("boxer shorts", None),
    "zokni": ("socks", "accessories"),
    "harisnya": ("tights", "accessories"),
    "harisnyanadrág": ("tights", "accessories"),
    # Knitwear and tops.
    "pulóver": ("sweater", "sweaters"),
    "pulcsi": ("sweater", "sweaters"),
    "szvetter": ("sweater", "sweaters"),
    "garbó": ("turtleneck sweater", "sweaters"),
    "kardigán": ("cardigan", "sweaters"),
    "kardigan": ("cardigan", "sweaters"),
    "melegítőfelső": ("sweatshirt", "sweaters"),
    "szabadidőfelső": ("sweatshirt", "sweaters"),
    "kapucnis": ("hooded", None),
    "felső": ("top", "tops"),
    "póló": ("t-shirt", "tops"),
    "pólóing": ("polo shirt", "tops"),
    "trikó": ("tank top", "tops"),
    "atléta": ("tank top", "tops"),
    "body": ("bodysuit", "tops"),
    "top": ("top", "tops"),
    "ing": ("shirt", "shirts"),
    "farmering": ("denim shirt", "shirts"),
    "flanelling": ("flannel shirt", "shirts"),
    "blúz": ("blouse", "blouses"),
    # Trousers, jeans, skirts and shorts.
    "nadrág": ("trousers", "trousers"),
    "farmernadrág": ("jeans", "jeans"),
    "farmer": ("jeans", "jeans"),
    "rövidnadrág": ("shorts", "shorts"),
    "bermuda": ("bermuda shorts", "shorts"),
    "melegítőnadrág": ("sweatpants", "trousers"),
    "szabadidőnadrág": ("joggers", "trousers"),
    "melegítő": ("tracksuit", "trousers"),
    "tréningnadrág": ("track pants", "trousers"),
    "sínadrág": ("ski trousers", "trousers"),
    "legging": ("leggings", "trousers"),
    "jogger": ("joggers", "trousers"),
    "chino": ("chinos", "trousers"),
    "cicanadrág": ("leggings", "trousers"),
    "leggings": ("leggings", "trousers"),
    "kertésznadrág": ("dungarees", "trousers"),
    "szövetnadrág": ("tailored trousers", "trousers"),
    "öltönynadrág": ("suit trousers", "trousers"),
    "vászonnadrág": ("linen trousers", "trousers"),
    "bőrnadrág": ("leather trousers", "trousers"),
    "szoknya": ("skirt", "skirts"),
    "miniszoknya": ("mini skirt", "skirts"),
    "farmerszoknya": ("denim skirt", "skirts"),
    "ceruzaszoknya": ("pencil skirt", "skirts"),
    "harangszoknya": ("flared skirt", "skirts"),
    "tüllszoknya": ("tulle skirt", "skirts"),
    # Shoes.
    "cipő": ("shoes", "shoes"),
    "félcipő": ("shoes", "shoes"),
    "sportcipő": ("sneakers", "shoes"),
    "edzőcipő": ("trainers", "shoes"),
    "tornacipő": ("sneakers", "shoes"),
    "futócipő": ("running shoes", "shoes"),
    "magassarkú": ("high heels", "shoes"),
    "tűsarkú": ("stiletto heels", "shoes"),
    "körömcipő": ("pumps", "shoes"),
    "balerinacipő": ("ballet flats", "shoes"),
    "balerina": ("ballet flats", "shoes"),
    "csizma": ("boots", "shoes"),
    "bakancs": ("lace-up boots", "shoes"),
    "bokacsizma": ("ankle boots", "shoes"),
    "bokacipő": ("ankle boots", "shoes"),
    "térdcsizma": ("knee-high boots", "shoes"),
    "combcsizma": ("thigh-high boots", "shoes"),
    "gumicsizma": ("rain boots", "shoes"),
    "hócsizma": ("snow boots", "shoes"),
    "hótaposó": ("snow boots", "shoes"),
    "szandál": ("sandals", "shoes"),
    "papucs": ("slides", "shoes"),
    "strandpapucs": ("flip flops", "shoes"),
    "mamusz": ("slippers", "shoes"),
    "mokaszin": ("moccasins", "shoes"),
    "klumpa": ("clogs", "shoes"),
    "espadrilles": ("espadrilles", "shoes"),
    # Bags.
    "táska": ("bag", "bags"),
    "kézitáska": ("handbag", "bags"),
    "válltáska": ("shoulder bag", "bags"),
    "oldaltáska": ("crossbody bag", "bags"),
    "hátizsák": ("backpack", "bags"),
    "borítéktáska": ("clutch bag", "bags"),
    "övtáska": ("belt bag", "bags"),
    "sporttáska": ("gym bag", "bags"),
    "strandtáska": ("beach bag", "bags"),
    "bevásárlótáska": ("tote bag", "bags"),
    "pénztárca": ("wallet", "accessories"),
    "tárca": ("wallet", "accessories"),
    "bőrönd": ("suitcase", "bags"),
    "utazótáska": ("travel bag", "bags"),
    "hátitáska": ("backpack", "bags"),
    "bevásárlószatyor": ("tote bag", "bags"),
    # Accessories.
    "kiegészítő": ("accessory", "accessories"),
    "öv": ("belt", "accessories"),
    "sál": ("scarf", "accessories"),
    "kendő": ("scarf", "accessories"),
    "sapka": ("hat", "accessories"),
    "kötött sapka": ("beanie", "accessories"),
    "baseballsapka": ("baseball cap", "accessories"),
    "kalap": ("hat", "accessories"),
    "kesztyű": ("gloves", "accessories"),
    "napszemüveg": ("sunglasses", "accessories"),
    "szemüveg": ("glasses", "accessories"),
    "ékszer": ("jewellery", "accessories"),
    "nyaklánc": ("necklace", "accessories"),
    "fülbevaló": ("earrings", "accessories"),
    "karkötő": ("bracelet", "accessories"),
    "gyűrű": ("ring", "accessories"),
    "karóra": ("watch", "accessories"),
    "óra": ("watch", "accessories"),
    "nyakkendő": ("tie", "accessories"),
    "csokornyakkendő": ("bow tie", "accessories"),
    "hajpánt": ("headband", "accessories"),
    "esernyő": ("umbrella", "accessories"),
    "derékmelegítő": ("waist warmer", "accessories"),
    "szett": ("set", None),
}

COLOURS = {
    "fekete": "black",
    "fehér": "white",
    "törtfehér": "off-white",
    "ekrü": "ecru",
    "piros": "red",
    "vörös": "red",
    "meggypiros": "cherry red",
    "borvörös": "wine red",
    "bordó": "burgundy",
    "burgundi": "burgundy",
    "kék": "blue",
    "sötétkék": "navy",
    "tengerészkék": "navy",
    "világoskék": "light blue",
    "égszínkék": "sky blue",
    "királykék": "royal blue",
    "farmerkék": "denim blue",
    "türkiz": "turquoise",
    "zöld": "green",
    "sötétzöld": "dark green",
    "világoszöld": "light green",
    "olívazöld": "olive green",
    "olíva": "olive",
    "khaki": "khaki",
    "menta": "mint",
    "sárga": "yellow",
    "mustár": "mustard",
    "mustársárga": "mustard yellow",
    "barna": "brown",
    "sötétbarna": "dark brown",
    "világosbarna": "light brown",
    "konyak": "cognac",
    "teveszín": "camel",
    "szürke": "grey",
    "sötétszürke": "dark grey",
    "világosszürke": "light grey",
    "antracit": "charcoal",
    "ezüst": "silver",
    "arany": "gold",
    "bézs": "beige",
    "drapp": "beige",
    "krém": "cream",
    "rózsaszín": "pink",
    "púderrózsaszín": "powder pink",
    "púder": "powder pink",
    "lila": "purple",
    "levendula": "lavender",
    "ibolya": "violet",
    "narancs": "orange",
    "narancssárga": "orange",
    "korall": "coral",
    "barack": "peach",
    "színes": "colourful",
    "tarka": "multicoloured",
    "vegyes": "mixed",
    "szín": "colour",
    "színű": "coloured",
    "világos": "light",
    "sötét": "dark",
    "éjkék": "midnight blue",
    "galambkék": "dove blue",
    "homok": "sand",
    "rozsda": "rust",
    "rozsdavörös": "rust red",
    "piszkosfehér": "off-white",
    "padlizsán": "aubergine",
    "melír": "melange",
    "melírozott": "melange",
}

MATERIALS = {
    "bőr": "leather",
    "műbőr": "faux leather",
    "velúr": "suede",
    "szarvasbőr": "suede",
    "pamut": "cotton",
    "len": "linen",
    "vászon": "linen",
    "gyapjú": "wool",
    "kasmír": "cashmere",
    "merinó": "merino wool",
    "selyem": "silk",
    "szatén": "satin",
    "bársony": "velvet",
    "kordbársony": "corduroy",
    "kord": "corduroy",
    "csipke": "lace",
    "tüll": "tulle",
    "sifon": "chiffon",
    "viszkóz": "viscose",
    "poliészter": "polyester",
    "műszőrme": "faux fur",
    "szőrme": "fur",
    "polár": "fleece",
    "flanel": "flannel",
    "kötött": "knitted",
    "horgolt": "crochet",
    "steppelt": "quilted",
    "pufi": "puffer",
    "pehely": "down",
    "farmeranyag": "denim",
    "tweed": "tweed",
    "neoprén": "neoprene",
    "gumi": "rubber",
    "vízálló": "waterproof",
    "flitteres": "sequinned",
    "gyöngyös": "beaded",
    "szegecses": "studded",
    "bélelt": "lined",
    "dzsörzé": "jersey",
    "szövet": "woven fabric",
    "vatta": "padded",
    "softshell": "softshell",
    "denim": "denim",
}

PATTERNS = {
    "csíkos": "striped",
    "kockás": "checked",
    "skótkockás": "tartan",
    "pöttyös": "polka dot",
    "virágos": "floral",
    "virágmintás": "floral print",
    "mintás": "patterned",
    "mintájú": "patterned",
    "egyszínű": "plain",
    "állatmintás": "animal print",
    "leopárdmintás": "leopard print",
    "kígyómintás": "snakeskin print",
    "zebramintás": "zebra print",
    "terepmintás": "camouflage",
    "batikolt": "tie-dye",
    "nyomott": "printed",
    "feliratos": "slogan",
    "logós": "logo",
    "hímzett": "embroidered",
    "pepita": "houndstooth",
    "halszálkás": "herringbone",
    "paisley": "paisley",
    "geometrikus": "geometric",
    "káró": "check",
    "sima": "plain",
}

STYLES = {
    # Fit and shape.
    "szűk": "slim",
    "karcsúsított": "fitted",
    "testhezálló": "bodycon",
    "bő": "loose",
    "laza": "relaxed",
    "oversize": "oversized",
    "oversized": "oversized",
    "egyenes": "straight",
    "trapéz": "flared",
    "harang": "flared",
    "rakott": "pleated",
    "pliszírozott": "pleated",
    "szaggatott": "ripped",
    "koptatott": "washed",
    "sztreccs": "stretch",
    "rugalmas": "stretch",
    "kivágott": "cut-out",
    "kivágás": "neckline",
    "kerek": "round",
    "pliszé": "pleated",
    "csatos": "buckled",
    "kifordítható": "reversible",
    "boka": "ankle",
    "funkcionális": "technical",
    "kismama": "maternity",
    "utazó": "travel",
    "bevásárló": "shopping",
    "tréning": "training",
    "sí": "ski",
    "közepes": "medium",
    "rövidített": "cropped",
    "crop": "cropped",
    "magas": "high",
    "alacsony": "low",
    "széles": "wide",
    "keskeny": "narrow",
    "nagy": "big",
    "kis": "small",
    "kicsi": "small",
    "hosszú": "long",
    "rövid": "short",
    "mini": "mini",
    "midi": "midi",
    "maxi": "maxi",
    "háromnegyedes": "three-quarter",
    "térdig": "knee-length",
    "bokáig": "ankle-length",
    "földig": "floor-length",
    "ujjatlan": "sleeveless",
    "pántos": "strappy",
    "spagettipántos": "spaghetti strap",
    "puffos": "puff sleeve",
    "galléros": "collared",
    "garbós": "turtleneck",
    "csuklyás": "hooded",
    "cipzáras": "zip-up",
    "gombos": "button-up",
    "zsebes": "pocket",
    "fodros": "ruffled",
    "masnis": "bow",
    "rojtos": "fringed",
    "övvel": "belted",
    "övös": "belted",
    "fűzős": "lace-up",
    "platform": "platform",
    "telitalpú": "wedge",
    "lapos": "flat",
    "sarkú": "heeled",
    "vastag": "thick",
    "vékony": "thin",
    "könnyű": "lightweight",
    "meleg": "warm",
    "puha": "soft",
    "fényes": "shiny",
    "csillogó": "glitter",
    "matt": "matte",
    "átlátszó": "sheer",
    "áttetsző": "sheer",
    "elegáns": "elegant",
    "sportos": "sporty",
    "sport": "sport",
    "hétköznapi": "casual",
    "vintage": "vintage",
    "retro": "retro",
    "bohém": "boho",
    "klasszikus": "classic",
    "basic": "basic",
    "ujjú": "sleeve",
    "nyakú": "neck",
    "szárú": "leg",
    "derekú": "waist",
    "talpú": "sole",
    "orrú": "toe",
    # Occasions and seasons.
    "nyári": "summer",
    "nyár": "summer",
    "téli": "winter",
    "tél": "winter",
    "tavaszi": "spring",
    "tavasz": "spring",
    "őszi": "autumn",
    "ősz": "autumn",
    "átmeneti": "light",
    "alkalmi": "formal",
    "esti": "evening",
    "koktél": "cocktail",
    "esküvői": "wedding",
    "esküvő": "wedding",
    "ballagási": "graduation",
    "parti": "party",
    "buli": "party",
    "bulis": "party",
    "irodai": "office",
    "iroda": "office",
    "munka": "work",
    "üzleti": "business",
    "strand": "beach",
    "otthoni": "lounge",
    "karácsonyi": "christmas",
    "farsangi": "costume",
    "túra": "hiking",
    "futó": "running",
    "jóga": "yoga",
    "fitnesz": "fitness",
    "iskolai": "school",
}

# Who it is for; kept as a soft hint, never a filter.
GENDERS = {
    "női": ("women's", "women"),
    "nőknek": ("women's", "women"),
    "hölgy": ("women's", "women"),
    "férfi": ("men's", "men"),
    "férfiaknak": ("men's", "men"),
    "uraknak": ("men's", "men"),
    "gyerek": ("kids'", "kids"),
    "gyermek": ("kids'", "kids"),
    "lány": ("girls'", "kids"),
    "kislány": ("girls'", "kids"),
    "fiú": ("boys'", "kids"),
    "kisfiú": ("boys'", "kids"),
    "baba": ("baby", "kids"),
    "lányka": ("girls'", "kids"),
    "tini": ("teen", "kids"),
    "unisex": ("unisex", None),
}
ENGLISH_GENDERS = {
    "women": "women",
    "womens": "women",
    "woman": "women",
    "ladies": "women",
    "men": "men",
    "mens": "men",
    "man": "men",
    "kids": "kids",
    "kid": "kids",
    "girls": "kids",
    "boys": "kids",
    "baby": "kids",
}

# Two-word expressions whose meaning is not the sum of the words.
PHRASES = {
    ("hosszú", "ujjú"): "long sleeve",
    ("rövid", "ujjú"): "short sleeve",
    ("háromnegyedes", "ujjú"): "three-quarter sleeve",
    ("puffos", "ujjú"): "puff sleeve",
    ("denevér", "ujjú"): "batwing sleeve",
    ("v", "nyakú"): "v-neck",
    ("kerek", "nyakú"): "crew neck",
    ("magas", "nyakú"): "high neck",
    ("csónak", "nyakú"): "boat neck",
    ("mély", "kivágású"): "plunging neckline",
    ("vállra", "ejtett"): "off-the-shoulder",
    ("pánt", "nélküli"): "strapless",
    ("magas", "derekú"): "high-waisted",
    ("alacsony", "derekú"): "low-rise",
    ("magas", "sarkú"): "high heels",
    ("lapos", "talpú"): "flat",
    ("széles", "szárú"): "wide-leg",
    ("bő", "szárú"): "wide-leg",
    ("egyenes", "szárú"): "straight-leg",
    ("szűk", "szárú"): "skinny",
    ("térdig", "érő"): "knee-length",
    ("bokáig", "érő"): "ankle-length",
    ("földig", "érő"): "floor-length",
    ("nyitott", "orrú"): "open-toe",
    ("kapucnis", "pulóver"): "hoodie",
    ("melegítő", "felső"): "sweatshirt",
    ("szabadidő", "felső"): "sweatshirt",
    ("kötött", "sapka"): "beanie",
    ("estélyi", "ruha"): "evening dress",
    ("menyasszonyi", "ruha"): "wedding dress",
    ("alkalmi", "ruha"): "occasion dress",
    ("baseball", "sapka"): "baseball cap",
    ("dupla", "soros"): "double-breasted",
    ("skót", "kockás"): "tartan",
    ("a", "vonalú"): "a-line",
    ("v", "kivágás"): "v-neck",
    ("kerek", "kivágás"): "round neck",
    ("vegyes", "színek"): "multicoloured",
}

# Function words and shopper filler with no visual meaning.
FILLER = {
    "a",
    "az",
    "egy",
    "és",
    "vagy",
    "is",
    "meg",
    "de",
    "nem",
    "keresek",
    "keresem",
    "szeretnék",
    "kellene",
    "kéne",
    "kell",
    "valami",
    "valamilyen",
    "olyan",
    "ilyen",
    "hasonló",
    "mint",
    "amilyen",
    "stílusú",
    "stílus",
    "darab",
    "új",
    "akciós",
    "olcsó",
    "kedvezményes",
    "leárazott",
    "eredeti",
    "minőségi",
    "szép",
    "jó",
    "méret",
    "méretű",
}
CONNECTORS = {"és": "and", "nélkül": "without"}

# Endings, longest first: case, plural, accusative, possessive and adjective.
# Accents are folded away, so "-ból" and "-ből" are both "bol".
SUFFIXES = (
    "kent",
    "jait",
    "jeit",
    "ainak",
    "einek",
    "bol",
    "rol",
    "tol",
    "nak",
    "nek",
    "val",
    "vel",
    "ban",
    "ben",
    "hoz",
    "hez",
    "nal",
    "nel",
    "ert",
    "kat",
    "ket",
    "jat",
    "jet",
    "ait",
    "eit",
    "ok",
    "ek",
    "ak",
    "ot",
    "et",
    "at",
    "ig",
    "ra",
    "re",
    "ba",
    "be",
    "on",
    "en",
    "ja",
    "je",
    "al",
    "el",
    "ai",
    "ei",
    "k",
    "t",
    "n",
    "i",
)
MIN_STEM = 2


@dataclass(frozen=True)
class Sense:
    english: str
    kind: str  # garment, colour, material, pattern, style, gender, filler, connector
    category: str | None = None
    gender: str | None = None


def _senses() -> dict[str, Sense]:
    senses: dict[str, Sense] = {}
    for word, (english, category) in GARMENTS.items():
        senses[fold(word)] = Sense(english, "garment", category)
    for kind, table in (
        ("colour", COLOURS),
        ("material", MATERIALS),
        ("pattern", PATTERNS),
        ("style", STYLES),
    ):
        for word, english in table.items():
            senses.setdefault(fold(word), Sense(english, kind))
    for word, (english, gender) in GENDERS.items():
        senses[fold(word)] = Sense(english, "gender", gender=gender)
    for word in FILLER:
        senses.setdefault(fold(word), Sense("", "filler"))
    for word, english in CONNECTORS.items():
        senses[fold(word)] = Sense(english, "connector")
    return senses


SENSES = _senses()
PHRASE_SENSES = {(fold(a), fold(b)): english for (a, b), english in PHRASES.items()}
# A garment that modifies another garment names its fabric: "farmerdzseki" is denim.
MODIFIER_ENGLISH = {"farmer": "denim", "bor": "leather", "sport": "sport"}
_WORD = re.compile(r"[^\W_]+(?:['’-][^\W_]+)*")
_HUNGARIAN_LETTERS = set("áéíóöőúüű")


# English words that carry a hint, for shoppers who type English.
ENGLISH_CATEGORIES = {
    **{word: "coats" for word in ("coat", "parka", "trench", "raincoat", "overcoat")},
    **{
        word: "jackets"
        for word in ("jacket", "blazer", "bomber", "windbreaker", "vest", "gilet", "anorak")
    },
    **{word: "dresses" for word in ("dress", "gown")},
    **{
        word: "sweaters"
        for word in (
            "sweater",
            "jumper",
            "hoodie",
            "cardigan",
            "sweatshirt",
            "pullover",
            "turtleneck",
        )
    },
    "shirt": "shirts",
    "blouse": "blouses",
    **{
        word: "tops"
        for word in ("top", "tshirt", "tee", "tank", "camisole", "bodysuit", "tunic", "polo")
    },
    **{
        word: "trousers"
        for word in ("trousers", "pants", "leggings", "joggers", "sweatpants", "chinos")
    },
    "jeans": "jeans",
    "skirt": "skirts",
    "shorts": "shorts",
    **{
        word: "shoes"
        for word in (
            "shoe",
            "shoes",
            "sneaker",
            "trainers",
            "boot",
            "heels",
            "sandal",
            "loafer",
            "pumps",
            "flats",
            "slippers",
            "mules",
            "clogs",
        )
    },
    **{
        word: "bags"
        for word in ("bag", "handbag", "backpack", "tote", "clutch", "purse", "satchel")
    },
    **{
        word: "accessories"
        for word in (
            "scarf",
            "belt",
            "hat",
            "cap",
            "beanie",
            "gloves",
            "sunglasses",
            "socks",
            "tights",
            "wallet",
            "necklace",
            "earrings",
            "bracelet",
            "watch",
            "tie",
        )
    },
}
ENGLISH_COLOURS = {
    word for english in COLOURS.values() if " " not in english for word in [english]
} | {"gray", "ivory", "tan", "teal", "maroon", "lilac", "nude", "multicolour", "multicolor"}


_MORE_ENGLISH = """
a an and of with for in on the without photo picture
jackets coats dresses sweaters shirts blouses tops tshirts denim skirts sneakers
boots sandals loafers bags jewelry suit romper lingerie pajamas robe moccasins oxford
leather suede cotton linen wool cashmere silk satin velvet corduroy lace chiffon
knit knitted ribbed quilted floral striped plaid checked tartan polka dot print
printed slim skinny loose relaxed oversized fitted straight wide flared cropped long
short mini midi maxi sleeve sleeves sleeveless neck collar hood hooded zip button
high low waisted rise summer winter spring autumn fall evening party casual formal
office wedding beach sport sporty running vintage elegant classic warm light
stripe stripes check metallic jersey sweat legging organic training woven cable ruffle ruffled
boxy lounge trim fleece padded cargo mule platform buckle crossbody pendant aviator charm stud
crystal laptop travel ski wrap tunic maternity denim washed distressed ripped embroidered
"""


def _english_words() -> frozenset[str]:
    words = set(ENGLISH_GENDERS) | set(ENGLISH_CATEGORIES) | ENGLISH_COLOURS
    phrases = [sense.english for sense in SENSES.values()] + list(PHRASES.values())
    for english in phrases:
        for word in english.replace("'", "").split():
            words.add(word.replace("-", ""))
            words.update(part for part in word.split("-") if len(part) > 1)
    words.update(_MORE_ENGLISH.split())
    return frozenset(words)


ENGLISH_WORDS = _english_words()


@dataclass(frozen=True)
class Word:
    """One typed word and how it was read."""

    text: str  # as typed
    english: str  # what reaches the model; the typed word when unknown
    kind: str  # garment, colour, material, pattern, style, gender, filler, connector,
    # other (an English word without a hint), machine (translated by the model) or unknown
    language: str | None = None  # "hu", "en", or None when unknown
    category: str | None = None
    gender: str | None = None
    lemma: str | None = None  # the folded dictionary form that matched

    @property
    def known(self) -> bool:
        return self.kind != "unknown"


@lru_cache(maxsize=4096)
def stems(word: str) -> tuple[str, ...]:
    """Folded candidate stems of ``word``, the word itself first.

    Up to two endings are stripped (``kabatokat`` -> ``kabatok`` -> ``kabat``). An
    assimilated instrumental (``kabattal``) also loses its doubled consonant.
    """
    found = [word]
    frontier = [word]
    for _ in range(2):
        following = []
        for current in frontier:
            for ending in SUFFIXES:
                if current.endswith(ending) and len(current) - len(ending) >= MIN_STEM:
                    stem = current[: -len(ending)]
                    variants = [stem]
                    if ending in {"al", "el"} and len(stem) > 2 and stem[-1] == stem[-2]:
                        variants.append(stem[:-1])
                    for variant in variants:
                        if variant not in found:
                            found.append(variant)
                            following.append(variant)
        frontier = following
    return tuple(found)


def lemma_of(word: str) -> str | None:
    """The dictionary form of a folded word, trying its stems from the longest down."""
    for stem in sorted(stems(word), key=len, reverse=True):
        if stem in SENSES:
            return stem
    return None


def lookup(word: str) -> Sense | None:
    lemma = lemma_of(word)
    return SENSES[lemma] if lemma else None


def _compound(word: str) -> Sense | None:
    """Split an unknown compound into two known parts: ``bőrkabát`` is leather coat."""
    for cut in range(MIN_STEM, len(word) - MIN_STEM + 1):  # longest head first
        head = lookup(word[cut:])
        modifier = SENSES.get(word[:cut])
        if head is None or modifier is None or modifier.kind in {"filler", "connector"}:
            continue
        if head.kind in {"filler", "connector", "gender"}:
            continue
        phrase = PHRASE_SENSES.get((word[:cut], word[cut:]))
        if phrase is None:
            for stem in stems(word[cut:]):
                phrase = PHRASE_SENSES.get((word[:cut], stem)) or phrase
        if phrase:
            return Sense(phrase, head.kind, head.category)
        first = (
            MODIFIER_ENGLISH.get(word[:cut], modifier.english)
            if modifier.kind == "garment"
            else modifier.english
        )
        return Sense(f"{first} {head.english}", head.kind, head.category)
    return None


def _english(text: str, folded: str) -> Word:
    category = ENGLISH_CATEGORIES.get(folded) or ENGLISH_CATEGORIES.get(folded.removesuffix("s"))
    gender = ENGLISH_GENDERS.get(folded)
    if category:
        kind = "garment"
    elif folded in ENGLISH_COLOURS:
        kind = "colour"
    elif gender:
        kind = "gender"
    else:
        kind = "other"
    return Word(text, text.lower(), kind, "en", category, gender, folded)


def _read(text: str) -> Word:
    folded = fold(text).replace("'", "").replace("’", "")
    if folded in SENSES:
        lemma = folded
    elif folded in ENGLISH_WORDS:
        return _english(text, folded)
    else:
        lemma = lemma_of(folded)
    if lemma is None and folded.removesuffix("s") in ENGLISH_WORDS:
        return _english(text, folded)
    sense = SENSES[lemma] if lemma else None
    if sense is None and len(folded) >= 2 * MIN_STEM + 2:
        sense = _compound(folded)
    if sense is None:
        return Word(text, text, "unknown")
    return Word(
        text, sense.english, sense.kind, "hu", sense.category, sense.gender, lemma or folded
    )


def _phrase(first: str, second: str) -> str | None:
    a, b = fold(first), fold(second)
    for stem in stems(b):
        english = PHRASE_SENSES.get((a, stem))
        if english:
            return english
    return None


def analyse(text: str) -> list[Word]:
    """Read each word of a query; two-word expressions and hyphenated pairs first."""
    raw = _WORD.findall(text)
    words: list[Word] = []
    position = 0
    while position < len(raw):
        token = raw[position]
        if position + 1 < len(raw):
            english = _phrase(token, raw[position + 1])
            if english:
                head = _read(raw[position + 1])
                kind = "garment" if head.kind == "garment" else "style"
                text = f"{token} {raw[position + 1]}"
                words.append(Word(text, english, kind, "hu", head.category, lemma=head.lemma))
                position += 2
                continue
        if "-" in token:
            words.extend(_hyphenated(token))
        else:
            words.append(_read(token))
        position += 1
    return words


def _hyphenated(token: str) -> list[Word]:
    parts = token.split("-")
    if len(parts) == 2:
        english = _phrase(*parts)
        if english:
            return [Word(token, english, "style", "hu")]
    read = [_read(part) for part in parts]
    if len(read) > 1 and all(item.kind == "colour" for item in read):
        # "fekete-fehér" is one two-colour item: black and white.
        english = " and ".join(item.english for item in read)
        return [Word(token, english, "colour", read[0].language, lemma=read[0].lemma)]
    if all(item.known for item in read):
        return read
    joined = _read(token.replace("-", ""))
    if joined.language == "en":
        return [replace(joined, text=token, english=token.lower())]
    return [replace(joined, text=token)] if joined.known else read


def looks_hungarian(word: str) -> bool:
    """Hungarian letters mark a word the dictionary should have known."""
    return bool(_HUNGARIAN_LETTERS & set(word.casefold()))


def english_text(words: list[Word]) -> str:
    """The English the model sees; filler drops, unknown words pass through as typed."""
    return " ".join(word.english for word in words if word.english).strip()
