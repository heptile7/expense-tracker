"""Turn a raw bank narration into a stable merchant key.

The merchant key is what "similar payment" means throughout the app: a user
correction is stored against it, and every past and future transaction with the
same key follows that correction. It is the direction (D/C) plus the cleaned
payee name, so the same merchant groups together whether it was paid by UPI,
card or NEFT, while a refund *from* a merchant stays separate from a purchase.

Handles HDFC-style dash-delimited narrations (``UPI-SWIGGY-swiggy@icici-...``)
and the slash-delimited style used by CUB and most other banks
(``UPI/DR/412345678901/SWIGGY/YESB/swiggy@yesbank/...``).
"""

import re
from dataclasses import dataclass

# Leading channel markers, stripped repeatedly from the start of the narration.
_PREFIX_RE = re.compile(
    r"^(?:UPI[A-Z]{0,4}|NEFT(?:\s*(?:CR|DR))?|RTGS(?:\s*(?:CR|DR))?|IMPS|MMT|CRV\s+POS|POS|PCD|ME\s+DC\s+SI|DC\s+SI|"
    r"ECOM|ACH\s*[DC]?|NACH\s*[DC]?|ECS|BIL|ONL|INB|IB|MB|FT|TPT|TO|BY|CR|DR)\b\s*[-/:]?\s*"
)
_CHANNELS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(?:TO\s+|BY\s+)?UPI|\bUPI\b"), "UPI"),
    (re.compile(r"^(?:TO\s+|BY\s+)?NEFT\b|\bNEFT\b"), "NEFT"),
    (re.compile(r"^(?:TO\s+|BY\s+)?RTGS\b"), "RTGS"),
    (re.compile(r"^(?:TO\s+|BY\s+)?(?:IMPS|MMT)\b|\bIMPS\b"), "IMPS"),
    (re.compile(r"^(?:ATW|NWD|EAW)\b|\bATM\b|CASH\s+WDL|CASH\s+WITHDRAWAL"), "ATM"),
    (re.compile(r"^(?:CRV\s+)?POS\b|^PCD\b|^(?:ME\s+)?DC\s+SI\b|^ECOM\b|\bPOS\s+DEBIT\b"), "CARD"),
    (re.compile(r"^(?:ACH|NACH|ECS)\b|\bNACH\b"), "ACH"),
    (re.compile(r"^(?:CHQ|CHEQUE|CLG|TO\s+CLG|BY\s+CLG|INWARD\s+CLG)\b"), "CHQ"),
    (re.compile(r"^(?:FT|TPT|IB|INB|MB)\b|\bTPT\b"), "TRANSFER"),
]

_NOISE_WORDS = {
    "UPI", "NEFT", "IMPS", "RTGS", "CR", "DR", "P2A", "P2M", "P2P", "POS", "ACH", "NACH", "ECS", "ATW", "NWD",
    "EAW", "TPT", "FT", "MB", "IB", "INB", "BIL", "ONL", "CHQ", "CLG", "TO", "BY", "TRANSFER", "TRF", "PAYMENT",
    "PAYMENTS", "PAY", "DEBIT", "CREDIT", "MOB", "MMT", "NA", "NULL", "COLLECT", "REQUEST", "OID", "REF",
    "NETBANK", "NETBANKING", "INR", "ECOM", "PURCHASE", "PAYMENT FROM PHONE", "UPI PAYMENT", "SENT USING PAYTM U",
    "SENT USING PAYTM UPI", "PAID VIA", "VIA", "MANDATE", "AUTOPAY", "DC", "SI", "ME", "PCD", "CRV", "IN", "IND",
}
_BANK_CODES = {
    "HDFC", "SBIN", "ICIC", "UTIB", "YESB", "KKBK", "CIUB", "PUNB", "BARB", "CNRB", "IDIB", "IOBA", "UBIN",
    "IDFB", "INDB", "FDRL", "KVBL", "TMBL", "AIRP", "PYTM", "JIOP", "AUBL", "RATN", "SIBL", "CBIN", "MAHB",
    "PSIB", "UCBA", "BKID", "IBKL", "ESFB", "FINO", "DBSS", "SCBL", "CITI", "HSBC", "DLXB", "KARB", "CSBK",
    "NSPB", "HDFCBANK", "AXIS", "ICICI", "SBI", "KOTAK", "YES", "PAYTM BANK",
}
# Payment gateways / PSPs whose name hides the real merchant; key on the VPA instead.
_GATEWAYS = (
    "PAYU", "RAZORPAY", "CASHFREE", "BILLDESK", "CCAVENUE", "PHONEPE", "GOOGLE PAY", "GOOGLEPAY", "BHARATPE",
    "PINE LABS", "PINELABS", "JUSPAY", "EASEBUZZ", "ONE97", "PAYTM", "PAYGLOCAL", "INSTAMOJO",
)
_CITIES = {
    "BANGALORE", "BENGALURU", "CHENNAI", "MUMBAI", "DELHI", "NEW DELHI", "HYDERABAD", "PUNE", "KOLKATA", "GURGAON",
    "GURUGRAM", "NOIDA", "COIMBATORE", "MADURAI", "TRICHY", "TIRUCHIRAPPALLI", "KUMBAKONAM", "THANJAVUR", "SALEM",
    "ERODE", "TIRUPUR", "VELLORE", "KOCHI", "AHMEDABAD", "JAIPUR", "LUCKNOW", "INDIA", "MUM", "BLR", "CHN", "DEL",
}

# Words that carry no meaning about what was bought (channel markers, bank codes, cities).
STOP_WORDS = frozenset(_NOISE_WORDS | _BANK_CODES | _CITIES | {"MERUPI", "YBLUPI", "UPIAR", "UPIOUT", "NETBANK"})

_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_VPA_RE = re.compile(r"[A-Z0-9._]{2,}@[A-Z]{2,}")
_MASKED_RE = re.compile(r"[0-9X*]{4,}")
_SUFFIX_RE = re.compile(
    r"(?:\s+|^)(?:PRIVATE\s+LIMITED|PVT\.?\s*LTD\.?|PRIVATE\s+LTD|PRIV\w*|LIMI\w*|LIMITED|LTD\.?|LLP|INC\.?|PVT\.?|CO\.)$"
)
_RRN_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_UTR_RE = re.compile(r"\b[A-Z]{4}[RNH]\d{8,16}\b|\bN\d{9,}\b")


@dataclass(frozen=True)
class Normalized:
    channel: str
    counterparty: str  # human-readable payee/payer
    vpa: str | None
    merchant_key: str
    llm_hint: str  # narration with every number masked, safe to send to the model


def _letters(s: str) -> int:
    return sum(ch.isalpha() for ch in s)


# Words dropped from inside a payee name (whole-token noise like PAY/PAYMENT is kept in names: "AMAZON PAY").
_NAME_NOISE_WORDS = {
    "UPI", "POS", "DEBIT", "ECOM", "PURCHASE", "DC", "SI", "ME", "PCD", "CRV", "IN", "IND", "NA", "INR", "CR", "DR",
    "NEFT", "IMPS", "RTGS", "ACH", "NACH", "ATW", "NWD", "EAW", "AUTOPAY", "MANDATE",
}


def _is_noise_word(word: str) -> bool:
    w = word.strip(" .,;*#()")
    if not w or w in _NAME_NOISE_WORDS:
        return True
    if any(ch.isdigit() for ch in w) and (_MASKED_RE.search(w) or sum(ch.isdigit() for ch in w) / len(w) > 0.4):
        return True
    return bool(re.fullmatch(r"UPI[A-Z]{0,4}", w))


def _is_noise_token(tok: str) -> bool:
    t = tok.strip(" .,;*#()")
    if _letters(t) < 3 or "@" in t or _IFSC_RE.match(t) or t in _NOISE_WORDS or t in _BANK_CODES:
        return True
    digits = sum(ch.isdigit() for ch in t)
    if " " not in t and digits and digits / len(t) > 0.4:
        return True  # a single code-like word; multi-word segments ("12:34:56 1234567890 WWW AMAZON IN") are judged per word
    # Every word is noise (e.g. "416021XXXXXX1234 POS DEBIT")
    return all(_is_noise_word(w) for w in t.split())


def _clean_name(raw: str) -> str:
    words = [w.strip(" .,;*#()") for w in raw.split()]
    words = [w for w in words if w and not _is_noise_word(w) and w != "WWW"]
    # Card terminals often wrap the merchant in a city and the acquiring bank: "BANGALORE SWIGGY ... HDFC BANK"
    while len(words) > 1 and (words[-1] in _CITIES or words[-1] in _BANK_CODES or words[-1] in {"BANK", "BAN", "BA"}):
        words.pop()
    while len(words) > 1 and words[0] in _CITIES:
        words.pop(0)
    name = " ".join(words)
    name = re.sub(r"\d{4,}", " ", name)
    name = re.sub(r"[^A-Z0-9&.' ]", " ", name)
    name = re.sub(r"\s+", " ", name).strip(" .'")
    for _ in range(3):
        stripped = _SUFFIX_RE.sub("", name).strip(" .'")
        if stripped == name or _letters(stripped) < 3:
            break
        name = stripped
    return name[:40].strip()


def detect_channel(text: str) -> str:
    for pattern, channel in _CHANNELS:
        if pattern.search(text):
            return channel
    return "OTHER"


def _strip_prefixes(text: str) -> str:
    for _ in range(4):
        new = _PREFIX_RE.sub("", text, count=1).lstrip(" -/:")
        if new == text:
            break
        text = new
    return text


def mask_for_llm(narration: str) -> str:
    """Replace every run of digits (refs, account/card/phone numbers) with '#'."""
    text = re.sub(r"\s+", " ", narration.upper()).strip()
    text = re.sub(r"[0-9X*]*\d[0-9X*]*", "#", text)
    text = re.sub(r"#(?:[#\s]*#)+", "#", text)
    return text[:120]


def extract_ref(narration: str) -> str | None:
    """Best-effort transaction reference (UPI/IMPS RRN or NEFT/RTGS UTR) from the narration."""
    text = narration.upper()
    m = _UTR_RE.search(text) or _RRN_RE.search(text)
    return m.group(0) if m else None


def normalize(narration: str, direction: str) -> Normalized:
    text = re.sub(r"\s+", " ", (narration or "").upper()).strip()
    channel = detect_channel(text)
    d = "D" if direction == "debit" else "C"

    vpa_match = _VPA_RE.search(text)
    vpa = vpa_match.group(0).lower() if vpa_match else None

    if channel == "ATM":
        return Normalized(channel, "ATM withdrawal", None, f"{d}:ATM CASH", mask_for_llm(text))

    body = _strip_prefixes(text)
    tokens = [t.strip() for t in re.split(r"[-/|:]+|\s{2,}", body)]
    name = ""
    for tok in tokens:
        if not _is_noise_token(tok):
            name = _clean_name(tok)
            if _letters(name) >= 3:
                break
            name = ""
    if not name:
        # Nothing name-like between delimiters: fall back to the first meaningful words.
        name = _clean_name(" ".join(re.split(r"[-/|:]+", body)))
        name = " ".join(name.split()[:4])

    is_gateway = bool(name) and name.startswith(_GATEWAYS)
    if vpa and (not name or is_gateway):
        key_part = vpa.upper()
        counterparty = f"{name} ({vpa})" if name else vpa
    else:
        key_part = name or "UNKNOWN"
        counterparty = name.title() if name else (text[:40] or "Unknown")

    return Normalized(channel, counterparty, vpa, f"{d}:{key_part}", mask_for_llm(text))
