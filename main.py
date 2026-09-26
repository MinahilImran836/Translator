"""
English <-> Urdu translator for Pakistani banking communication.

Pipeline: detect language (no AI) -> shield sensitive values locally -> pick
relevant cold samples -> verify no original value is in the prompt -> ONE
streamed call to OpenRouter -> local integrity check (no retry; failures are
flagged, never re-sent) -> restore values locally -> heuristic checks ->
masked-only history.

Privacy invariant: original sensitive values live only in memory for the
duration of one request. History, feedback and the cache hold masked text.

Self-improvement is fully automatic and conservative: feedback is monitored,
and a pattern only changes the prompt or example bank once it has recurred
across enough independent sessions/messages (see compute_proposals() and
auto_improve()). There is no admin UI; every change is applied by the server
itself when the evidence threshold is met, and is idempotent via
proposal_decisions so the same evidence can't reapply itself repeatedly.
"""

import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import time
from collections import Counter, OrderedDict
from contextlib import asynccontextmanager, closing
from typing import AsyncIterator, Callable, Dict, List, Literal, NamedTuple, Optional, Tuple

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TRANSLATOR_DB_PATH", "translator.db")
KNOWLEDGE_DIR = os.environ.get("KNOWLEDGE_DIR", os.path.join(BASE_DIR, "knowledge"))
STATIC_DIR = os.path.join(BASE_DIR, "static")

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "qwen/qwen3.8-27b:free")
OPENROUTER_FALLBACK_MODELS = [
    m.strip()
    for m in os.environ.get(
        "OPENROUTER_FALLBACK_MODELS",
        "nvidia/nemotron-3-ultra-550b-a55b:free,google/gemma-4-26b-a4b-it:free",
    ).split(",")
    if m.strip()
]
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_REFERRER = os.environ.get("OPENROUTER_REFERRER", "http://localhost")
OPENROUTER_APP_TITLE = os.environ.get("OPENROUTER_APP_TITLE", "Urdu Banking Translator")

MAX_INPUT_CHARS = 5000
MAX_SAMPLES_IN_PROMPT = 3
HISTORY_LIMIT = 100
HISTORY_TTL_S = 24 * 3600
CACHE_SIZE = 500
SESSION_COOKIE = "tsid"
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{16,64}")

http_client: Optional[httpx.AsyncClient] = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http_client
    init_db()
    knowledge.refresh()
    # One pooled client so every request reuses the warm TLS connection to OpenRouter.
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=300),
    )
    yield
    await http_client.aclose()


app = FastAPI(title="Urdu Banking Translator", version="5.0.0", lifespan=lifespan)

if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def session_cookie(request: Request, call_next):
    session_id = request.cookies.get(SESSION_COOKIE, "")
    is_new = not SESSION_ID_RE.fullmatch(session_id)
    if is_new:
        session_id = secrets.token_urlsafe(24)
    request.state.session_id = session_id

    response = await call_next(request)
    if is_new:
        is_https = (request.url.scheme == "https"
                    or request.headers.get("x-forwarded-proto") == "https")
        response.set_cookie(SESSION_COOKIE, session_id, httponly=True,
                            samesite="lax", secure=is_https)
    return response


# ---------------------------------------------------------------------------
# Storage (SQLite) — masked text only
# ---------------------------------------------------------------------------

# Earlier versions stored unmasked text in these tables.
LEGACY_TABLES = ("history", "translations", "terminology")

# Auto-translate fires while the user is still typing; drafts of the same
# sentence within this window are folded into one history entry.
DRAFT_MERGE_WINDOW_S = 120


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA secure_delete = ON")  # zero out deleted rows on disk
    return conn


def init_db() -> None:
    with closing(db()) as conn:
        existing = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        legacy = [t for t in LEGACY_TABLES if t in existing]
        for table in legacy:
            conn.execute(f"DROP TABLE {table}")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS masked_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                source_lang TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                masked_source TEXT NOT NULL,
                masked_output TEXT NOT NULL,
                protected_types TEXT NOT NULL,
                sample_ids TEXT NOT NULL,
                score INTEGER NOT NULL,
                warnings TEXT NOT NULL,
                integrity_ok INTEGER NOT NULL,
                security_flag INTEGER NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_masked_history_session ON masked_history(session_id, id);

            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                history_id INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                category TEXT,
                comment TEXT,
                correction TEXT,
                masked_source TEXT NOT NULL,
                masked_output TEXT NOT NULL,
                source_lang TEXT NOT NULL,
                target_lang TEXT NOT NULL,
                sample_ids TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS proposal_decisions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_key TEXT NOT NULL,
                decision TEXT NOT NULL,
                decided_at REAL NOT NULL
            );
            """
        )
        conn.commit()
        if legacy:
            conn.execute("VACUUM")  # rewrite the file so dropped plaintext is not recoverable


def save_history(session_id: str, record: Dict) -> int:
    now = time.time()
    values = (
        record["source_lang"], record["target_lang"], record["masked_source"], record["masked_output"],
        json.dumps(record["protected_types"]), json.dumps(record["sample_ids"]), record["score"],
        json.dumps(record["warnings"], ensure_ascii=False), int(record["integrity_ok"]),
        int(record["security_flag"]), now,
    )
    source = record["masked_source"]

    with closing(db()) as conn:
        conn.execute("DELETE FROM masked_history WHERE created_at < ?", (now - HISTORY_TTL_S,))
        last = conn.execute(
            "SELECT id, masked_source, created_at FROM masked_history WHERE session_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()

        if last and now - last["created_at"] < DRAFT_MERGE_WINDOW_S and (
            source.startswith(last["masked_source"]) or last["masked_source"].startswith(source)
        ):
            row_id = last["id"]
            conn.execute(
                "UPDATE masked_history SET source_lang = ?, target_lang = ?, masked_source = ?, "
                "masked_output = ?, protected_types = ?, sample_ids = ?, score = ?, warnings = ?, "
                "integrity_ok = ?, security_flag = ?, created_at = ? WHERE id = ?",
                (*values, row_id),
            )
        else:
            row_id = conn.execute(
                "INSERT INTO masked_history (source_lang, target_lang, masked_source, masked_output, "
                "protected_types, sample_ids, score, warnings, integrity_ok, security_flag, created_at, "
                "session_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (*values, session_id),
            ).lastrowid
            conn.execute(
                "DELETE FROM masked_history WHERE session_id = ? AND id NOT IN "
                "(SELECT id FROM masked_history WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
                (session_id, session_id, HISTORY_LIMIT),
            )
        conn.commit()
    return row_id


def _history_row(row: sqlite3.Row) -> Dict:
    item = dict(row)
    item.pop("session_id", None)
    for field in ("protected_types", "sample_ids", "warnings"):
        item[field] = json.loads(item[field])
    item["integrity_ok"] = bool(item["integrity_ok"])
    item["security_flag"] = bool(item["security_flag"])
    return item


def fetch_history(session_id: str, limit: int) -> List[Dict]:
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM masked_history WHERE session_id = ? AND created_at >= ? "
            "ORDER BY id DESC LIMIT ?",
            (session_id, time.time() - HISTORY_TTL_S, limit),
        ).fetchall()
    return [_history_row(row) for row in rows]


def fetch_history_item(session_id: str, history_id: int) -> Dict:
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT * FROM masked_history WHERE id = ? AND session_id = ?", (history_id, session_id)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Translation not found in this session.")
    return _history_row(row)


def clear_history(session_id: str) -> None:
    with closing(db()) as conn:
        conn.execute("DELETE FROM masked_history WHERE session_id = ?", (session_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# Language detection (script matching + Roman-Urdu word rules, no AI)
# ---------------------------------------------------------------------------

URDU_CHAR_RE = re.compile(r"[؀-ۿݐ-ݿﭐ-﷿ﹰ-﻿]")
LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
LATIN_WORD_RE = re.compile(r"[a-z]+")

# Common Roman-Urdu function words. English look-alikes (main, the, me, to,
# so, hi, den) are deliberately left out.
ROMAN_URDU_WORDS = frozenset("""
    hai hain ho hoga hogi hoge hota hoti hotay raha rahi rahe nahi nahin nai mein
    mera meri mere mujhe mujhay aap ap apna apni apne hum humara hamara hamari tum
    yeh ye woh wo kya kyun kyon kaise kaisay kab kahan kitna kitne kitni ka ki ke
    ko se sy par pe aur ya lekin magar tha thi thay gaya gayi gaye gya gyi diya
    dein karo karein karen kar karna karni karne kiya chahiye chahie bhi sirf abhi
    kal aaj paisay paise raqam bhej bhejo bhejna nikal jama khata shukriya
    meherbani bataein batayen bata zara kat kaat hua hui huay wapis
""".split())


def urdu_share(text: str) -> float:
    urdu = len(URDU_CHAR_RE.findall(text))
    latin = len(LATIN_CHAR_RE.findall(text))
    return urdu / (urdu + latin) if urdu + latin else 0.0


def detect_language(text: str) -> str:
    """Returns 'ur' (Urdu script), 'ur-Latn' (Roman Urdu) or 'en'."""
    # English never contains Urdu script, while Urdu banking text is full of
    # English terms (ATM, OTP, account), so a modest Urdu-script share wins.
    if urdu_share(text) >= 0.2:
        return "ur"
    words = LATIN_WORD_RE.findall(text.lower())
    hits = sum(word in ROMAN_URDU_WORDS for word in words)
    if (hits >= 2 and hits / len(words) >= 0.2) or (hits and len(words) <= 3):
        return "ur-Latn"
    return "en"


# ---------------------------------------------------------------------------
# Sensitive Information Shield
#
# Financial identifiers are replaced locally by typed placeholders such as
# [[IBAN_1]] before anything is sent to the model. As a final safety net, any
# remaining run of 4+ digits is masked too. Times and dates are re-rendered in
# the target language ("10 PM" -> "رات 10 بجے"); every other value is restored
# exactly as written.
# ---------------------------------------------------------------------------

D = "[0-9۰-۹]"  # ASCII and Urdu (Extended Arabic-Indic) digits
URDU_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")

EN_MONTHS = ["January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
UR_MONTHS = ["جنوری", "فروری", "مارچ", "اپریل", "مئی", "جون", "جولائی",
             "اگست", "ستمبر", "اکتوبر", "نومبر", "دسمبر"]
EN_MONTH = (r"(?P<mon>(?i:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?"
            r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?))\.?")

DAY_PERIODS = {
    "صبح": "morning", "subah": "morning", "subha": "morning",
    "دوپہر": "noon", "dopahar": "noon", "dopehar": "noon",
    "سہپہر": "afternoon", "sehpahar": "afternoon", "sehpehar": "afternoon",
    "شام": "evening", "shaam": "evening", "sham": "evening",
    "رات": "night", "raat": "night", "rat": "night",
}

LRI, RLI, PDI = "⁦", "⁧", "⁩"
PLACEHOLDER_RE = re.compile(r"\[\[\s*([A-Z]+)_(\d+)\s*\]\]")


class Entity(NamedTuple):
    key: str          # e.g. "IBAN_1"
    original: str
    replacement: str  # what is re-injected (original, or a localized time/date)

    @property
    def type(self) -> str:
        return self.key.rsplit("_", 1)[0]


class Rule(NamedTuple):
    label: str
    pattern: re.Pattern
    render: Optional[Callable[[re.Match, str], Optional[str]]] = None


def _clock(hour: int, minute: Optional[str]) -> str:
    return f"{hour}:{minute}" if minute and minute != "00" else str(hour)


def render_time_to_urdu(match: re.Match, target_lang: str) -> Optional[str]:
    """'10:45 PM' -> 'رات 10:45 بجے' when translating into Urdu."""
    hour = int(match.group("h"))
    if target_lang != "ur" or not 1 <= hour <= 12:
        return None
    is_am = (match.group("ap") or match.group("ap2")).lower() == "a"
    if is_am:
        period = "رات" if hour in (12, 1, 2, 3) else "صبح"
    elif hour in (12, 1, 2):
        period = "دوپہر"
    elif hour in (3, 4, 5):
        period = "سہ پہر"
    elif hour in (6, 7):
        period = "شام"
    else:
        period = "رات"
    return f"{period} {_clock(hour, match.group('m'))} بجے"


def render_time_to_english(match: re.Match, target_lang: str) -> Optional[str]:
    """'رات 9 بجے' / 'raat 9 bajay' -> '9 PM' when translating into English."""
    hour = int(match.group("h").translate(URDU_DIGITS))
    minute = (match.group("m") or "").translate(URDU_DIGITS) or None
    if target_lang != "en" or not 1 <= hour <= 12:
        return None
    period = DAY_PERIODS.get(re.sub(r"\s", "", match.group("p") or "").lower())
    clock = _clock(hour, minute)
    if period is None:
        return f"{clock} o'clock" if minute is None else clock
    if period == "morning" or (period == "noon" and hour == 11):
        return f"{clock} AM"
    if period == "night" and not 6 <= hour <= 11:
        return f"{clock} AM"
    return f"{clock} PM"


def render_date(match: re.Match, target_lang: str) -> Optional[str]:
    """Swaps the month name into the target language; digits stay untouched."""
    month = match.group("mon")
    if month in UR_MONTHS:
        if target_lang != "en":
            return None
        name = EN_MONTHS[UR_MONTHS.index(month)]
    else:
        if target_lang != "ur":
            return None
        name = UR_MONTHS[[m[:3].lower() for m in EN_MONTHS].index(month[:3].lower())]
    return " ".join(part for part in (match.group("d"), name, match.group("y")) if part)


def _re(pattern: str, flags: int = 0) -> re.Pattern:
    return re.compile(pattern, flags)


# Ordered by priority: earlier rules claim their span first. A rule with a
# named group "v" masks only that group (e.g. the digits after "OTP").
SHIELD_RULES: List[Rule] = [
    # Placeholders already present (e.g. re-translating masked history) pass through.
    Rule("PLACEHOLDER", PLACEHOLDER_RE),
    Rule("EMAIL", _re(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    Rule("LINK", _re(r"(?:https?://|www\.)[^\s<>\"]*[^\s<>\".,;:!?)\]']")),
    Rule("IBAN", _re(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}(?: ?[A-Z0-9]{1,3})?\b")),
    Rule("CNIC", _re(rf"\b{D}{{5}}-{D}{{7}}-{D}\b|\b{D}{{13}}\b")),
    Rule("CARD", _re(rf"\b(?:{D}{{4}}[ -]?){{3}}{D}{{4}}\b|(?:[*xX•]{{4}}[ -]?){{1,3}}{D}{{4}}\b")),
    Rule("PHONE", _re(rf"(?:\+92|\b0092|\b0)[ -]?3{D}{{2}}[ -]?{D}{{7}}\b")),
    Rule("TXN", _re(r"\b(?:TXN|TRX|TRN|TID|RRN|STAN|REF|FT)[-_#:]?\s?[A-Z0-9-]*\d[A-Z0-9-]*\b")),
    Rule("SECRET", _re(rf"\b(?:OTP|M?PIN|TPIN|CVV|CVC|passcode|password)\b\D{{0,15}}?(?P<v>{D}{{3,8}})\b",
                       re.IGNORECASE)),
    Rule("AMOUNT", _re(
        rf"(?:\bRs\.?|\bPKR|\bUSD|US\$|\$|€|£)\s?{D}[0-9۰-۹,]*(?:\.{D}+)?"
        r"(?:\s?(?:/-|(?:k|K|lac|lakh|million|crore)\b))?"
    )),
    Rule("AMOUNT", _re(
        rf"(?P<v>{D}[0-9۰-۹,]*(?:\.{D}+)?)\s?(?:روپے|روپیہ|روپئے|rupees|rupay|rupaye)",
        re.IGNORECASE,
    )),
    Rule("DATE", _re(rf"\b{D}{{4}}-{D}{{2}}-{D}{{2}}\b|\b{D}{{1,2}}[/.-]{D}{{1,2}}[/.-]{D}{{2,4}}\b")),
    Rule("DATE", _re(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?[\s-]{EN_MONTH}(?:[\s,-]+(?P<y>\d{{4}}))?\b"),
         render_date),
    Rule("DATE", _re(rf"\b{EN_MONTH}\s(?P<d>\d{{1,2}})(?:st|nd|rd|th)?(?:,?\s(?P<y>\d{{4}}))?\b"),
         render_date),
    Rule("DATE", _re(rf"(?P<d>{D}{{1,2}})\s*(?P<mon>{'|'.join(UR_MONTHS)})(?:\s*(?P<y>{D}{{4}}))?"),
         render_date),
    Rule("TIME", _re(r"\b(?P<h>\d{1,2})(?::(?P<m>[0-5]\d))?\s?(?:(?P<ap>[AaPp])\.[Mm]\.|(?P<ap2>[AaPp])[Mm]\b)"),
         render_time_to_urdu),
    Rule("TIME", _re(rf"(?:(?P<p>صبح|دوپہر|سہ ?پہر|شام|رات)\s*)?(?P<h>{D}{{1,2}})(?::(?P<m>{D}{{2}}))?\s*بجے"),
         render_time_to_english),
    Rule("TIME", _re(r"\b(?:(?P<p>subah|subha|dopahar|dopehar|sehpahar|sehpehar|shaam|sham|raat|rat)\s+)?"
                     r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?:bajay|bajey|baje|bjay|bje)\b", re.IGNORECASE),
         render_time_to_english),
    Rule("TIME", _re(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b")),
    Rule("AMOUNT", _re(rf"\b{D}{{1,3}}(?:,{D}{{2,3}})+(?:\.{D}+)?\b")),
    Rule("ACCOUNT", _re(rf"\b{D}(?:[ -]?{D}){{8,19}}\b")),
    Rule("NUMBER", _re(rf"{D}{{4,}}")),
]


def shield(text: str, target_lang: str) -> Tuple[str, List[Entity]]:
    """Masks sensitive values with [[TYPE_n]] placeholders; returns (masked_text, entities)."""
    spans: List[Tuple[int, int, Rule, re.Match]] = []

    def overlaps(start: int, end: int) -> bool:
        return any(start < s_end and end > s_start for s_start, s_end, _, _ in spans)

    for rule in SHIELD_RULES:
        for match in rule.pattern.finditer(text):
            start, end = match.span("v") if "v" in rule.pattern.groupindex else match.span()
            if not overlaps(start, end):
                spans.append((start, end, rule, match))

    spans.sort(key=lambda span: span[0])

    counters: Dict[str, int] = {}
    entities: List[Entity] = []
    pieces: List[str] = []
    cursor = 0
    for start, end, rule, match in spans:
        label = match.group(1) if rule.label == "PLACEHOLDER" else rule.label
        counters[label] = counters.get(label, 0) + 1
        key = f"{label}_{counters[label]}"
        original = text[start:end]
        replacement = (rule.render(match, target_lang) if rule.render else None) or original
        entities.append(Entity(key, original, replacement))
        pieces.append(text[cursor:start])
        pieces.append(f"[[{key}]]")
        cursor = end
    pieces.append(text[cursor:])

    return "".join(pieces), entities


def sensitive_entities(entities: List[Entity]) -> List[Entity]:
    """Entities holding a real value (not a placeholder that was already in the input)."""
    return [e for e in entities if not PLACEHOLDER_RE.fullmatch(e.original)]


def mask(text: Optional[str], target_lang: str) -> Optional[str]:
    return shield(text, target_lang)[0] if text else text


def isolate(value: str, target_lang: str) -> str:
    """Wraps a re-injected value in a Unicode bidi isolate so LTR values
    (Rs. 25,000, IBANs, +92 numbers) keep their order inside RTL Urdu text."""
    has_urdu = bool(URDU_CHAR_RE.search(value))
    if target_lang == "ur" and not has_urdu:
        return f"{LRI}{value}{PDI}"
    if target_lang == "en" and has_urdu:
        return f"{RLI}{value}{PDI}"
    return value


def reinject(text: str, entities: List[Entity], target_lang: str) -> str:
    by_key = {e.key: e for e in entities}

    def replace(match: re.Match) -> str:
        entity = by_key.get(f"{match.group(1)}_{match.group(2)}")
        return isolate(entity.replacement, target_lang) if entity else match.group()

    return PLACEHOLDER_RE.sub(replace, text)


class StreamReinjector:
    """Re-injects values into streamed chunks, holding back a placeholder split across chunks."""

    def __init__(self, entities: List[Entity], target_lang: str):
        self.entities = entities
        self.target_lang = target_lang
        self.buffer = ""

    def feed(self, chunk: str) -> str:
        self.buffer += chunk
        cut = self.buffer.rfind("[[")
        if cut != -1 and "]]" not in self.buffer[cut:] and len(self.buffer) - cut < 24:
            ready, self.buffer = self.buffer[:cut], self.buffer[cut:]
        elif self.buffer.endswith("["):
            ready, self.buffer = self.buffer[:-1], "["
        else:
            ready, self.buffer = self.buffer, ""
        return reinject(ready, self.entities, self.target_lang)

    def flush(self) -> str:
        ready, self.buffer = self.buffer, ""
        return reinject(ready, self.entities, self.target_lang)


# ---------------------------------------------------------------------------
# Integrity validation (deterministic, runs before values are restored)
# ---------------------------------------------------------------------------


class Integrity(NamedTuple):
    ok: bool
    problems: List[str]


def check_integrity(raw_output: str, entities: List[Entity], target_lang: str) -> Integrity:
    counts = Counter(f"{t}_{n}" for t, n in PLACEHOLDER_RE.findall(raw_output))
    expected = {e.key for e in entities}
    problems: List[str] = []

    missing = sorted(expected - counts.keys())
    if missing:
        problems.append("missing " + ", ".join(f"[[{k}]]" for k in missing))
    duplicated = sorted(k for k in expected if counts[k] > 1)
    if duplicated:
        problems.append("repeated " + ", ".join(f"[[{k}]]" for k in duplicated))
    unknown = sorted(set(counts) - expected)
    if unknown:
        problems.append("unknown " + ", ".join(f"[[{k}]]" for k in unknown))

    remainder = PLACEHOLDER_RE.sub(" ", raw_output)
    if "[[" in remainder or "]]" in remainder:
        problems.append("malformed placeholder brackets")
    invented = sorted({e.type for e in shield(remainder, target_lang)[1]})
    if invented:
        problems.append("values not present in the source: " + ", ".join(invented))

    return Integrity(not problems, problems)


# ---------------------------------------------------------------------------
# Security: flag instruction-like text (defense in depth, never a guarantee).
# Flagged text is still translated as ordinary content.
# ---------------------------------------------------------------------------

INJECTION_RE = re.compile(
    r"\b(?:ignore|disregard|forget)\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+"
    r"(?:instructions|prompts|rules)"
    r"|\breveal\s+(?:the\s+|your\s+)?(?:system|hidden)\s+prompt"
    r"|\bnew\s+instructions\s*:"
    r"|</?(?:system|assistant|user|source)>"
    r"|(?:پچھلی|سابقہ)\s+(?:تمام\s+)?ہدایات"
    r"|\bpichl[ie]\s+(?:tamam\s+|saari\s+)?hidayat",
    re.IGNORECASE,
)


def detect_prompt_injection(text: str) -> bool:
    return bool(INJECTION_RE.search(text))


# ---------------------------------------------------------------------------
# Knowledge: editable prompt + curated cold samples (knowledge/*.json)
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"[^\W\d_]{2,}")
STOPWORDS = frozenset("""
    the an to of for and or is are was be your you my me it this that in on at with from by has
    have been will can not please our we us do how what hai ka ki ke ko se mein mera meri mere
    aap nahi ho raha rahi kar dein gaya gayi gaye hoon hua hui kaise karun please
    کے کی کا کو سے میں ہے ہیں اور یا نہیں کر آپ میرا میری میرے یہ وہ گیا گئی گئے
""".split())


def text_tokens(text: str) -> set:
    return {t for t in TOKEN_RE.findall(text.lower()) if t not in STOPWORDS}


def placeholder_types(text: str) -> set:
    return {t for t, _ in PLACEHOLDER_RE.findall(text)}


class KnowledgeBase:
    """Loads knowledge/prompt.json and knowledge/cold_samples.json, reloading when either file changes."""

    def __init__(self, directory: str):
        self.prompt_path = os.path.join(directory, "prompt.json")
        self.samples_path = os.path.join(directory, "cold_samples.json")
        self.version: Tuple[float, float] = (0.0, 0.0)
        self.prompt: Dict = {}
        self.samples: List[Dict] = []
        self.idf: Dict[str, float] = {}

    def refresh(self) -> None:
        version = (os.path.getmtime(self.prompt_path), os.path.getmtime(self.samples_path))
        if version == self.version:
            return
        with open(self.prompt_path, encoding="utf-8") as f:
            self.prompt = json.load(f)
        with open(self.samples_path, encoding="utf-8") as f:
            self.samples = json.load(f)["samples"]
        for sample in self.samples:
            sample["_tokens"] = text_tokens(" ".join(
                [sample["en"], sample["ur"], sample.get("roman", ""), " ".join(sample.get("tags", []))]
            ))
            sample["_types"] = placeholder_types(sample["en"])
        df = Counter(t for s in self.samples for t in s["_tokens"])
        n = len(self.samples)
        self.idf = {t: math.log((n + 1) / (c + 1)) + 1 for t, c in df.items()}
        self.version = version

    def public_samples(self) -> List[Dict]:
        return [{k: v for k, v in s.items() if not k.startswith("_")} for s in self.samples]

    @staticmethod
    def _write_json(path: str, data: Dict) -> None:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)

    def save_samples(self, samples: List[Dict]) -> None:
        self._write_json(self.samples_path, {"version": 1, "samples": samples})
        self.refresh()

    def save_prompt(self, prompt: Dict) -> None:
        self._write_json(self.prompt_path, prompt)
        self.refresh()


knowledge = KnowledgeBase(KNOWLEDGE_DIR)


def select_samples(masked_text: str, script_lang: str, security_flag: bool) -> List[Dict]:
    """Picks the few most relevant approved samples for this input.

    Deliberately independent of feedback: only the approved bank decides, so no
    individual rating can change what the model sees.
    """
    query_tokens = text_tokens(masked_text)
    query_types = placeholder_types(masked_text)

    scored: List[Tuple[float, Dict]] = []
    for sample in knowledge.samples:
        if not sample.get("approved", False):
            continue
        if "security" in sample.get("tags", []) and not security_flag:
            continue
        score = sum(knowledge.idf.get(t, 0) for t in query_tokens & sample["_tokens"])
        score += 1.5 * len(query_types & sample["_types"])
        if script_lang == "ur-Latn" and sample.get("roman"):
            score += 0.5
        if "security" in sample.get("tags", []):
            score += 100  # always include one when the input looks like an injection
        if score > 0:
            scored.append((score, sample))

    scored.sort(key=lambda item: item[0], reverse=True)
    chosen = [sample for _, sample in scored[:MAX_SAMPLES_IN_PROMPT]]
    if not chosen:
        chosen = [s for s in knowledge.samples if "core" in s.get("tags", []) and s.get("approved", False)][:2]
    return list(reversed(chosen))  # most relevant example sits closest to the input


def sample_errors(sample: Dict, existing_ids: set) -> List[str]:
    errors = []
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{2,39}", sample["id"]):
        errors.append("id must be 3-40 lowercase letters, digits or dashes")
    elif sample["id"] in existing_ids:
        errors.append(f"id '{sample['id']}' already exists")
    texts = {field: sample.get(field) for field in ("en", "ur", "roman") if sample.get(field)}
    if "en" not in texts or "ur" not in texts:
        errors.append("both 'en' and 'ur' are required")
    for field, text in texts.items():
        leaked = sorted({e.type for e in sensitive_entities(shield(text, "en")[1])})
        if leaked:
            errors.append(f"'{field}' contains unmasked values ({', '.join(leaked)}); use placeholders "
                          f"such as [[AMOUNT_1]]")
    keys = {field: sorted(f"{t}_{n}" for t, n in PLACEHOLDER_RE.findall(text)) for field, text in texts.items()}
    if len({tuple(v) for v in keys.values()}) > 1:
        errors.append("every language version must contain the same placeholders")
    return errors


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def format_user_message(masked_text: str) -> str:
    return f"<source>\n{masked_text}\n</source>"


def relevant_terminology(masked_text: str, target_lang: str) -> List[str]:
    """Only the glossary entries that occur in this input, to keep the prompt small."""
    lowered = masked_text.lower()
    pairs = []
    for english, urdu in knowledge.prompt.get("loanwords", {}).items():
        if target_lang == "ur" and re.search(rf"\b{re.escape(english)}\b", lowered):
            pairs.append(f"{english} → {urdu}")
        elif target_lang == "en" and urdu in masked_text:
            pairs.append(f"{urdu} → {english}")
    return pairs[:12]


def build_system_prompt(masked_text: str, target_lang: str) -> str:
    prompt = knowledge.prompt
    keep_in_english = ", ".join(prompt.get("keep_in_english", []))
    lines = [prompt["role"][target_lang]]
    for section in prompt["sections"]:
        rules = section.get("all", []) + section.get(target_lang, [])
        if rules:
            lines.append(f"\n## {section['title']}")
            lines.extend(f"- {rule.replace('{keep_in_english}', keep_in_english)}" for rule in rules)

    terms = relevant_terminology(masked_text, target_lang)
    if terms:
        lines.append("\n## Preferred wording in this text")
        lines.append("- " + "; ".join(terms))

    # Only rules an admin approved (after a recurring, independently reported pattern).
    issue_rules = prompt.get("issue_rules", {})
    enabled = [key for key in prompt.get("enabled_issue_rules", []) if key in issue_rules]
    if enabled:
        lines.append("\n## Reviewer feedback")
        lines.extend(f"- {issue_rules[key]['rule']}" for key in enabled)
    return "\n".join(lines)


def sample_pair(sample: Dict, script_lang: str, target_lang: str) -> Tuple[str, str]:
    use_roman = script_lang == "ur-Latn" and sample.get("roman")
    if target_lang == "ur":
        return (sample["roman"] if use_roman else sample["en"]), sample["ur"]
    return (sample["roman"] if use_roman else sample["ur"]), sample["en"]


def build_messages(masked_text: str, script_lang: str, target_lang: str,
                   samples: List[Dict]) -> List[Dict]:
    messages = [{"role": "system", "content": build_system_prompt(masked_text, target_lang)}]
    for sample in samples:
        source, target = sample_pair(sample, script_lang, target_lang)
        messages.append({"role": "user", "content": format_user_message(source)})
        messages.append({"role": "assistant", "content": target})
    messages.append({"role": "user", "content": format_user_message(masked_text)})
    return messages


DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


class TranslationError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def assert_no_leak(messages: List[Dict], entities: List[Entity]) -> None:
    """Refuses to call the model if any original sensitive value made it into the prompt."""
    outgoing = "\n".join(m["content"] for m in messages)
    leaked = sorted({e.type for e in sensitive_entities(entities) if e.original in outgoing})
    if leaked:
        raise TranslationError(500, "Blocked before sending: a protected value ("
                               + ", ".join(leaked) + ") would have reached the model.")


AWKWARD_TIME_RE = re.compile(r"بجے\s+پر(?=[\s۔،]|$)")


def clean_output(text: str, target_lang: str) -> str:
    text = re.sub(r"</?source>", "", text).strip()
    if target_lang == "ur":
        # "10 بجے پر" is unidiomatic; Urdu uses the time phrase alone.
        text = AWKWARD_TIME_RE.sub("بجے", text)
    return text


# ---------------------------------------------------------------------------
# OpenRouter streaming call
# ---------------------------------------------------------------------------


def _upstream_error(status: int, body: bytes) -> TranslationError:
    if status == 429:
        return TranslationError(429, "The free models are busy right now (rate-limited). "
                                     "Please try again in a few seconds.")
    try:
        message = json.loads(body)["error"]["message"]
    except (ValueError, KeyError, TypeError):
        message = f"HTTP {status}"
    return TranslationError(502, f"Translation service error: {message}")


# When the primary free model is rate-limited, every request would first wait
# for its rejection before falling back; try it last for a while instead.
PRIMARY_COOLDOWN_S = 60
_primary_cooldown_until = 0.0


def model_order() -> List[str]:
    if time.monotonic() < _primary_cooldown_until:
        order = [*OPENROUTER_FALLBACK_MODELS, OPENROUTER_MODEL]
    else:
        order = [OPENROUTER_MODEL, *OPENROUTER_FALLBACK_MODELS]
    return list(dict.fromkeys(order))


def record_serving_model(model: Optional[str]) -> None:
    global _primary_cooldown_until
    if model and model.split(":")[0] != OPENROUTER_MODEL.split(":")[0]:
        _primary_cooldown_until = time.monotonic() + PRIMARY_COOLDOWN_S


async def stream_openrouter(messages: List[Dict], max_tokens: int) -> AsyncIterator[Tuple[str, Optional[str]]]:
    """Yields (text_delta, model_name) pairs as the model generates."""
    payload = {
        "models": model_order(),  # OpenRouter tries these in order within a single request
        "messages": messages,
        "stream": True,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "reasoning": {"enabled": False},
    }
    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "HTTP-Referer": OPENROUTER_REFERRER,
        "X-Title": OPENROUTER_APP_TITLE,
    }

    try:
        async with http_client.stream("POST", OPENROUTER_URL, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                raise _upstream_error(resp.status_code, await resp.aread())
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue  # blank lines and ": OPENROUTER PROCESSING" keep-alives
                data = line[6:]
                if data == "[DONE]":
                    break
                chunk = json.loads(data)
                if "error" in chunk:
                    raise TranslationError(502, f"Translation service error: {chunk['error'].get('message')}")
                choices = chunk.get("choices") or []
                if choices:
                    yield choices[0].get("delta", {}).get("content") or "", chunk.get("model")
    except httpx.HTTPError as exc:
        raise TranslationError(502, f"Could not reach the translation service ({type(exc).__name__}).")


# ---------------------------------------------------------------------------
# Heuristic feedback (automated, no human-in-the-loop required)
# ---------------------------------------------------------------------------

REPEATED_WORD_RE = re.compile(r"\b(\w+)\s+\1\b", re.IGNORECASE)


def heuristic_feedback(source_text: str, raw_output: str, final_text: str, target_lang: str,
                       integrity: Integrity, security_flag: bool) -> Tuple[int, List[str]]:
    if not final_text:
        return 0, ["The model returned an empty translation."]

    score = 100
    warnings: List[str] = []

    if not integrity.ok:
        score -= 50
        warnings.append("Integrity check failed (" + "; ".join(integrity.problems)
                        + "). Review before sending.")

    ratio = len(final_text) / max(len(source_text.strip()), 1)
    if ratio < 0.3:
        score -= 20
        warnings.append("Translation looks unusually short compared to the source.")
    elif ratio > 3.0:
        score -= 15
        warnings.append("Translation looks unusually long compared to the source.")

    # Judge the script on the model's own words, not on re-injected IBANs/amounts.
    share = urdu_share(PLACEHOLDER_RE.sub(" ", raw_output))
    if DEVANAGARI_RE.search(final_text):
        score -= 30
        warnings.append("Output contains Hindi (Devanagari) script.")
    if target_lang == "ur" and share < 0.4:
        score -= 30
        warnings.append("Output is not mostly in Urdu script.")
    elif target_lang == "en" and share > 0.2:
        score -= 30
        warnings.append("Output still contains Urdu script.")

    if final_text == source_text.strip():
        score -= 40
        warnings.append("Output is identical to the input.")

    if REPEATED_WORD_RE.search(final_text):
        score -= 10
        warnings.append("Immediately repeated words detected (possible generation artifact).")

    if security_flag:
        warnings.append("Source contains instruction-like text; it was translated as ordinary "
                        "content and flagged for review.")

    if not warnings:
        warnings.append("No issues detected by automated checks.")
    return max(0, score), warnings


# ---------------------------------------------------------------------------
# Translation pipeline
# ---------------------------------------------------------------------------

# Keyed and valued by masked text only, so the cache never holds real values
# (and the same message with different amounts still hits).
_cache: "OrderedDict[Tuple, Tuple[str, Optional[str]]]" = OrderedDict()


def cache_get(key: Tuple) -> Optional[Tuple[str, Optional[str]]]:
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    return None


def cache_put(key: Tuple, value: Tuple[str, Optional[str]]) -> None:
    _cache[key] = value
    _cache.move_to_end(key)
    if len(_cache) > CACHE_SIZE:
        _cache.popitem(last=False)


async def run_translation(text: str, direction: str, session_id: str) -> AsyncIterator[Dict]:
    started = time.perf_counter()
    text = text.strip()
    script_lang = detect_language(text)
    if direction == "auto":
        source_lang = script_lang
        target_lang = "ur" if source_lang == "en" else "en"
    else:
        source_lang, target_lang = direction.split("-")
    yield {"type": "meta", "source_lang": source_lang, "target_lang": target_lang,
           "detected": direction == "auto"}

    knowledge.refresh()
    security_flag = detect_prompt_injection(text)
    masked_text, entities = shield(text, target_lang)
    samples = select_samples(masked_text, script_lang, security_flag)
    messages = build_messages(masked_text, script_lang, target_lang, samples)
    assert_no_leak(messages, entities)

    cache_key = (target_lang, script_lang, masked_text, knowledge.version)
    cached = cache_get(cache_key)

    if cached:
        # Served from the local cache: zero model calls for this request.
        raw_output, model = cached
        integrity = check_integrity(raw_output, entities, target_lang)
        yield {"type": "delta", "text": reinject(raw_output, entities, target_lang)}
    else:
        # Exactly one model call per translation. If the local integrity check below
        # fails, the result is flagged in the response — it is never re-sent to the model.
        max_tokens = min(8192, 256 + 2 * len(text))
        reinjector = StreamReinjector(entities, target_lang)
        parts: List[str] = []
        model = None
        async for delta, chunk_model in stream_openrouter(messages, max_tokens):
            model = chunk_model or model
            parts.append(delta)
            ready = reinjector.feed(delta)
            if ready:
                yield {"type": "delta", "text": ready}
        tail = reinjector.flush()
        if tail:
            yield {"type": "delta", "text": tail}

        raw_output = "".join(parts)
        integrity = check_integrity(raw_output, entities, target_lang)
        record_serving_model(model)
        if integrity.ok and not DEVANAGARI_RE.search(raw_output):
            cache_put(cache_key, (raw_output, model))

    final_text = clean_output(reinject(raw_output, entities, target_lang), target_lang)
    score, warnings = heuristic_feedback(text, raw_output, final_text, target_lang,
                                         integrity, security_flag)
    protected_types = sorted({e.type for e in sensitive_entities(entities)})
    sample_ids = [s["id"] for s in samples]

    history_id = save_history(session_id, {
        "source_lang": source_lang,
        "target_lang": target_lang,
        "masked_source": masked_text,
        "masked_output": clean_output(raw_output, target_lang),
        "protected_types": protected_types,
        "sample_ids": sample_ids,
        "score": score,
        "warnings": warnings,
        "integrity_ok": integrity.ok,
        "security_flag": security_flag,
    })

    yield {
        "type": "done",
        "history_id": history_id,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "translated_text": final_text,
        "sent_to_model": masked_text,
        "model": model,
        "cached": bool(cached),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "samples_used": sample_ids,
        "security_flag": security_flag,
        "integrity": {"ok": integrity.ok, "problems": integrity.problems},
        "privacy": {
            "protected_count": len(sensitive_entities(entities)),
            "protected_types": protected_types,
            "originals_sent_to_model": False,  # enforced by assert_no_leak
            "history_masked": True,
        },
        "feedback": {"score": score, "warnings": warnings},
    }


# ---------------------------------------------------------------------------
# API: translation, history, feedback
# ---------------------------------------------------------------------------


class TranslateRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_INPUT_CHARS)
    direction: Literal["auto", "en-ur", "ur-en"] = "auto"


class FeedbackRequest(BaseModel):
    history_id: int
    rating: Literal[1, -1]
    category: Optional[str] = Field(None, max_length=40)
    comment: Optional[str] = Field(None, max_length=500)
    correction: Optional[str] = Field(None, max_length=MAX_INPUT_CHARS)


def require_api_key() -> None:
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=500, detail="OPENROUTER_API_KEY is not set on the server.")


@app.get("/", include_in_schema=False)
def root():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
def health() -> Dict:
    return {"status": "ok", "models": [OPENROUTER_MODEL, *OPENROUTER_FALLBACK_MODELS]}


@app.post("/shield")
def preview_shield(payload: TranslateRequest) -> Dict:
    """Shows exactly what would be sent to the model, without calling it."""
    text = payload.text.strip()
    source_lang = detect_language(text)
    target_lang = ("ur" if source_lang == "en" else "en") if payload.direction == "auto" \
        else payload.direction.split("-")[1]
    masked_text, entities = shield(text, target_lang)
    return {"source_lang": source_lang, "target_lang": target_lang, "sent_to_model": masked_text,
            "protected_types": sorted({e.type for e in sensitive_entities(entities)})}


@app.post("/translate/stream")
async def translate_stream(payload: TranslateRequest, request: Request) -> StreamingResponse:
    """Server-sent events: one `meta`, many `delta`s, then `done` or `error`.

    Exactly one OpenRouter call happens per translation (zero on a cache hit). If the
    local integrity check fails, that is reported in `done.integrity`, not retried.
    """
    require_api_key()

    async def events() -> AsyncIterator[str]:
        try:
            async for event in run_translation(payload.text, payload.direction, request.state.session_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except TranslationError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': exc.message}, ensure_ascii=False)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/translate")
async def translate(payload: TranslateRequest, request: Request) -> Dict:
    require_api_key()
    try:
        async for event in run_translation(payload.text, payload.direction, request.state.session_id):
            if event["type"] == "done":
                return event
    except TranslationError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message)
    raise HTTPException(status_code=502, detail="Translation did not complete.")


@app.get("/history")
def get_history(request: Request, limit: int = 50) -> List[Dict]:
    return fetch_history(request.state.session_id, min(max(limit, 1), HISTORY_LIMIT))


@app.delete("/history")
def delete_history(request: Request) -> Dict[str, str]:
    clear_history(request.state.session_id)
    return {"status": "cleared"}


@app.get("/feedback/categories")
def feedback_categories() -> List[Dict[str, str]]:
    knowledge.refresh()
    categories = [{"key": key, "label": rule["label"]}
                  for key, rule in knowledge.prompt.get("issue_rules", {}).items()]
    return categories + [{"key": "other", "label": "Other"}]


@app.post("/feedback")
def submit_feedback(payload: FeedbackRequest, request: Request) -> Dict:
    """Stores a rating for one of this session's translations, for monitoring only.

    A single rating never changes the prompt or the example bank; see compute_proposals().
    Free text is masked before saving.
    """
    session_id = request.state.session_id
    item = fetch_history_item(session_id, payload.history_id)
    categories = {c["key"] for c in feedback_categories()}
    category = payload.category if payload.category in categories else None
    comment = mask(payload.comment.strip() if payload.comment else None, "en")
    correction = mask(payload.correction.strip() if payload.correction else None, item["target_lang"])

    with closing(db()) as conn:
        conn.execute("DELETE FROM feedback WHERE session_id = ? AND history_id = ?",
                     (session_id, payload.history_id))
        conn.execute(
            "INSERT INTO feedback (session_id, history_id, rating, category, comment, correction, "
            "masked_source, masked_output, source_lang, target_lang, sample_ids, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, payload.history_id, payload.rating, category, comment or None, correction or None,
             item["masked_source"], item["masked_output"], item["source_lang"], item["target_lang"],
             json.dumps(item["sample_ids"]), time.time()),
        )
        conn.commit()

    # Automatic and conservative: this only changes anything when the new evidence pushes
    # some pattern's independent-example count over the threshold (see auto_improve()).
    applied = auto_improve()
    return {"status": "stored_for_monitoring", "stored": {"comment": comment, "correction": correction},
            "self_improved": applied}


# ---------------------------------------------------------------------------
# Conservative learning
#
# Feedback is stored for monitoring only. A recurring pattern becomes a
# *proposal* once it has been reported in enough independent examples
# (distinct sessions AND distinct messages). Nothing changes until an admin
# approves a proposal; the evidence and any new example are re-validated at
# that moment. A single bad translation can therefore never change behavior.
# ---------------------------------------------------------------------------


def learning_policy() -> Tuple[int, int]:
    policy = knowledge.prompt.get("learning", {})
    return policy.get("min_independent_examples", 3), policy.get("window_days", 30)


def normalize_message(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower()).rstrip(".۔!?؟ ")


def independent_count(rows: List[Dict], same_message: bool = False) -> int:
    """How many independent examples support a pattern: one per session, and for
    cross-message patterns also one per distinct message."""
    sessions = len({r["session_id"] for r in rows})
    if same_message:
        return sessions
    return min(sessions, len({normalize_message(r["masked_source"]) for r in rows}))


def draft_example(example_id: str, source_lang: str, target_lang: str, source: str, translation: str) -> Dict:
    draft = {"id": example_id, "tags": ["learned"], "en": "", "ur": "", "approved": True}
    source_field = "roman" if source_lang == "ur-Latn" else ("en" if target_lang == "ur" else "ur")
    draft[source_field] = source
    draft["ur" if target_lang == "ur" else "en"] = translation
    return draft


def _proposal(key: str, kind: str, title: str, support: List[Dict], independent: int, required: int,
              detail: Dict) -> Dict:
    return {"key": key, "kind": kind, "title": title, "independent_examples": independent,
            "required": required, "feedback_ids": sorted(r["id"] for r in support), "detail": detail}


def compute_proposals() -> List[Dict]:
    knowledge.refresh()
    required, window_days = learning_policy()
    with closing(db()) as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, session_id, rating, category, correction, masked_source, masked_output, source_lang, "
            "target_lang, sample_ids, created_at FROM feedback WHERE created_at >= ?",
            (time.time() - window_days * 86400,),
        )]
        decided = dict(conn.execute(
            "SELECT proposal_key, MAX(decided_at) FROM proposal_decisions GROUP BY proposal_key"
        ).fetchall())
    for row in rows:
        row["sample_ids"] = json.loads(row["sample_ids"])

    def fresh(key: str, candidates: List[Dict]) -> List[Dict]:
        # Evidence already considered in an earlier decision does not count again.
        return [r for r in candidates if r["created_at"] > decided.get(key, 0)]

    proposals: List[Dict] = []
    negatives = [r for r in rows if r["rating"] < 0]
    samples_by_id = {s["id"]: s for s in knowledge.public_samples()}

    # 1. A feedback category recurs -> enable its pre-written prompt rule.
    enabled = set(knowledge.prompt.get("enabled_issue_rules", []))
    for rule_key, rule in knowledge.prompt.get("issue_rules", {}).items():
        key = f"rule:{rule_key}"
        support = fresh(key, [r for r in negatives if r["category"] == rule_key])
        independent = independent_count(support)
        if rule_key not in enabled and independent >= required:
            proposals.append(_proposal(key, "enable_rule", f"Add prompt rule: {rule['label']}", support,
                                       independent, required, {"rule_key": rule_key, "rule": rule["rule"]}))

    # 2. An example keeps appearing in badly rated translations -> disable it.
    for sample_id, sample in samples_by_id.items():
        if not sample.get("approved", False):
            continue
        key = f"disable:{sample_id}"
        used = fresh(key, [r for r in rows if sample_id in r["sample_ids"]
                           and r["created_at"] > sample.get("updated_at", 0)])
        support = [r for r in used if r["rating"] < 0]
        independent = independent_count(support)
        if independent >= required and len(support) > 2 * (len(used) - len(support)):
            proposals.append(_proposal(key, "disable_sample", f"Disable example '{sample_id}'", support,
                                       independent, required, {"sample": sample,
                                                               "upvotes": len(used) - len(support)}))

    # 3. The same message is independently corrected, or independently approved
    #    with the same output -> add it to the example bank.
    known_texts = {normalize_message(s[f]) for s in samples_by_id.values() for f in ("en", "ur", "roman") if s.get(f)}
    groups: Dict[Tuple[str, str, str], List[Dict]] = {}
    for row in rows:
        norm = normalize_message(row["masked_source"])
        if norm not in known_texts:
            groups.setdefault((row["source_lang"], row["target_lang"], norm), []).append(row)

    for (source_lang, target_lang, norm), group in groups.items():
        digest = hashlib.sha1(f"{target_lang}|{norm}".encode()).hexdigest()[:10]
        source = group[0]["masked_source"]
        for kind, key, support, texts in (
            ("fix_example", f"fix:{digest}",
             [r for r in group if r["rating"] < 0 and r["correction"]], lambda r: r["correction"]),
            ("good_example", f"good:{digest}",
             [r for r in group if r["rating"] > 0], lambda r: r["masked_output"]),
        ):
            support = fresh(key, support)
            options: Dict[str, List[Dict]] = {}
            for r in support:
                options.setdefault(texts(r).strip(), []).append(r)
            if kind == "good_example":
                # Approvals only count when independent users approved the same output.
                support = max(options.values(), key=lambda rs: independent_count(rs, True), default=[])
            independent = independent_count(support, same_message=True)
            if independent < required:
                continue
            candidates = []
            for text, rs in sorted(options.items(), key=lambda kv: -independent_count(kv[1], True)):
                draft = draft_example(f"learned-{digest}", source_lang, target_lang, source, text)
                candidates.append({"text": text, "sessions": independent_count(rs, True), "draft": draft,
                                   "errors": sample_errors(draft, set(samples_by_id))})
            if kind == "good_example":
                candidates = candidates[:1]
            title = ("Add corrected example for a repeatedly corrected message" if kind == "fix_example"
                     else "Add example from a repeatedly approved translation")
            proposals.append(_proposal(key, kind, title, support, independent, required,
                                       {"source": source, "source_lang": source_lang,
                                        "target_lang": target_lang, "candidates": candidates}))
    return proposals


def record_decision(key: str, decision: str) -> None:
    with closing(db()) as conn:
        conn.execute("INSERT INTO proposal_decisions (proposal_key, decision, decided_at) VALUES (?, ?, ?)",
                     (key, decision, time.time()))
        conn.commit()


def auto_improve() -> List[Dict]:
    """Applies proposals automatically, but only once independent evidence crosses the
    threshold in compute_proposals(). No human review step exists: this IS the approval.

    Called right after a feedback item is stored, i.e. exactly when new evidence could
    exist. A single feedback item can never trigger a change by itself, because
    compute_proposals() already requires >= min_independent_examples distinct
    sessions (and, for cross-message patterns, distinct messages) before a pattern is
    even returned as a proposal. Each proposal key is applied at most once: after
    record_decision(), compute_proposals() only counts evidence newer than the decision,
    so the same votes can't reapply themselves.
    """
    applied = []
    for proposal in compute_proposals():
        key, kind, detail = proposal["key"], proposal["kind"], proposal["detail"]

        if kind == "enable_rule":
            prompt = dict(knowledge.prompt)
            enabled = set(prompt.get("enabled_issue_rules", []))
            if detail["rule_key"] not in enabled:
                prompt["enabled_issue_rules"] = sorted(enabled | {detail["rule_key"]})
                knowledge.save_prompt(prompt)
            record_decision(key, "applied")
            applied.append({"kind": kind, "key": key, "rule_key": detail["rule_key"]})

        elif kind == "disable_sample":
            sample_id = detail["sample"]["id"]
            samples = knowledge.public_samples()
            for sample in samples:
                if sample["id"] == sample_id and sample.get("approved", False):
                    sample.update(approved=False, updated_at=round(time.time(), 3))
            knowledge.save_samples(samples)
            record_decision(key, "applied")
            applied.append({"kind": kind, "key": key, "sample_id": sample_id})

        else:  # fix_example / good_example: add a new curated example
            chosen = next((c for c in detail["candidates"] if not c["errors"]), None)
            if chosen is None:
                # Every candidate failed local validation (e.g. still had a real value in
                # free text); never guess. Recorded so this exact evidence isn't retried
                # every time, but a differently worded correction can still propose again.
                record_decision(key, "invalid")
                continue
            samples = knowledge.public_samples()
            if chosen["draft"]["id"] not in {s["id"] for s in samples}:
                knowledge.save_samples(samples + [chosen["draft"]])
            record_decision(key, "applied")
            applied.append({"kind": kind, "key": key, "sample_id": chosen["draft"]["id"]})
    return applied
