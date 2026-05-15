"""niches.py — Niche configuration for AutoPost multi-niche support.

Each niche defines:
  - RSS feed sources
  - Google News search queries
  - Tone detection keywords (positive / negative)
  - Hashtags per tone
  - CTA text for X and Telegram
  - Image background styles per mood (positive / negative / neutral)

Usage:
    from src.agents.niches import get_niche, NICHE_IDS
    cfg = get_niche("fitness")
    feeds = cfg["feeds"]
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Niche catalogue
# ---------------------------------------------------------------------------

NICHES: dict[str, dict] = {

    # ── CRYPTO (original — preserved exactly) ───────────────────────────────
    "crypto": {
        "label": "Crypto & Web3",
        "icon": "₿",
        "color": "#f59e0b",
        "feeds": [],          # news_scout uses its own built-in crypto feeds
        "gnews_queries": [],  # news_scout builds these from CoinGecko trending
        "tone_positive": {
            "record", "all-time high", "ath", "surge", "rally", "approval",
            "approved", "launch", "inflow", "partnership", "upgrade",
            "accumulation", "bullish", "breakout", "milestone", "etf",
            "institutional", "buy", "buys", "purchased",
        },
        "tone_negative": {
            "hack", "exploit", "drain", "stolen", "breach", "crash", "plunge",
            "liquidation", "liquidated", "lawsuit", "rejected", "collapse",
            "ban", "banned", "fraud", "arrest", "indicted", "scam",
            "rug pull", "attack",
        },
        "hashtags": {
            "positive": ["#BullMarket", "#CryptoRally", "#ToTheMoon", "#CryptoBull", "#CryptoNews"],
            "negative": ["#CryptoCrash", "#BearMarket", "#CryptoAlert", "#RiskOff", "#CryptoNews"],
            "neutral":  ["#CryptoUpdate", "#MarketWatch", "#OnChain", "#CryptoNews", "#Blockchain"],
            "universal": ["#Crypto", "#Web3", "#DYOR", "#CryptoTrader", "#DigitalAssets", "#EliteMarginDesk"],
        },
        "cta_x":  "👉 Follow @EliteMarginDesk for daily crypto alpha",
        "cta_tg": "\n\n🔔 Follow the channel for daily crypto updates\n🌐 https://www.elitemargindesk.io?ref=7UBN23I0\n💬 @CryptohEMD",
        "image_styles": {},   # image_gen uses its own BG_STYLES for crypto
    },

    # ── FITNESS & HEALTH ────────────────────────────────────────────────────
    "fitness": {
        "label": "Fitness & Health",
        "icon": "💪",
        "color": "#22c55e",
        "feeds": [
            ("menshealth",   "https://www.menshealth.com/rss/all.xml",              2),
            ("shape",        "https://www.shape.com/feeds/all",                     2),
            ("healthline",   "https://www.healthline.com/rss/news",                 2),
            ("womenshealth", "https://www.womenshealthmag.com/rss/all.xml",         2),
            ("runnersworld", "https://www.runnersworld.com/rss/all.xml",            3),
            ("selfmag",      "https://www.self.com/feed/rss",                       3),
        ],
        "gnews_queries": [
            "fitness workout tips", "nutrition health diet", "muscle building exercise",
        ],
        "tone_positive": {
            "gain", "gains", "improve", "achieve", "best", "record", "peak",
            "transform", "strong", "boost", "increase", "effective", "proven",
            "success", "growth", "progress",
        },
        "tone_negative": {
            "injury", "dangerous", "risk", "avoid", "harmful", "banned",
            "recall", "warning", "overdose", "death", "disease", "illness",
        },
        "keywords": {
            "workout", "exercise", "training", "muscle", "gym", "fitness", "diet",
            "nutrition", "calories", "protein", "carbs", "fat", "cardio", "yoga",
            "running", "marathon", "strength", "weight loss", "weight gain",
            "body fat", "supplement", "recovery", "endurance", "hiit", "squat",
            "deadlift", "abs", "core", "stretching", "sleep", "stress",
            "mental health", "hydration", "fiber", "vitamins", "resistance",
            "mobility", "flexibility", "reps", "sets", "burn", "lean", "tone",
            "athletic", "sport", "sweat", "rest day", "meal prep", "macros",
            "intermittent fasting", "keto", "vegan", "plant-based", "bulk",
            "cut", "physique", "body composition", "heart rate", "vo2 max",
            "trainer", "coach", "gym class", "crossfit", "pilates", "swimming",
            "cycling", "rowing", "plyometric", "dumbbell", "barbell", "kettlebell",
        },
        "hashtags": {
            "positive": ["#FitLife", "#GainsTrain", "#Gains", "#Motivation", "#FitnessGoals"],
            "negative": ["#InjuryPrevention", "#FitnessWarning", "#HealthAlert", "#SafeWorkout"],
            "neutral":  ["#FitnessTips", "#HealthyLiving", "#WorkoutTips", "#NutritionFacts"],
            "universal": ["#Fitness", "#Health", "#Workout", "#Wellness", "#Gym", "#HealthyLifestyle"],
        },
        "cta_x":  "👉 Follow for daily fitness & health tips",
        "cta_tg": "\n\n🔔 Follow for daily fitness & health content",
    },

    # ── BUSINESS & ENTREPRENEURSHIP ─────────────────────────────────────────
    "business": {
        "label": "Business & Entrepreneurship",
        "icon": "📊",
        "color": "#3b82f6",
        "feeds": [
            ("entrepreneur",  "https://www.entrepreneur.com/latest.rss",              1),
            ("inc",           "https://www.inc.com/rss.html",                         1),
            ("fastcompany",   "https://www.fastcompany.com/latest/rss",               1),
            ("forbes_biz",    "https://www.forbes.com/real-time/feed2/",              2),
            ("wsj_biz",       "https://feeds.a.dj.com/rss/WSJcomUSBusiness.xml",      1),
            ("hbr",           "https://hbr.org/feed/",                                2),
        ],
        "gnews_queries": [
            "startup funding entrepreneurship", "business strategy leadership",
            "company earnings revenue growth",
        ],
        "tone_positive": {
            "funding", "raised", "growth", "profit", "record", "launch", "ipo",
            "acquisition", "partnership", "expansion", "revenue", "milestone",
            "valuation", "unicorn", "success", "breakthrough",
        },
        "tone_negative": {
            "layoffs", "bankruptcy", "loss", "fraud", "lawsuit", "scandal",
            "collapse", "shutdown", "fired", "crash", "decline", "failure",
        },
        "keywords": {
            "startup", "company", "ceo", "founder", "co-founder", "investor",
            "venture capital", "vc", "series a", "series b", "series c",
            "seed round", "angel", "private equity", "market share", "customers",
            "employees", "workforce", "strategy", "leadership", "management",
            "quarterly", "earnings", "sales", "business", "entrepreneur",
            "industry", "product", "service", "hiring", "economy", "enterprise",
            "b2b", "b2c", "saas", "corporate", "retail", "supply chain",
            "productivity", "remote work", "hybrid", "culture", "innovation",
            "disruption", "scale", "pivot", "exit", "merger", "acquisition",
            "board", "shareholders", "stock", "equity", "operations",
            "manufacturing", "logistics", "branding", "marketing", "pricing",
        },
        "hashtags": {
            "positive": ["#StartupSuccess", "#BusinessGrowth", "#Entrepreneurship", "#Funding", "#Startup"],
            "negative": ["#BusinessAlert", "#Layoffs", "#StartupFail", "#MarketCrash"],
            "neutral":  ["#BusinessNews", "#Leadership", "#Strategy", "#BusinessTips"],
            "universal": ["#Business", "#Entrepreneur", "#CEO", "#Productivity", "#Innovation", "#Success"],
        },
        "cta_x":  "👉 Follow for daily business & startup insights",
        "cta_tg": "\n\n🔔 Follow for daily business & entrepreneurship content",
    },

    # ── FOOD & COOKING ───────────────────────────────────────────────────────
    "food": {
        "label": "Food & Cooking",
        "icon": "🍕",
        "color": "#f97316",
        "feeds": [
            ("seriouseats",  "https://www.seriouseats.com/atom.xml",                  2),
            ("bonappetit",   "https://www.bonappetit.com/feed/rss",                   2),
            ("eater",        "https://www.eater.com/rss/index.xml",                   2),
            ("foodnetwork",  "https://www.foodnetwork.com/fn-dish/feed.rss",          3),
            ("foodandwine",  "https://www.foodandwine.com/feed",                      2),
        ],
        "gnews_queries": [
            "food recipes cooking trends", "restaurant review food industry",
            "nutrition food science",
        ],
        "tone_positive": {
            "delicious", "perfect", "best", "award", "trending", "viral",
            "popular", "healthy", "fresh", "organic", "farm-to-table", "gourmet",
        },
        "tone_negative": {
            "recall", "contamination", "outbreak", "salmonella", "listeria",
            "ban", "dangerous", "warning", "allergy", "toxic", "unsafe",
        },
        "keywords": {
            "recipe", "restaurant", "chef", "cooking", "cook", "ingredients",
            "meal", "dish", "eat", "eating", "drink", "wine", "beer", "cocktail",
            "spirits", "coffee", "tea", "kitchen", "flavor", "taste", "bake",
            "baking", "roast", "grill", "grilled", "fry", "fried", "sauteed",
            "cuisine", "menu", "brunch", "dinner", "lunch", "breakfast", "snack",
            "dessert", "bread", "pasta", "sauce", "salad", "soup", "steak",
            "seafood", "vegan", "vegetarian", "gluten-free", "spice", "herb",
            "fermented", "marinated", "braised", "smoked", "raw", "farm",
            "harvest", "seasonal", "local", "street food", "food truck",
            "michelin", "fast food", "takeout", "delivery", "foodie",
        },
        "hashtags": {
            "positive": ["#Foodie", "#FoodPorn", "#Delicious", "#TastingMenu", "#GourmetFood"],
            "negative": ["#FoodSafety", "#FoodRecall", "#FoodAlert", "#HealthWarning"],
            "neutral":  ["#FoodTips", "#CookingTips", "#Recipe", "#HomeChef", "#MealPrep"],
            "universal": ["#Food", "#Cooking", "#Recipes", "#Foodstagram", "#Chef", "#EatWell"],
        },
        "cta_x":  "👉 Follow for daily food & recipe inspiration",
        "cta_tg": "\n\n🔔 Follow for daily food & cooking content",
    },

    # ── FASHION & STYLE ──────────────────────────────────────────────────────
    "fashion": {
        "label": "Fashion & Style",
        "icon": "👗",
        "color": "#ec4899",
        "feeds": [
            ("vogue",        "https://www.vogue.com/feed/rss",                        1),
            ("highsnobiety", "https://www.highsnobiety.com/feed/",                    2),
            ("hypebeast",    "https://hypebeast.com/feed",                            2),
            ("wwd",          "https://wwd.com/feed/",                                 1),
            ("elle",         "https://www.elle.com/rss/all.xml",                      2),
        ],
        "gnews_queries": [
            "fashion trends style 2025", "luxury brand collection release",
            "streetwear sneakers drop",
        ],
        "tone_positive": {
            "trending", "iconic", "launch", "drop", "collab", "sold out",
            "bestseller", "award", "new collection", "debut", "exclusive",
        },
        "tone_negative": {
            "controversy", "backlash", "cancel", "offensive", "copy",
            "counterfeit", "plagiarism", "scandal", "boycott",
        },
        "keywords": {
            "designer", "collection", "runway", "outfit", "clothing", "wear",
            "brand", "sneaker", "sneakers", "shoe", "shoes", "jacket", "coat",
            "dress", "shirt", "pants", "trousers", "skirt", "jeans", "denim",
            "luxury", "streetwear", "model", "fashion week", "wardrobe",
            "lookbook", "editorial", "capsule", "couture", "haute couture",
            "accessories", "bag", "handbag", "jewellery", "jewelry", "watch",
            "sunglasses", "style", "look", "trend", "vogue", "elle", "wwd",
            "resort collection", "spring summer", "fall winter", "aw25", "ss25",
            "menswear", "womenswear", "unisex", "collaboration", "limited edition",
            "restock", "colourway", "silhouette", "fabric", "textile", "leather",
            "sustainable fashion", "fast fashion", "vintage", "thrift", "bespoke",
            "atelier", "maison", "fashion show", "catwalk", "front row",
        },
        "hashtags": {
            "positive": ["#FashionWeek", "#NewCollection", "#StyleGoals", "#FashionDrop", "#OOTD"],
            "negative": ["#FashionAlert", "#StyleMiss", "#FashionControversy"],
            "neutral":  ["#FashionTips", "#StyleInspiration", "#FashionAdvice", "#StyleTips"],
            "universal": ["#Fashion", "#Style", "#OOTD", "#Streetwear", "#Luxury", "#FashionBlogger"],
        },
        "cta_x":  "👉 Follow for daily fashion & style inspiration",
        "cta_tg": "\n\n🔔 Follow for daily fashion & style content",
    },

    # ── TRAVEL ───────────────────────────────────────────────────────────────
    "travel": {
        "label": "Travel & Adventure",
        "icon": "✈️",
        "color": "#06b6d4",
        "feeds": [
            ("lonelyplanet",  "https://www.lonelyplanet.com/blog/feed/rss",           2),
            ("natgeo_travel", "https://www.nationalgeographic.com/travel/rss/",       1),
            ("cntraveler",    "https://www.cntraveler.com/feed/rss",                  1),
            ("afar",          "https://www.afar.com/rss",                             2),
            ("travelleisure", "https://www.travelandleisure.com/rss",                 2),
        ],
        "gnews_queries": [
            "travel destination 2025 tips", "best places visit tourism",
            "travel deal flight hotel",
        ],
        "tone_positive": {
            "best", "paradise", "stunning", "breathtaking", "luxury", "hidden gem",
            "must-visit", "bucket list", "magical", "epic", "affordable", "deal",
        },
        "tone_negative": {
            "dangerous", "travel warning", "ban", "disaster", "hurricane",
            "strike", "cancel", "delay", "scam", "unsafe", "avoid",
        },
        "keywords": {
            "destination", "hotel", "flight", "trip", "vacation", "journey",
            "tour", "guide", "beach", "mountain", "resort", "airline", "airport",
            "passport", "visa", "booking", "itinerary", "sightseeing", "attraction",
            "backpack", "explore", "city break", "road trip", "cruise", "island",
            "country", "travel tips", "travel guide", "hostel", "airbnb",
            "transit", "layover", "backpacker", "solo travel", "family travel",
            "honeymoon", "safari", "camping", "hiking", "ski", "surf", "dive",
            "snorkeling", "national park", "heritage", "culture", "cuisine",
            "local", "off the beaten path", "travel hack", "budget travel",
            "first class", "business class", "lounge", "miles", "points",
            "travel insurance", "luggage", "packing", "jet lag", "time zone",
        },
        "hashtags": {
            "positive": ["#TravelGoals", "#Wanderlust", "#TravelInspiration", "#BucketList", "#Paradise"],
            "negative": ["#TravelWarning", "#TravelAlert", "#TravelSafety", "#TravelBan"],
            "neutral":  ["#TravelTips", "#TravelGuide", "#TravelHack", "#TravelPlanning"],
            "universal": ["#Travel", "#Explore", "#Adventure", "#Wanderlust", "#TravelPhotography", "#TravelBlogger"],
        },
        "cta_x":  "👉 Follow for daily travel inspiration & tips",
        "cta_tg": "\n\n🔔 Follow for daily travel & adventure content",
    },

    # ── TECHNOLOGY & AI ──────────────────────────────────────────────────────
    "technology": {
        "label": "Technology & AI",
        "icon": "🤖",
        "color": "#8b5cf6",
        "feeds": [
            ("techcrunch",  "https://feeds.feedburner.com/TechCrunch",                1),
            ("theverge",    "https://www.theverge.com/rss/index.xml",                 1),
            ("wired",       "https://www.wired.com/feed/rss",                         1),
            ("arstechnica", "https://feeds.arstechnica.com/arstechnica/index",        2),
            ("mit_tech",    "https://www.technologyreview.com/feed/",                 1),
        ],
        "gnews_queries": [
            "artificial intelligence AI breakthrough 2025", "tech startup funding product launch",
            "software developer tools release",
        ],
        "tone_positive": {
            "launch", "breakthrough", "record", "funding", "open source", "release",
            "milestone", "approved", "partnership", "acquisition", "ipo", "growth",
        },
        "tone_negative": {
            "breach", "hack", "vulnerability", "lawsuit", "ban", "antitrust",
            "layoffs", "crash", "outage", "scandal", "privacy", "leak",
        },
        "keywords": {
            "software", "hardware", "app", "device", "platform", "data", "cloud",
            "ai", "artificial intelligence", "machine learning", "deep learning",
            "algorithm", "programming", "developer", "code", "coding", "update",
            "feature", "product", "tech", "digital", "mobile", "computer",
            "model", "llm", "gpt", "chatgpt", "openai", "anthropic", "google",
            "apple", "microsoft", "meta", "amazon", "nvidia", "chip", "gpu",
            "robot", "robotics", "automation", "smartphone", "laptop", "tablet",
            "wearable", "iot", "internet of things", "5g", "quantum", "blockchain",
            "cybersecurity", "encryption", "api", "saas", "open source",
            "github", "startup", "tech startup", "silicon valley", "big tech",
            "neural network", "transformer", "inference", "fine-tuning",
        },
        "hashtags": {
            "positive": ["#TechNews", "#Innovation", "#AIBreakthrough", "#TechLaunch", "#StartupNews"],
            "negative": ["#TechAlert", "#CyberSecurity", "#DataBreach", "#TechFail", "#Outage"],
            "neutral":  ["#TechUpdate", "#Programming", "#SoftwareDev", "#AITools", "#DevNews"],
            "universal": ["#Tech", "#AI", "#ArtificialIntelligence", "#Technology", "#Coding", "#Developer"],
        },
        "cta_x":  "👉 Follow for daily tech & AI news",
        "cta_tg": "\n\n🔔 Follow for daily technology & AI content",
    },

    # ── MARKETING & SOCIAL MEDIA ────────────────────────────────────────────
    "marketing": {
        "label": "Marketing & Social Media",
        "icon": "📣",
        "color": "#14b8a6",
        "feeds": [
            ("hubspot",      "https://blog.hubspot.com/marketing/rss.xml",            1),
            ("buffer",       "https://buffer.com/resources/rss/",                     2),
            ("socialmedia",  "https://www.socialmediatoday.com/rss.xml",              1),
            ("sprout",       "https://sproutsocial.com/insights/feed/",               2),
            ("later",        "https://later.com/blog/feed/",                          2),
        ],
        "gnews_queries": [
            "digital marketing strategy 2025", "social media algorithm update",
            "content marketing SEO tips",
        ],
        "tone_positive": {
            "growth", "viral", "record", "engagement", "conversion", "ROI",
            "breakthrough", "launched", "trending", "successful", "increase",
        },
        "tone_negative": {
            "algorithm change", "reach drop", "ban", "lawsuit", "scandal",
            "backlash", "fail", "outage", "decline", "penalty",
        },
        "keywords": {
            "marketing", "campaign", "brand", "branding", "audience", "social media",
            "content", "strategy", "advertising", "ads", "seo", "search engine",
            "email", "influencer", "followers", "traffic", "leads", "funnel",
            "analytics", "instagram", "tiktok", "youtube", "twitter", "x",
            "facebook", "linkedin", "newsletter", "blog", "copy", "copywriting",
            "storytelling", "ctr", "impressions", "organic reach", "paid media",
            "ppc", "display ads", "retargeting", "automation", "crm", "hubspot",
            "hootsuite", "buffer", "sprout", "agency", "creative", "ab test",
            "persona", "segmentation", "ugc", "user generated", "affiliate",
            "podcast", "webinar", "landing page", "cta", "click through",
            "open rate", "bounce rate", "keyword research", "backlinks",
            "content calendar", "brand voice", "social proof",
        },
        "hashtags": {
            "positive": ["#MarketingWin", "#ContentMarketing", "#GrowthHacking", "#Viral", "#DigitalMarketing"],
            "negative": ["#MarketingAlert", "#AlgorithmChange", "#ReachDrop", "#SEOUpdate"],
            "neutral":  ["#MarketingTips", "#SocialMediaTips", "#ContentStrategy", "#SEOTips"],
            "universal": ["#Marketing", "#SocialMedia", "#DigitalMarketing", "#Branding", "#ContentCreator"],
        },
        "cta_x":  "👉 Follow for daily marketing & growth tips",
        "cta_tg": "\n\n🔔 Follow for daily marketing & social media content",
    },

    # ── REAL ESTATE ──────────────────────────────────────────────────────────
    "real_estate": {
        "label": "Real Estate",
        "icon": "🏠",
        "color": "#84cc16",
        "feeds": [
            ("therealdeal",  "https://therealdeal.com/feed/",                         1),
            ("inman",        "https://www.inman.com/feed/",                           1),
            ("housingwire",  "https://www.housingwire.com/feed/",                     1),
            ("bisnow",       "https://www.bisnow.com/rss",                            2),
            ("realestate",   "https://realestate.usnews.com/real-estate/rss",         2),
        ],
        "gnews_queries": [
            "real estate market housing prices 2025", "property investment mortgage rates",
            "commercial real estate development",
        ],
        "tone_positive": {
            "surge", "record", "growth", "appreciation", "boom", "demand",
            "milestone", "luxury", "development", "investment", "profit",
        },
        "tone_negative": {
            "crash", "decline", "foreclosure", "eviction", "ban", "lawsuit",
            "bubble", "collapse", "correction", "default", "loss",
        },
        "keywords": {
            "housing", "home", "property", "real estate", "market", "mortgage",
            "interest rate", "buyer", "seller", "agent", "realtor", "listing",
            "rent", "rental", "lease", "apartment", "house", "condo", "townhouse",
            "commercial", "residential", "development", "construction", "price",
            "neighborhood", "square feet", "inventory", "supply", "refinance",
            "lender", "loan", "down payment", "appraisal", "escrow", "closing",
            "title", "zoning", "permit", "landlord", "tenant", "eviction",
            "affordable housing", "luxury home", "penthouse", "flip", "reit",
            "multifamily", "single family", "duplex", "co-op", "hoa",
            "home sales", "existing home", "new construction", "pending sales",
            "cash buyer", "bidding war", "days on market",
        },
        "hashtags": {
            "positive": ["#RealEstateBoom", "#PropertyInvestment", "#HousingMarket", "#RealEstateInvesting"],
            "negative": ["#RealEstateAlert", "#HousingCrash", "#MortgageRates", "#Foreclosure"],
            "neutral":  ["#RealEstateTips", "#PropertyTips", "#HomeBuying", "#RealEstateNews"],
            "universal": ["#RealEstate", "#Property", "#Housing", "#Investing", "#RealEstateAgent", "#Homes"],
        },
        "cta_x":  "👉 Follow for daily real estate & property insights",
        "cta_tg": "\n\n🔔 Follow for daily real estate & property content",
    },

    # ── ENTERTAINMENT ────────────────────────────────────────────────────────
    "entertainment": {
        "label": "Entertainment & Pop Culture",
        "icon": "🎬",
        "color": "#f43f5e",
        "feeds": [
            ("variety",    "https://variety.com/feed/",                               1),
            ("hollywood",  "https://www.hollywoodreporter.com/feed/",                 1),
            ("billboard",  "https://www.billboard.com/feed/",                         1),
            ("ew",         "https://ew.com/feed/",                                    2),
            ("deadline",   "https://deadline.com/feed/",                              1),
        ],
        "gnews_queries": [
            "movie release box office record", "celebrity news entertainment",
            "music album tour announcement",
        ],
        "tone_positive": {
            "record", "blockbuster", "award", "oscar", "grammy", "launch",
            "premiere", "trending", "viral", "sold out", "number one",
        },
        "tone_negative": {
            "cancelled", "controversy", "scandal", "lawsuit", "backlash",
            "flop", "boycott", "arrest", "drama", "feud",
        },
        "keywords": {
            "movie", "film", "show", "series", "tv show", "album", "song", "track",
            "artist", "musician", "singer", "actor", "actress", "director",
            "celebrity", "celeb", "star", "music", "streaming", "netflix", "disney",
            "hbo", "amazon prime", "apple tv", "concert", "tour", "episode",
            "season", "trailer", "teaser", "cast", "role", "character",
            "studio", "box office", "television", "grammy", "oscar", "emmy",
            "golden globe", "bafta", "billboard", "chart", "debut album",
            "music video", "live performance", "red carpet", "premiere night",
            "sequel", "reboot", "spinoff", "documentary", "biopic", "animation",
            "superhero", "marvel", "dc", "franchise", "theme park", "broadway",
            "theatre", "theater", "comedian", "stand-up", "podcast", "celebrity",
        },
        "hashtags": {
            "positive": ["#NewRelease", "#BoxOffice", "#Entertainment", "#NowWatching", "#MustWatch"],
            "negative": ["#Entertainment", "#Controversy", "#CelebNews", "#Drama"],
            "neutral":  ["#EntertainmentNews", "#PopCulture", "#Movies", "#Music", "#TV"],
            "universal": ["#Entertainment", "#Hollywood", "#PopCulture", "#Celebrity", "#FilmTwitter"],
        },
        "cta_x":  "👉 Follow for daily entertainment & pop culture news",
        "cta_tg": "\n\n🔔 Follow for daily entertainment & pop culture content",
    },
}

# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

NICHE_IDS = list(NICHES.keys())


def get_niche(niche_id: str) -> dict:
    """Return niche config, falling back to crypto if unknown."""
    return NICHES.get(niche_id, NICHES["crypto"])


def niche_tone(story: dict, niche_id: str) -> str:
    """Classify story tone using niche-specific keywords."""
    cfg = get_niche(niche_id)
    title = (story.get("title") or "").lower()
    if any(kw in title for kw in cfg.get("tone_negative", set())):
        return "negative"
    if any(kw in title for kw in cfg.get("tone_positive", set())):
        return "positive"
    return "neutral"


def niche_hashtags(niche_id: str, tone: str, limit: int = 12) -> str:
    """Return space-joined hashtag string for the given niche + tone."""
    cfg = get_niche(niche_id)
    h = cfg.get("hashtags", {})
    # Map positive/negative/neutral to the right bucket
    tone_key = "positive" if tone in ("positive", "bull") else (
               "negative" if tone in ("negative", "bear") else "neutral")
    tags: list[str] = []
    seen: set[str] = set()

    def _add(t: str) -> None:
        if t not in seen:
            seen.add(t)
            tags.append(t)

    for t in h.get(tone_key, []):
        _add(t)
    for t in h.get("universal", []):
        _add(t)
        if len(tags) >= limit:
            break
    return " ".join(tags[:limit])


def niche_image_styles(niche_id: str) -> dict[str, list[str]] | None:
    """Return niche-specific image background styles, or None to use crypto defaults."""
    cfg = get_niche(niche_id)
    return cfg.get("image_styles") or None


# ---------------------------------------------------------------------------
# Niche → Viral Scout search topics
# ---------------------------------------------------------------------------
# These are the SHORT search keywords used by the Viral Scout to scan
# YouTube / TikTok / Instagram / X for viral content. They should be:
#   - 2–4 words max
#   - the kind of phrase a creator would type into the platform search bar
#   - between 5 and 8 per niche (more = slower scans, less = thin samples)
#
# The Viral Scout will pick these automatically based on the user's selected
# niche (settings.niche). Users can still override per-account via the
# `viral_topics` setting or VIRAL_TOPICS env var.

NICHE_VIRAL_TOPICS: dict[str, tuple[str, ...]] = {
    "crypto": (
        "crypto", "bitcoin", "ethereum", "crypto news", "altcoin",
    ),
    "fitness": (
        "workout", "gym motivation", "home workout", "weight loss",
        "muscle building", "abs workout", "fitness tips",
    ),
    "business": (
        "startup", "entrepreneur", "business tips", "side hustle",
        "make money online", "small business", "productivity",
    ),
    "food": (
        "recipe", "easy recipe", "quick meals", "food hacks",
        "viral recipe", "cooking tips", "meal prep",
    ),
    "fashion": (
        "outfit ideas", "ootd", "streetwear", "fashion trends",
        "style tips", "sneaker drop", "fashion haul",
    ),
    "travel": (
        "travel", "travel tips", "hidden gems", "budget travel",
        "travel hacks", "best destinations", "solo travel",
    ),
    "technology": (
        "ai tools", "tech news", "chatgpt", "ai", "tech tips",
        "gadgets", "new tech",
    ),
    "marketing": (
        "marketing tips", "social media growth", "content creation",
        "instagram growth", "tiktok strategy", "personal branding", "seo tips",
    ),
    "real_estate": (
        "real estate", "real estate tips", "first home buyer",
        "house tour", "real estate investing", "mortgage tips", "property",
    ),
    "entertainment": (
        "movie review", "tv shows", "celebrity news", "music",
        "pop culture", "new release", "trailer reaction",
    ),
}


def niche_viral_topics(niche_id: str) -> list[str]:
    """Return the viral-scout search keywords for the given niche.

    Falls back to the crypto list if the niche is unknown (mirrors get_niche).
    """
    topics = NICHE_VIRAL_TOPICS.get(niche_id) or NICHE_VIRAL_TOPICS["crypto"]
    return list(topics)


# ---------------------------------------------------------------------------
# Niche-specific BG_STYLES for image_gen
# ---------------------------------------------------------------------------

NICHE_BG_STYLES: dict[str, dict[str, list[str]]] = {

    "fitness": {
        "positive": [
            "athletic person achieving fitness peak performance at sunrise, golden light, "
            "outdoor stadium track, motion blur, inspirational, cinematic 8k, no text",
            "professional gym interior with modern equipment, green and white lighting, "
            "motivational atmosphere, clean minimal aesthetic, 8k photorealistic, no text",
            "healthy meal prep flat lay, vibrant colorful vegetables and proteins, "
            "clean kitchen counter, top-down view, natural light, 8k, no text",
            "mountain trail runner in epic landscape at golden hour, sweeping vista, "
            "determination and freedom, cinematic wide shot, 8k, no text",
            "modern fitness class high energy, diverse group workout, bright studio, "
            "dynamic motion, professional photography, no text no watermark",
        ],
        "negative": [
            "caution warning sign in gym environment, yellow safety tape, "
            "serious medical professional, clinical setting, dramatic lighting, no text",
            "injury prevention concept, red and white medical colors, sterile clinical, "
            "professional health alert imagery, no text no watermark",
        ],
        "neutral": [
            "clean minimal fitness studio, soft morning light, yoga mats, "
            "wellness atmosphere, pastel tones, 8k photorealistic, no text",
            "nutrition science flat lay, supplements vitamins healthy food, "
            "white background, professional product photography, no text",
            "aerial view of outdoor sports park, people exercising, blue sky, "
            "green grass, vibrant colors, drone photography, no text",
        ],
    },

    "business": {
        "positive": [
            "modern glass skyscraper exterior at golden hour, financial district, "
            "success and ambition, dramatic upward angle, cinematic, 8k, no text",
            "dynamic startup team celebrating in modern open office, diverse group, "
            "celebration confetti, warm lighting, authentic, 8k, no text",
            "stock market bull charging through financial district, green data streams, "
            "powerful and optimistic, dramatic lighting, cinematic, no text",
            "luxury boardroom with city skyline view, executive meeting, "
            "confidence and power, dark wood and glass, 8k, no text",
            "entrepreneur at standing desk overlooking city at night, ambitious, "
            "dramatic window light, cinematic depth of field, 8k, no text",
        ],
        "negative": [
            "empty corporate office after layoffs, grey muted tones, "
            "dramatic shadows, serious atmosphere, cinematic, no text",
            "stock market crash visualization, red graphs on dark screens, "
            "trading floor panic, dramatic red lighting, no text no watermark",
        ],
        "neutral": [
            "clean modern coworking space, diverse professionals working, "
            "soft natural light, creative atmosphere, 8k photorealistic, no text",
            "business strategy whiteboard in modern conference room, "
            "professionals collaborating, bright natural light, no text",
            "aerial view of city financial district at dusk, lights coming on, "
            "cinematic drone shot, no text no watermark",
        ],
    },

    "food": {
        "positive": [
            "stunning gourmet dish beautifully plated on dark slate, "
            "perfect soft lighting, steam rising, cinematic food photography, no text",
            "vibrant colorful farmer's market produce, fresh vegetables and fruits, "
            "natural outdoor light, abundance and freshness, 8k, no text",
            "award-winning chef plating in professional kitchen, dramatic lighting, "
            "artistry and passion, cinematic close-up, no text",
            "overhead flat lay of elaborate home-cooked feast, warm tones, "
            "rustic wooden table, abundance, natural light, no text",
            "perfect pizza with stretchy cheese pull, dramatic lighting, "
            "Italian restaurant setting, cinematic food porn, no text",
        ],
        "negative": [
            "food safety inspection concept, clinical laboratory setting, "
            "red warning colors, serious professional atmosphere, no text",
            "warning sign food recall concept, red and white colors, "
            "clean minimal design, serious tone, no text no watermark",
        ],
        "neutral": [
            "clean minimal food photography, single beautiful dish on white, "
            "perfect natural light, editorial style, no text",
            "cooking process in home kitchen, natural daylight, fresh ingredients, "
            "warm tones, authentic home cooking, no text",
            "cafe scene with beautiful latte art, cozy atmosphere, "
            "warm tones, lifestyle photography, no text",
        ],
    },

    "fashion": {
        "positive": [
            "high fashion editorial runway show, dramatic lighting, "
            "luxury fabric texture, model silhouette, Vogue style, no text",
            "luxury streetwear flat lay, designer pieces on marble surface, "
            "editorial lighting, minimal aesthetic, 8k, no text",
            "fashion district city scene, stylish people, vibrant colors, "
            "street photography aesthetic, golden hour, no text",
            "exclusive boutique interior, luxury fashion store, "
            "marble floors, dramatic spotlights, editorial, no text",
            "fashion week backstage, models preparing, dramatic lighting, "
            "motion and glamour, cinematic, no text",
        ],
        "negative": [
            "dark moody fashion editorial, controversy aesthetic, "
            "dramatic shadows, serious tone, no text no watermark",
        ],
        "neutral": [
            "minimal fashion editorial, clean white studio, "
            "single garment perfectly lit, professional photography, no text",
            "fashion mood board aesthetic, fabric textures and colors, "
            "editorial flat lay, minimalist, no text",
            "stylish urban setting, city street fashion, candid photography, "
            "natural light, authentic style, no text",
        ],
    },

    "travel": {
        "positive": [
            "breathtaking aerial view of tropical paradise island, turquoise ocean, "
            "white sand, drone photography, golden hour, 8k, no text",
            "epic mountain landscape at sunrise, hiker silhouette on peak, "
            "dramatic sky, adventure and freedom, cinematic, no text",
            "ancient historic city at golden hour, warm tones, architecture, "
            "wanderlust atmosphere, drone photography, no text",
            "luxury overwater bungalow resort, pristine turquoise water, "
            "perfection and paradise, cinematic, no text",
            "train journey through stunning European countryside, window view, "
            "motion blur landscape, adventure, cinematic, no text",
        ],
        "negative": [
            "dramatic storm approaching travel destination, warning atmosphere, "
            "dark clouds over ocean, caution, cinematic, no text",
            "travel disruption concept, empty airport, cancelled signs, "
            "muted tones, serious atmosphere, no text",
        ],
        "neutral": [
            "cozy airport lounge scene, traveler with map, wanderlust, "
            "warm golden tones, cinematic lifestyle, no text",
            "world map flat lay with travel accessories, passport camera, "
            "wanderlust concept, natural light, no text",
            "scenic road trip highway through vast landscape, freedom and adventure, "
            "open road, dramatic sky, cinematic, no text",
        ],
    },

    "technology": {
        "positive": [
            "futuristic AI neural network visualization, glowing blue data streams, "
            "dark background, breakthrough concept, cinematic, no text",
            "cutting-edge tech lab interior, scientists working, holographic displays, "
            "innovation atmosphere, 8k, no text",
            "modern data center rows of servers, blue lighting, technology power, "
            "cinematic perspective, no text",
            "robot hand and human hand reaching toward each other, AI collaboration, "
            "dramatic lighting, symbolic, cinematic, no text",
            "startup office with multiple screens, team collaboration, "
            "modern minimal design, innovation energy, no text",
        ],
        "negative": [
            "cybersecurity threat concept, red and dark hacker aesthetic, "
            "binary code streams, dramatic tension, no text",
            "broken screen data breach visualization, red error codes, "
            "dark dramatic atmosphere, no text no watermark",
        ],
        "neutral": [
            "clean minimal tech workspace, laptop and devices on white desk, "
            "soft natural light, professional, no text",
            "circuit board macro photography, technology texture, "
            "blue and green tones, abstract tech, no text",
            "person coding on laptop in modern cafe, soft focus background, "
            "developer lifestyle, no text",
        ],
    },

    "marketing": {
        "positive": [
            "viral social media analytics dashboard, growing engagement charts, "
            "green metrics, success visualization, dark minimal UI, no text",
            "creative marketing agency team brainstorming, colorful sticky notes, "
            "modern loft office, energy and creativity, no text",
            "brand launch event, colorful balloons and excitement, "
            "crowd celebrating, vivid colors, cinematic, no text",
            "influencer content creation setup, ring light camera, "
            "modern room aesthetic, social media vibes, no text",
        ],
        "negative": [
            "analytics dashboard showing decline, red metrics, "
            "serious business atmosphere, dark tone, no text",
            "algorithm change concept, social media disruption, "
            "dark dramatic tones, warning atmosphere, no text",
        ],
        "neutral": [
            "content creation flat lay, camera phone notebook, "
            "minimal white aesthetic, marketing tools, no text",
            "digital marketing concept, connected devices and platforms, "
            "clean modern visualization, no text",
            "brand strategy meeting in creative office, mood boards, "
            "warm tones, collaborative, no text",
        ],
    },

    "real_estate": {
        "positive": [
            "luxury modern villa exterior at golden hour, infinity pool, "
            "stunning architecture, dream home, cinematic, no text",
            "real estate agent presenting luxury penthouse with city view, "
            "success and achievement, dramatic light, no text",
            "new residential development aerial view, modern homes, "
            "green spaces, drone photography, bright, no text",
            "iconic skyscraper construction completing, growth and investment, "
            "dramatic sky, cinematic, no text",
        ],
        "negative": [
            "housing market decline concept, red downward arrows, "
            "serious tone, dark muted colors, no text",
            "foreclosure warning sign on suburban house, "
            "overcast sky, serious atmosphere, no text",
        ],
        "neutral": [
            "clean modern house interior, minimal Scandinavian design, "
            "natural light, lifestyle photography, no text",
            "real estate agent showing home to couple, professional, "
            "bright interior, lifestyle, no text",
            "city skyline panorama at dusk, real estate landscape, "
            "cinematic drone shot, no text",
        ],
    },

    "entertainment": {
        "positive": [
            "movie premiere red carpet, celebrities and lights, "
            "glamour and excitement, cinematic photography, no text",
            "concert arena filled with fans and lights, euphoric atmosphere, "
            "dramatic stage lighting, no text",
            "blockbuster film set production, cinematic behind-the-scenes, "
            "epic scale and drama, no text",
            "award show stage golden trophies, spotlight drama, "
            "glamour and achievement, cinematic, no text",
        ],
        "negative": [
            "dark dramatic entertainment controversy concept, "
            "intense moody lighting, serious tone, no text",
        ],
        "neutral": [
            "cinema popcorn and screen glow, cozy movie night, "
            "warm tones, lifestyle photography, no text",
            "recording studio professional setup, microphone and mixing board, "
            "creative atmosphere, warm lighting, no text",
            "streaming platform concept, multiple screens with content, "
            "modern living room, lifestyle, no text",
        ],
    },
}
