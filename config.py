"""Static configuration: paths, model, default categories and seed keyword rules."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("EXPENSE_TRACKER_DATA", BASE_DIR / "data"))
DB_PATH = Path(os.getenv("EXPENSE_TRACKER_DB", DATA_DIR / "expenses.db"))
INBOX_DIR = DATA_DIR / "inbox"
GMAIL_CREDENTIALS = DATA_DIR / "credentials.json"
GMAIL_TOKEN = DATA_DIR / "gmail_token.json"

HOST = "127.0.0.1"  # never expose financial data beyond this machine
PORT = int(os.getenv("EXPENSE_TRACKER_PORT", "8765"))

# ── Claude ────────────────────────────────────────────────────────────────────
# Claude Haiku 5.5: cheapest current model, built for classification.
MODEL_ID = os.getenv("EXPENSE_TRACKER_MODEL", "claude-haiku-5-5")
# USD per million tokens (prompts <= 100K tokens; ours are ~3K).
PRICE_INPUT_PER_MTOK = 0.10
PRICE_OUTPUT_PER_MTOK = 0.50
LLM_BATCH_SIZE = 50
LLM_MAX_TOKENS = 8000  # thinking counts toward this on Haiku 5.5
LLM_FEW_SHOT_LIMIT = 40
LOW_CONFIDENCE = 0.6

UNCATEGORIZED = "Uncategorized"

# ── Categories ────────────────────────────────────────────────────────────────
# (name, kind, color, description). Descriptions are shown to the model.
DEFAULT_CATEGORIES: list[tuple[str, str, str, str]] = [
    ("Food & Dining", "expense", "#e8590c", "Restaurants, cafes, bakeries, food delivery (Swiggy, Zomato)"),
    ("Groceries", "expense", "#2f9e44", "Supermarkets, kirana stores, quick-commerce (Blinkit, Zepto, Instamart, BigBasket, DMart), milk"),
    ("Shopping", "expense", "#1971c2", "E-commerce, clothing, electronics, home goods (Amazon, Flipkart, Myntra)"),
    ("Transport", "expense", "#0c8599", "Cabs, autos, metro, bus, parking, tolls, FASTag (Uber, Ola, Rapido)"),
    ("Fuel", "expense", "#5c940d", "Petrol pumps (IOCL, HPCL, BPCL, Shell)"),
    ("Bills & Utilities", "expense", "#6741d9", "Electricity, water, cooking gas, mobile recharge, broadband, DTH"),
    ("Rent", "expense", "#862e9c", "House rent"),
    ("Household & Services", "expense", "#a61e4d", "Maid, cook, repairs, laundry, home services (Urban Company)"),
    ("Health", "expense", "#c92a2a", "Hospitals, clinics, pharmacies, diagnostics (Apollo, 1mg, PharmEasy)"),
    ("Insurance", "expense", "#364fc7", "Life, health and motor insurance premiums (LIC, Star Health, Acko)"),
    ("Education", "expense", "#087f5b", "School/college fees, courses, books"),
    ("Entertainment & Subscriptions", "expense", "#d6336c", "Movies, OTT and music subscriptions (Netflix, Hotstar, Spotify), BookMyShow, games, app stores"),
    ("Travel", "expense", "#1098ad", "Flights, trains (IRCTC), long-distance buses, hotels (MakeMyTrip, redBus, OYO)"),
    ("Personal Care", "expense", "#f08c00", "Salon, spa, gym, fitness, cosmetics"),
    ("Gifts & Donations", "expense", "#e64980", "Gifts, charity, temple donations"),
    ("EMI & Loans", "expense", "#495057", "Loan EMIs, BNPL and consumer-finance repayments (Bajaj Finance)"),
    ("Bank Charges", "expense", "#868e96", "Bank fees, SMS/annual card charges, penalties, GST on charges"),
    ("Cash Withdrawal", "expense", "#74b816", "ATM cash withdrawals"),
    ("Taxes", "expense", "#5f3dc4", "Income tax, advance tax, property tax, GST payments"),
    ("Payments to People", "expense", "#fd7e14", "UPI/IMPS/NEFT transfers to individuals when the purpose is unknown"),
    ("Other", "expense", "#adb5bd", "Expenses that fit no other category"),
    ("Salary", "income", "#2b8a3e", "Salary credits from an employer"),
    ("Interest", "income", "#37b24d", "Savings account or FD interest credited"),
    ("Refunds & Cashback", "income", "#40c057", "Refunds, reversals, cashback credits"),
    ("Other Income", "income", "#69db7c", "Money received that is not salary, interest or a refund (incl. from individuals)"),
    ("Self Transfer", "transfer", "#748ffc", "Moving money between your own accounts"),
    ("Credit Card Payment", "transfer", "#91a7ff", "Paying a credit card bill (incl. via CRED)"),
    ("Investments", "transfer", "#4c6ef5", "Mutual funds, SIPs, stocks, NPS, PPF, FDs (Zerodha, Groww, ICCL)"),
    (UNCATEGORIZED, "expense", "#ced4da", "Could not be determined"),
]

# Sub-categories: (parent, [(name, description)]). Claude picks the most specific one that fits;
# reports roll them up into the parent and let you drill down.
SUBCATEGORIES: dict[str, list[tuple[str, str]]] = {
    "Food & Dining": [
        ("Food Delivery", "Swiggy, Zomato and other food delivery apps"),
        ("Restaurants & Cafes", "Eating out: restaurants, messes, cafes, bakeries, tea and snack stalls"),
    ],
    "Groceries": [
        ("Online & Quick Commerce", "Blinkit, Zepto, Swiggy Instamart, BigBasket, JioMart"),
        ("Supermarkets & Kirana", "DMart, Reliance Fresh, More, local provision stores"),
        ("Milk & Daily Needs", "Milk, vegetables, fruits, daily essentials"),
    ],
    "Shopping": [
        ("Online Shopping", "Amazon, Flipkart, Meesho and other marketplaces"),
        ("Clothing & Footwear", "Myntra, Ajio, textiles, garments, footwear"),
        ("Electronics & Gadgets", "Croma, Reliance Digital, mobiles, gadgets"),
        ("Home & Furniture", "IKEA, furniture, home goods, kitchenware"),
    ],
    "Transport": [
        ("Cabs & Autos", "Uber, Ola, Rapido, autos, taxis"),
        ("Public Transport", "Metro, bus, local train"),
        ("Vehicle Upkeep", "Servicing, repairs, tyres, washing, vehicle accessories"),
        ("Parking & Tolls", "Parking, FASTag, tolls"),
    ],
    "Bills & Utilities": [
        ("Electricity", "Electricity board bills (TANGEDCO, BESCOM...)"),
        ("Mobile & Internet", "Mobile recharges, postpaid, broadband"),
        ("Gas & Water", "Cooking gas, piped gas, water"),
        ("DTH & Cable", "Tata Play, Dish TV, Sun Direct, cable"),
    ],
    "Health": [
        ("Pharmacy", "Medicines: pharmacies, 1mg, PharmEasy, MedPlus"),
        ("Doctor & Hospital", "Consultations, hospitals, clinics, dental"),
        ("Diagnostics", "Labs, scans, tests"),
    ],
    "Entertainment & Subscriptions": [
        ("OTT & Music", "Netflix, Hotstar, Prime Video, Spotify, YouTube Premium"),
        ("Movies & Events", "BookMyShow, PVR, INOX, concerts, events"),
        ("Apps & Games", "App stores, in-app purchases, games, software subscriptions"),
    ],
    "Travel": [
        ("Flights", "IndiGo, Air India, Akasa and other airlines"),
        ("Trains & Buses", "IRCTC, redBus, long-distance buses"),
        ("Hotels & Stays", "OYO, Airbnb, hotels, lodges"),
    ],
    "Household & Services": [
        ("Domestic Help", "Maid, cook, driver, caretaker"),
        ("Repairs & Services", "Urban Company, plumber, electrician, carpenter, cleaning"),
        ("Laundry", "Laundry, dry cleaning, ironing"),
    ],
    "Personal Care": [
        ("Salon & Grooming", "Salon, spa, beauty parlour, cosmetics"),
        ("Fitness", "Gym, cult.fit, sports, yoga"),
    ],
    "Investments": [
        ("Mutual Funds & SIPs", "Mutual fund SIPs and purchases (ICCL, CAMS, KFintech)"),
        ("Stocks", "Shares via brokers (Zerodha, Upstox)"),
        ("Deposits & Schemes", "FD, RD, PPF, NPS, gold schemes"),
    ],
}

# Needs / wants / savings grouping for the 50-30-20 view. Sub-categories inherit their parent's group.
CATEGORY_GROUPS: dict[str, list[str]] = {
    "needs": ["Groceries", "Rent", "Bills & Utilities", "Health", "Insurance", "EMI & Loans", "Education",
              "Transport", "Fuel", "Household & Services", "Taxes", "Bank Charges"],
    "wants": ["Food & Dining", "Shopping", "Entertainment & Subscriptions", "Travel", "Personal Care",
              "Gifts & Donations", "Payments to People", "Cash Withdrawal", "Other", UNCATEGORIZED],
    "savings": ["Investments"],
    "income": ["Salary", "Interest", "Refunds & Cashback", "Other Income"],
    "transfer": ["Self Transfer", "Credit Card Payment"],
}

# Money plan: one call, so use a stronger model than the categorizer.
PLAN_MODEL = os.getenv("EXPENSE_TRACKER_PLAN_MODEL", "claude-opus-5-5")

# ── Local (offline) categorizer ───────────────────────────────────────────────
# Business-type words common in Indian payee names and UPI remarks. The local
# categorizer treats each word as weak evidence for a category; your own
# corrections count for much more. Words match whole words in the payee name,
# UPI ID or remark. (category, direction, words)
LOCAL_LEXICON: list[tuple[str, str, list[str]]] = [
    ("Groceries", "debit", ["STORES", "STORE", "SUPERMARKET", "MART", "KIRANA", "PROVISIONS", "PROVISION", "FRUITS", "VEGETABLES", "VEGETABLE", "DAIRY", "MILK", "TRADERS", "GROCERY", "GROCERIES", "DEPARTMENTAL"]),
    ("Food & Dining", "debit", ["MESS", "HOTEL", "BHAVAN", "CAFE", "RESTAURANT", "BIRYANI", "SWEETS", "BAKERY", "BAKES", "TEA", "COFFEE", "JUICE", "CANTEEN", "DHABA", "FOODS", "FOOD", "KITCHEN", "PIZZA", "CHICKEN", "TIFFIN", "ICECREAM", "DARSHINI", "SNACKS", "LUNCH", "DINNER", "BREAKFAST", "PARCEL"]),
    ("Health", "debit", ["MEDICAL", "MEDICALS", "PHARMACY", "PHARMA", "CLINIC", "HOSPITAL", "HOSPITALS", "DENTAL", "DIAGNOSTICS", "DIAGNOSTIC", "LABS", "HEALTHCARE", "OPTICALS", "EYE", "MEDICINE", "MEDICINES", "DOCTOR", "NURSING"]),
    ("Personal Care", "debit", ["SALON", "SPA", "BEAUTY", "PARLOUR", "PARLOR", "TAILOR", "TAILORS", "TAILORING", "STITCHING", "GYM", "FITNESS", "UNISEX", "HAIRCUT", "COSMETICS"]),
    ("Shopping", "debit", ["TEXTILES", "TEXTILE", "SILKS", "FASHION", "FASHIONS", "GARMENTS", "FOOTWEAR", "ELECTRONICS", "MOBILES", "BOUTIQUE", "JEWELLERS", "JEWELLERY", "COLLECTIONS", "STATIONERY", "STATIONERS", "GIFTS", "TOYS", "FURNITURE"]),
    ("Transport", "debit", ["AUTO", "MOTORS", "GARAGE", "TYRES", "TYRE", "SERVICE CENTER", "CABS", "TAXI", "PARKING", "TOLL", "BIKE", "WORKSHOP"]),
    ("Fuel", "debit", ["PETROL", "FUELS", "FILLING", "BUNK", "PETROLEUM", "DIESEL"]),
    ("Household & Services", "debit", ["ELECTRICALS", "HARDWARE", "HARDWARES", "PLUMBING", "PLUMBER", "ELECTRICIAN", "LAUNDRY", "DRYCLEAN", "IRONING", "CARPENTER", "PAINTS", "MAID", "COOK", "SERVANT", "REPAIR", "REPAIRS", "CLEANING", "WATERCAN"]),
    ("Bills & Utilities", "debit", ["ELECTRICITY", "BROADBAND", "FIBERNET", "FIBER", "CABLE", "DTH", "RECHARGE", "PREPAID", "POSTPAID", "EB", "WATER", "GAS"]),
    ("Gifts & Donations", "debit", ["FLOWER", "FLOWERS", "POOJA", "PUJA", "TEMPLE", "TRUST", "DEVASTHANAM", "KOVIL", "CHURCH", "MOSQUE", "DONATION", "CHARITY", "GIFT", "OFFERING"]),
    ("Education", "debit", ["SCHOOL", "COLLEGE", "ACADEMY", "TUITION", "COACHING", "INSTITUTE", "CLASSES", "BOOKS", "BOOK", "FEES", "EXAM"]),
    ("Travel", "debit", ["TRAVELS", "TOURS", "TOURISM", "HOLIDAYS", "RESORT", "RESORTS", "LODGE", "RESIDENCY", "INN", "TICKET", "TICKETS"]),
    ("Entertainment & Subscriptions", "debit", ["CINEMAS", "CINEMA", "THEATRE", "THEATER", "GAMES", "GAMING", "MOVIE", "SUBSCRIPTION"]),
    ("Rent", "debit", ["RENT", "RENTAL", "HOUSE RENT", "ADVANCE"]),
    ("Insurance", "debit", ["INSURANCE", "ASSURANCE", "PREMIUM"]),
    ("EMI & Loans", "debit", ["FINANCE", "FINSERV", "FINANCIAL", "CAPITAL", "LOAN", "EMI", "CREDIT"]),
    ("Investments", "any", ["SECURITIES", "BROKING", "MUTUAL", "ASSET", "INVESTMENT", "INVESTMENTS"]),
    ("Salary", "credit", ["TECHNOLOGIES", "TECHNOLOGY", "SOLUTIONS", "SOFTWARE", "INFOTECH", "SYSTEMS", "CONSULTING", "CONSULTANCY", "LABS", "PAYROLL", "SALARY", "SAL"]),
    ("Refunds & Cashback", "credit", ["REFUND", "REVERSAL", "CASHBACK", "RETURN"]),
]

# ── Seed rules ────────────────────────────────────────────────────────────────
# Keyword rules shipped with the app so common merchants never need an LLM call.
# Evaluated in this order (first match wins), so specific entries come first.
# Keywords match whole words in the upper-cased narration (an optional trailing
# "S" is allowed). Direction None = debit or credit.
SEED_RULES: list[tuple[str, str | None, list[str]]] = [
    ("Refunds & Cashback", "credit", ["REFUND", "RFND", "REVERSAL", "CASHBACK", "CRV POS"]),
    ("Interest", "credit", ["INT.PD", "INT PD", "INTEREST PAID", "INTEREST CREDIT", "SB INT", "CREDIT INTEREST", "INT CREDIT"]),
    ("Salary", "credit", ["SALARY", "SAL CREDIT", "PAYROLL"]),
    ("Credit Card Payment", None, ["CC PAYMENT", "CCPAYMENT", "CREDIT CARD PAYMENT", "CARD BILL", "CRED CLUB", "CREDCLUB", "DREAMPLUG", "PAYMENT RECEIVED THANK YOU"]),
    ("Credit Card Payment", "debit", ["CRED"]),
    ("Bank Charges", "debit", ["CHARGES", "CHRGS", "CHGS", "SMS CHG", "ANNUAL FEE", "MIN BAL", "CONSOLIDATED CHARGES", "DEBIT CARD FEE"]),
    ("Cash Withdrawal", "debit", ["ATW", "NWD", "EAW", "ATM WDL", "ATM CASH", "CASH WDL", "CASH WITHDRAWAL", "ATM WITHDRAWAL"]),
    ("Taxes", "debit", ["INCOME TAX", "CBDT", "TIN NSDL", "ADVANCE TAX", "PROPERTY TAX", "GST PAYMENT"]),
    ("Mutual Funds & SIPs", None, ["ICCL", "INDIAN CLEARING", "MUTUAL FUND", "MUTUALFUND", "SIP", "CAMS", "KFINTECH", "KUVERA", "ETMONEY", "PAYTM MONEY"]),
    ("Deposits & Schemes", None, ["NPS TRUST", "PPF"]),
    ("Investments", None, ["ZERODHA", "GROWW", "NEXTBILLION", "UPSTOX", "SMALLCASE", "BSE LIMITED"]),
    ("Insurance", "debit", ["LIC", "LICI", "LIFE INSURANCE", "INSURANCE", "POLICYBAZAAR", "STAR HEALTH", "HDFC ERGO", "ICICI LOMBARD", "ACKO", "GO DIGIT"]),
    ("EMI & Loans", "debit", ["EMI", "LOAN", "BAJAJ FINANCE", "BAJAJ FIN", "HOME CREDIT"]),
    ("Online & Quick Commerce", "debit", ["INSTAMART", "BIGBASKET", "BLINKIT", "GROFERS", "ZEPTO", "JIOMART"]),
    ("Supermarkets & Kirana", "debit", ["DMART", "AVENUE SUPERMARTS", "MORE RETAIL", "SPENCERS", "RELIANCE FRESH", "RELIANCE SMART", "NATURES BASKET", "SUPERMARKET"]),
    ("Milk & Daily Needs", "debit", ["COUNTRY DELIGHT", "MILKBASKET"]),
    ("Food Delivery", "debit", ["SWIGGY", "ZOMATO", "EATCLUB", "BOX8", "FAASOS"]),
    ("Restaurants & Cafes", "debit", ["DOMINOS", "MCDONALDS", "KFC", "STARBUCKS", "PIZZA HUT", "BURGER KING", "HALDIRAM", "ADYAR ANANDA BHAVAN", "SARAVANA BHAVAN", "RESTAURANT", "CAFE", "BAKERY", "HOTEL"]),
    ("Fuel", "debit", ["IOCL", "INDIAN OIL", "HPCL", "HINDUSTAN PETROLEUM", "BPCL", "BHARAT PETROLEUM", "SHELL", "NAYARA", "PETROL", "FUEL", "FILLING STATION"]),
    ("Cabs & Autos", "debit", ["UBER", "OLA", "OLACABS", "ANI TECHNOLOGIES", "RAPIDO", "ROPPEN", "NAMMA YATRI", "BLUSMART"]),
    ("Public Transport", "debit", ["METRO RAIL", "CMRL", "BMRCL", "DMRC"]),
    ("Parking & Tolls", "debit", ["FASTAG", "PARKING", "TOLL"]),
    ("Flights", "debit", ["INDIGO", "INTERGLOBE", "AIR INDIA", "AKASA", "SPICEJET", "VISTARA"]),
    ("Trains & Buses", "debit", ["IRCTC", "REDBUS"]),
    ("Hotels & Stays", "debit", ["OYO", "AIRBNB", "BOOKING.COM", "AGODA"]),
    ("Travel", "debit", ["MAKEMYTRIP", "GOIBIBO", "CLEARTRIP", "YATRA", "IXIGO"]),
    ("Electricity", "debit", ["TANGEDCO", "TNEB", "TNPDCL", "BESCOM", "MSEDCL", "ELECTRICITY"]),
    ("Mobile & Internet", "debit", ["AIRTEL", "JIO", "RELIANCE JIO", "VODAFONE", "BSNL", "ACT FIBERNET", "HATHWAY", "RECHARGE"]),
    ("DTH & Cable", "debit", ["TATA PLAY", "TATASKY", "DISH TV", "SUN DIRECT"]),
    ("Gas & Water", "debit", ["INDANE", "BHARATGAS", "HP GAS", "WATER BOARD", "METROWATER"]),
    ("Bills & Utilities", "debit", ["BBPS"]),
    ("Pharmacy", "debit", ["PHARMEASY", "1MG", "NETMEDS", "MEDPLUS", "PHARMACY", "MEDICAL", "PHARMA"]),
    ("Diagnostics", "debit", ["DIAGNOSTIC"]),
    ("Doctor & Hospital", "debit", ["HOSPITAL", "CLINIC"]),
    ("Health", "debit", ["APOLLO"]),
    ("OTT & Music", "debit", ["NETFLIX", "SPOTIFY", "HOTSTAR", "DISNEY", "AMAZON PRIME", "PRIMEVIDEO", "PRIME VIDEO", "YOUTUBE", "SONYLIV", "ZEE5", "JIOCINEMA", "JIOHOTSTAR"]),
    ("Movies & Events", "debit", ["BOOKMYSHOW", "PVR", "INOX"]),
    ("Apps & Games", "debit", ["GOOGLE PLAY", "GOOGLEPLAY", "APPLE MEDIA", "APPLE.COM", "APPLE SERVICES"]),
    ("Clothing & Footwear", "debit", ["MYNTRA", "AJIO"]),
    ("Electronics & Gadgets", "debit", ["CROMA", "RELIANCE DIGITAL"]),
    ("Home & Furniture", "debit", ["IKEA"]),
    ("Online Shopping", "debit", ["AMAZON", "FLIPKART", "MEESHO", "TATA CLIQ", "FIRSTCRY", "NYKAA"]),
    ("Shopping", "debit", ["DECATHLON", "LENSKART"]),
    ("Salon & Grooming", "debit", ["SALON", "SPA", "GREEN TRENDS", "NATURALS SALON"]),
    ("Fitness", "debit", ["CULTFIT", "CULT.FIT", "CUREFIT", "GYM"]),
    ("Education", "debit", ["SCHOOL", "COLLEGE", "UNIVERSITY", "UDEMY", "COURSERA", "BYJU", "UNACADEMY"]),
]
