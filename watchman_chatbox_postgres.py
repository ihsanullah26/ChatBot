"""
Watchman Smart Query Chatbox — complete pipeline, one file.

Read top to bottom, in this order:
    SECTION 1: CONFIG              - values YOU must edit (marked MANUAL)
    SECTION 2: DATABASE CONNECTION - connects to your real MySQL (MANUAL)
    SECTION 3: UNIT TYPE MAPPING   - which table holds which unit's logs
    SECTION 4: DATA LAYER          - the only functions that touch the DB
    SECTION 5: SCHEMA BUILDER      - builds the "menu" sent to the LLM
    SECTION 6: LLM PARSER          - calls DeepSeek V3.2 via OpenRouter (MANUAL: needs API key)
    SECTION 7: QUERY ENGINE        - does the actual math, never the LLM
    SECTION 8: FORMATTING          - turns a result into a sentence + list
    SECTION 9: MAIN PIPELINE       - answer_question(), calls everything above
    SECTION 10: RUN                - what happens when you run this file

Anywhere you see # >>> MANUAL: is something YOU need to set up or
confirm — a real credential, a running service, or a fact about the
real system that couldn't be verified from here.
"""

import json
import re
import time
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import pymysql

# >>> MANUAL: loads OPENROUTER_API_KEY (and anything else you put in a
# local .env file) into the environment. This is what lets you keep real
# secrets OUT of this source file — see the .gitignore note near
# OPENROUTER_API_KEY in SECTION 6 for what to do before pushing to
# GitHub. Requires `pip install python-dotenv`; if it's not installed,
# this just silently does nothing and you fall back to real `export`ed
# shell env vars, which works fine too.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from knowledge_base import WATCHMAN_KNOWLEDGE_BASE


# ============================================================
# LIGHTWEIGHT KB RETRIEVAL
# ------------------------------------------------------------
# The knowledge base has grown large enough (~13K tokens) that sending
# the WHOLE thing on every general_question call is wasteful — most
# questions only need one or two sections. This is a simple keyword-
# overlap retrieval step (not embeddings/vector search — that would be
# the "real" version of this for a production system, but overkill for
# this project's current scale). It cuts typical prompt size by roughly
# 80-90%, which matters both for cost and for staying comfortably under
# the request's token budget.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "do", "does", "did",
    "i", "my", "me", "you", "your", "it", "its", "to", "of", "in", "on",
    "for", "and", "or", "not", "can", "will", "how", "what", "when",
    "where", "why", "if", "so", "be", "have", "has", "with", "this",
    "that", "there", "am", "up", "at", "as", "get", "got",
}

_HEADER_PATTERN = re.compile(r'^[A-Z0-9][A-Z0-9 /\-\u2013\u2014\(\),&\.]{2,70}$')
_DATE_LABEL_PATTERN = re.compile(
    r'^[A-Z]+\s+\d{4}$'  # e.g. "MAY 2026", "SEPTEMBER 2022" — changelog
    r'|^\d+(\.\d+){2,}$'  # e.g. "192.168.4.1" — an IP address, not a header
)


def _looks_like_header(stripped: str) -> bool:
    if not stripped or set(stripped) == {"="}:
        return False
    if not _HEADER_PATTERN.match(stripped):
        return False
    if _DATE_LABEL_PATTERN.match(stripped):
        return False
    # Require at least one real word (3+ letters) — excludes bare IPs,
    # version numbers, and other numeric-only lines that otherwise match
    # the character-class pattern above.
    if not re.search(r'[A-Za-z]{3,}', stripped):
        return False
    return True

# Always included regardless of relevance score: cheap, and useful as
# baseline context / a fallback contact path for nearly any question.
_ALWAYS_INCLUDE_HEADERS = {"ABOUT WATCHMAN", "SUPPORT"}


def _tokenize(text: str) -> set:
    words = re.findall(r"[a-z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 1}


def _split_kb_into_sections(kb_text: str) -> list:
    """Returns an ordered list of (header, body_text) tuples, handling
    both the bare-header style used in the original KB and the
    ====-delimited style used in later additions."""
    lines = kb_text.split("\n")
    sections = []
    current_header = None
    current_lines = []
    for line in lines:
        stripped = line.strip()
        if _looks_like_header(stripped):
            if current_header is not None:
                sections.append((current_header, "\n".join(current_lines).strip()))
            current_header = stripped
            current_lines = []
        elif set(stripped) == {"="} and stripped:
            continue  # skip pure delimiter lines, they carry no content
        else:
            current_lines.append(line)
    if current_header is not None:
        sections.append((current_header, "\n".join(current_lines).strip()))
    return sections


# Parsed once at import time, not on every call.
_KB_SECTIONS = _split_kb_into_sections(WATCHMAN_KNOWLEDGE_BASE)


def select_relevant_kb_sections(question: str, top_k: int = 4) -> str:
    """Picks the top_k sections most relevant to the question (by simple
    keyword overlap against each section's header + body), always
    including the always-include sections, and returns them concatenated
    in their original order (so cross-references still read naturally)."""
    q_tokens = _tokenize(question)
    scored = []
    for header, body in _KB_SECTIONS:
        if header in _ALWAYS_INCLUDE_HEADERS:
            continue
        section_tokens = _tokenize(header) | _tokenize(body)
        overlap = q_tokens & section_tokens
        # Header matches count double — a header word matching the
        # question is a much stronger topical signal than a body word.
        score = len(overlap) + len(q_tokens & _tokenize(header))
        scored.append((score, header, body))

    scored.sort(key=lambda x: x[0], reverse=True)
    top_sections = {header for _, header, _ in scored[:top_k]}

    # Reassemble in ORIGINAL order (not score order), so the text still
    # reads coherently and cross-referenced facts stay nearby each other.
    chosen = []
    for header, body in _KB_SECTIONS:
        if header in _ALWAYS_INCLUDE_HEADERS or header in top_sections:
            chosen.append(f"{header}\n{body}")
    return "\n\n".join(chosen)


# ============================================================
# SECTION 1: CONFIG
# ============================================================

USER_ID = "test_user_1"
UNIT_ID = 1
QUESTION = "Does Watchman support 5GHz WiFi?"

# >>> MANUAL: set True while testing different models — prints the raw
# parsed JSON from parse_question() before any of the downstream logic
# runs, so you can see exactly how a given model classified the question
# (e.g. did it pick general_question vs not_applicable) without guessing
# from the final answer alone.
DEBUG_PRINT_PARSED = True


# ============================================================
# LIGHTWEIGHT MOOD / CONVERSATIONAL RESPONSE
# ------------------------------------------------------------
# This does NOT replace the existing general/database logic.
# It only handles short, purely conversational or emotional messages
# before the normal Watchman answer pipeline runs.
#
# Examples:
#   "I'm happy"     -> positive response
#   "I'm sad"       -> empathetic response
#   "I'm confused"  -> reassuring response
#   "I'm frustrated"-> calming response
#   "Shut up"       -> respectful/dismissive response
#
# Important: messages that also contain a real Watchman question are
# left alone so the existing support/database logic can answer them.
# ============================================================

_MOOD_RESPONSES = {
    "happy": "That's great to hear! 😊",
    "sad": "I'm sorry you're feeling sad. I'm here to help.",
    "confused": "No worries — I can help clear things up. 😊",
    "frustrated": "I understand. Let's work through it together.",
    "angry": "I understand you're frustrated. Let's see if I can help.",
    "excited": "That's awesome! 🎉",
    "tired": "Sounds like you need a little break. Take care!",
    "grateful": "You're very welcome! 😊",
    "dismissive": "Okay, I'll keep quiet. If you need me later, I'll be here.",
}

_MOOD_PATTERNS = {
    "dismissive": (
        r"\bshut\s*up\b",
        r"\bbe\s+quiet\b",
        r"\bstop\s+talking\b",
        r"\bstop\b",
        r"\bleave\s+me\s+alone\b",
        r"\bdon'?t\s+talk\b",
        r"\bget\s+lost\b",
        r"\bgo\s+away\b",
        r"\bleave\s+me\s+be\b",
        r"\bbuzz\s+off\b",
        r"\bgo\s+bother\s+someone\s+else\b",
    ),
    "confused": (
        r"\bi\s*(?:am|'m)\s+confused\b",
        r"\bim\s+confused\b",
        r"\bconfused\b",
        r"\bi\s*(?:am|'m)\s+lost\b",
        r"\bi\s+don'?t\s+understand\b",
    ),
    "frustrated": (
        r"\bi\s*(?:am|'m)\s+frustrated\b",
        r"\bim\s+frustrated\b",
        r"\bthis\s+is\s+frustrating\b",
        r"\bso\s+frustrating\b",
    ),
    "angry": (
        r"\bi\s*(?:am|'m)\s+angry\b",
        r"\bim\s+angry\b",
        r"\bi\s*(?:am|'m)\s+mad\b",
        r"\bim\s+mad\b",
    ),
    "sad": (
        r"\bi\s*(?:am|'m)\s+sad\b",
        r"\bim\s+sad\b",
        r"\bi\s*(?:am|'m)\s+upset\b",
        r"\bim\s+upset\b",
        r"\bi\s+feel\s+(?:bad|down|sad)\b",
    ),
    "happy": (
        r"\bi\s*(?:am|'m)\s+happy\b",
        r"\bim\s+happy\b",
        r"\bi\s*(?:am|'m)\s+glad\b",
        r"\bi\s+feel\s+great\b",
        r"\bfeeling\s+great\b",
        r"\bfeeling\s+good\b",
    ),
    "excited": (
        r"\bi\s*(?:am|'m)\s+excited\b",
        r"\bim\s+excited\b",
        r"\bso\s+excited\b",
        r"\bcan'?t\s+wait\b",
    ),
    "tired": (
        r"\bi\s*(?:am|'m)\s+tired\b",
        r"\bim\s+tired\b",
        r"\bi\s*(?:am|'m)\s+exhausted\b",
        r"\bim\s+exhausted\b",
        r"\bi\s+need\s+some\s+rest\b",
    ),
    "grateful": (
        r"\bthank\s+you\b",
        r"\bthanks\b",
        r"\bthank\s+you\s+so\s+much\b",
        r"\bi\s+appreciate\s+it\b",
    ),
}

def _is_pure_conversational_message(question: str) -> bool:
    """Return True only for short social/emotional messages, so a message
    like 'I'm frustrated because my Watchman is offline' still reaches the
    normal support/data pipeline."""
    q = question.strip().lower()
    if not q:
        return False
    if len(q) > 120:
        return False

    # Anything that looks like a real question should go through the normal
    # classifier / knowledge-base pipeline.
    if "?" in q:
        return False

    technical_terms = (
        "watchman", "temperature", "humidity", "sensor", "log", "portal",
        "wifi", " wi-fi", "subscription", "modem", "alert", "account",
        "password", "email", "device", "unit", "database", "data", "graph",
        "lte", "relay", "probe", "online", "offline",
    )
    if any(term in q for term in technical_terms):
        return False

    return True


def detect_mood_response(question: str):
    """Return a short mood-aware reply, or None when the message should
    continue through the existing chatbot logic.

    The actual reply text is generated by the LLM (see
    generate_mood_reply() in SECTION 6) rather than always returning the
    same fixed sentence per mood — so "I'm so tired, ugh" and "I'm tired
    lol" don't both get back the identical canned line. _MOOD_RESPONSES
    is kept as a pure fallback for when the API call itself fails."""
    if not _is_pure_conversational_message(question):
        return None

    q = question.strip().lower()

    # Highest-priority intents first.
    for mood in ("dismissive", "confused", "frustrated", "angry", "sad",
                 "happy", "excited", "tired", "grateful"):
        for pattern in _MOOD_PATTERNS[mood]:
            if re.search(pattern, q):
                return generate_mood_reply(question, mood)

    return None


# ============================================================
# SECTION 2: DATABASE CONNECTION
# ------------------------------------------------------------
# All connection params come from environment variables — none are
# hardcoded in source. MySQL doesn't have Postgres' separate
# schema/search_path concept — a MySQL "database" already IS what
# Postgres calls a schema — so DB_NAME here points directly at the
# database the real migrated data lives in (previously reached via
# DB_SCHEMA + a Postgres search_path option; that indirection doesn't
# apply anymore now that the app talks to MySQL directly).
#
# >>> MANUAL: set these before running (e.g. in a .env file loaded by
# your shell, or exported directly):
#   export DB_HOST=localhost
#   export DB_PORT=3306
#   export DB_NAME=mydatabase
#   export DB_USER=root
#   export DB_PASSWORD=your_real_password
# ============================================================

DB_HOST = os.environ.get("DB_HOST", "localhost")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_NAME = os.environ.get("DB_NAME", "mydatabase")
DB_USER = os.environ.get("DB_USER", "root")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "")  # >>> MANUAL

if not DB_PASSWORD:
    raise ValueError(
        "No database password set. Set the DB_PASSWORD environment variable before running."
    )

DB_CONFIG = {
    "host": DB_HOST,
    "port": DB_PORT,
    "user": DB_USER,
    "password": DB_PASSWORD,
    "database": DB_NAME,
    # Plain tuple rows (row[0], row[1], ...) — matches psycopg2's default
    # cursor shape, which is what every query site in this file already
    # indexes into. Not DictCursor; changing that would mean rewriting
    # every fetchone()/fetchall() call site to use column names instead.
    "cursorclass": pymysql.cursors.Cursor,
    # Every query in this file is a plain SELECT (confirmed — nothing
    # here writes to the DB), so there's nothing that strictly needs a
    # transaction to be committed. autocommit=True avoids relying on
    # get_connection()'s context manager to commit anything, which
    # matters because — unlike psycopg2, where `with conn:` commits or
    # rolls back a transaction — PyMySQL's connection context manager
    # behaves differently (its __enter__ returns a cursor, not the
    # connection), which is exactly why get_connection() below is its
    # own explicit @contextmanager wrapper rather than exposing the raw
    # pymysql connection object directly to the `with get_connection()
    # as conn:` pattern already used everywhere in this codebase.
    "autocommit": True,
}


@contextmanager
def get_connection():
    conn = pymysql.connect(**DB_CONFIG)
    try:
        yield conn
    finally:
        conn.close()


# ============================================================
# SECTION 3: UNIT TYPE -> TABLE MAPPING
# ============================================================

UNIT_TYPE_TABLE_MAP = {
    "simple": {
        "table": "avpeopletable",
        "columns": {"temprature": "temperature", "humidity": "humidity",
                    "gass": "gas_level", "people": "people_estimated"},
    },
    # 'buyout' is a subscription/billing plan name (confirmed by
    # knowledge_base.py: "Standard monthly, yearly, and BuyOut
    # subscription offerings..."), not a distinct hardware type. Verified
    # directly against the real dump before aliasing this: of 955 real
    # buyout units, 575 (60%) have sensor data specifically in
    # avpeopletable — versus 3 in logofwatchwithrealy and 12 in
    # avptemp — so this is overwhelmingly the right table. The
    # remaining ~377 units have no data in any sensor table at all
    # (likely purchased but never installed), and correctly get a
    # "no data found" answer either way, same as for 'simple'.
    "buyout": {
        "table": "avpeopletable",
        "columns": {"temprature": "temperature", "humidity": "humidity",
                    "gass": "gas_level", "people": "people_estimated"},
    },
    "relay": {
        "table": "logofwatchwithrealy",
        "columns": {"temprature": "temperature", "humidity": "humidity",
                    "gass": "gas_level", "people": "people_estimated"},
    },
    "avgtemp": {
        "table": "avptemp",
        "columns": {"temprature": "temperature", "humidity": "humidity",
                    "gass": "gas_level", "people": "people_estimated"},
    },
    "party": {
        "table": "avpeopletable",
        "columns": {"temprature": "temperature", "humidity": "humidity",
                    "gass": "gas_level", "people": "people_estimated"},
    },
    "noise": {
        "table": "soundlog",
        "columns": {"currnoise": "current_noise", "minnoise": "min_noise",
                    "peaknoise": "peak_noise", "noiselvl": "noise_level"},
    },
}


# Reverse index: table name -> its column config. Built once from
# UNIT_TYPE_TABLE_MAP so each table's own schema is only ever defined in
# one place, even when probed independently of any particular unit
# `type` value (see _get_unit_table_config's fallback below).
_TABLE_COLUMNS = {cfg["table"]: cfg["columns"] for cfg in UNIT_TYPE_TABLE_MAP.values()}


def _get_unit_table_config(unit_id: int) -> dict:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT type FROM watchrecord WHERE wid = %s", (unit_id,))
            row = cur.fetchone()
            if not row:
                raise ValueError(f"No unit found with id {unit_id}")
            unit_type = row[0]
            config = UNIT_TYPE_TABLE_MAP.get(unit_type)

            if config:
                # Mapped — but confirm the mapped table actually HAS rows
                # for this specific unit before trusting it. Real
                # production data has cases where a unit's nominal `type`
                # doesn't match where its data actually lives (see the
                # naming fallback note in ui_app.py's _lookup_name for a
                # confirmed example: some 'simple' units log to
                # logofwatchwithrealy, not avpeopletable). Rather than
                # confidently return the "wrong" table and let every
                # sensor question for that unit come back "no data
                # found", probe the other known tables too.
                cur.execute(
                    f"SELECT 1 FROM {config['table']} WHERE splog_id = %s LIMIT 1",
                    (unit_id,),
                )
                if cur.fetchone():
                    return {**config, "type": unit_type}

            # Either unmapped `type`, or mapped to a table with no rows
            # for this unit — probe every other known sensor table
            # before concluding there's genuinely no data anywhere.
            mapped_table = config["table"] if config else None
            for table, columns in _TABLE_COLUMNS.items():
                if table == mapped_table:
                    continue  # already checked above
                cur.execute(f"SELECT 1 FROM {table} WHERE splog_id = %s LIMIT 1", (unit_id,))
                if cur.fetchone():
                    return {"table": table, "columns": columns, "type": unit_type}

            if config:
                # Mapped, but genuinely no data in ANY known table — a
                # real "no data yet" unit, not a mapping bug. Return the
                # nominal mapping so downstream messaging stays accurate
                # (correct metric names in "this unit doesn't track X").
                return {**config, "type": unit_type}

    raise ValueError(f"Unknown unit type '{unit_type}' — add it to UNIT_TYPE_TABLE_MAP")


# ============================================================
# SECTION 4: DATA LAYER
# ============================================================

def get_unit_metrics(unit_id: int) -> list[str]:
    # _get_unit_table_config now probes real sensor tables directly for
    # unmapped `type` values or mismapped ones (see its docstring/comments
    # above), so this only returns [] for a genuinely unmapped type with
    # zero data in every known table — a real "not tracking anything yet"
    # unit, not a mapping bug. Degrading to an empty list rather than
    # raising here still matters: it lets general_question/smalltalk
    # keep working for that unit even though sensor questions can't.
    try:
        config = _get_unit_table_config(unit_id)
    except ValueError as e:
        print(f"  (unit metrics lookup failed for unit_id={unit_id}: {e})")
        return []
    return list(config["columns"].values())


def fetch_logs(unit_id: int, start, end: str) -> list[dict]:
    config = _get_unit_table_config(unit_id)
    table = config["table"]
    columns = config["columns"]

    column_list = ", ".join(columns.keys())
    if start is None:
        # all_time: no lower bound.
        query = f"""
            SELECT datetime, {column_list}
            FROM {table}
            WHERE splog_id = %s AND datetime <= %s
            ORDER BY datetime
        """
        params = (unit_id, end)
    else:
        query = f"""
            SELECT datetime, {column_list}
            FROM {table}
            WHERE splog_id = %s AND datetime BETWEEN %s AND %s
            ORDER BY datetime
        """
        params = (unit_id, start, end)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()

    logs = []
    for row in rows:
        timestamp = row[0]
        for i, (db_col, metric_name) in enumerate(columns.items()):
            value = row[i + 1]
            if value is None:
                continue
            logs.append({"timestamp": str(timestamp), "metric": metric_name, "value": float(value)})
    return logs


def unit_belongs_to_user(unit_id: int, user_id: str) -> bool:
    # FIXED (previously a known bug — user_id was accepted but never
    # checked, so any user_id "passed" for any real unit_id). The real
    # ownership link lives in alertconfigure: each row there ties a
    # splog_id (== watchrecord.wid, i.e. our unit_id) to the user_id
    # that configured alerts for it, which is the closest thing this
    # schema has to unit ownership.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM alertconfigure WHERE splog_id = %s AND user_id = %s",
                (unit_id, user_id),
            )
            return cur.fetchone() is not None


def get_subscription_status(user_id: str) -> str:
    # FIXED (previously a known bug — always checked the hardcoded
    # global USER_ID instead of the actual caller). Note: wmbundlesub is
    # a per-USER bundle subscription (it covers up to `totalwm` units
    # for that user), not a per-unit one — there's no column tying a
    # specific unit_id to a specific subscription row in this schema —
    # so checking by user_id (as done here) is the correct match to the
    # real data model, not a shortcut.
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT en_date FROM wmbundlesub WHERE user_id = %s ORDER BY en_date DESC LIMIT 1",
                (user_id,),
            )
            row = cur.fetchone()
    if not row or not row[0]:
        return "none"
        
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    return "active" if row[0] > now_utc else "none"


# ============================================================
# SECTION 5: SCHEMA BUILDER
# ============================================================

QUERY_TYPE_DESCRIPTIONS = (
    "Pick exactly one: "
    "count_threshold_crossings = customer asks HOW MANY TIMES a value "
    "crossed above/below a number, OR asks WHICH DAY/WHEN it crossed a "
    "specific threshold (e.g. 'on which day did it go above 75?') — the "
    "matching timestamps are returned either way, so this covers both "
    "phrasings. "
    "largest_change = customer asks about the BIGGEST SWING/CHANGE in a "
    "period, or which day had the biggest swing specifically (NOT which "
    "day crossed a threshold — that's count_threshold_crossings above, "
    "even when phrased as 'which day'). "
    "min_max = customer asks for the HIGHEST or LOWEST single reading in a "
    "period, or the full RANGE (both). Use the extreme field to say which. "
    "average = customer asks for a MEAN/TYPICAL value over a period. "
    "current_status = customer asks about the CURRENT/RIGHT NOW reading. "
    "smalltalk = a short social/conversational or emotional remark aimed "
    "AT the bot/conversation itself — a greeting, thanks, acknowledgment, "
    "reaction, OR a hostile/dismissive remark telling the bot to go away, "
    "be quiet, stating frustration with the bot, etc. (e.g. 'great', "
    "'thanks', 'hello', 'get lost', 'this is useless', 'shut up') — NOT a "
    "genuine question about anything. Use the sentiment field to capture "
    "which kind it is. "
    "not_applicable = a genuine question or request about something "
    "entirely unrelated to Watchman (e.g. weather, trivia, asking for a "
    "poem, gibberish) — NOT a social remark; use smalltalk for those "
    "instead. "
    "general_question = the question is about the WATCHMAN PRODUCT OR "
    "WEBSITE ITSELF rather than this unit's own sensor history — e.g. "
    "pricing, subscriptions, product types, setup, alerts, warranty, or "
    "portal features. Use this instead of not_applicable for anything a "
    "customer support page would normally answer."
)


def build_schema_for_unit(unit_id: int) -> dict:
    metrics = get_unit_metrics(unit_id)
    return {
        "name": "parse_sensor_question",
        "parameters": {
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": ["count_threshold_crossings", "largest_change",
                              "min_max", "average", "current_status",
                              "general_question", "smalltalk", "not_applicable"],
                    "description": QUERY_TYPE_DESCRIPTIONS,
                },
                "metric": {
                    "type": "string",
                    "enum": metrics,
                    "description": "Which sensor metric the question is about. Omit if query_type is not_applicable.",
                },
                "operator": {
                    "type": "string",
                    "enum": ["lt", "gt", "eq"],
                    "description": "Only for count_threshold_crossings / current_status.",
                },
                "threshold": {
                    "type": "number",
                    "description": "Only for count_threshold_crossings / current_status.",
                },
                "time_range_days": {
                    "type": "integer",
                    "description": "How many days back the question covers. Omit if all_time is true.",
                },
                "time_range_phrase": {
                    "type": "string",
                    "description": (
                        "The time period to show back to the customer, "
                        "written exactly as it should read after 'over' in "
                        "a sentence — e.g. 'the last 2 months', 'this "
                        "week', 'yesterday', 'the last 24 hours'. Phrase "
                        "it naturally in the customer's own terms — don't "
                        "just restate time_range_days as '60 days' if they "
                        "said '2 months'. Always include this alongside "
                        "time_range_days. Omit if all_time is true."
                    ),
                },
                "all_time": {
                    "type": "boolean",
                    "description": (
                        "true if the customer asked across the unit's "
                        "ENTIRE history rather than a specific bounded "
                        "period — phrases like 'so far', 'ever', "
                        "'overall', 'all time', 'since I got it', 'has it "
                        "ever'. When true, omit time_range_days and "
                        "time_range_phrase entirely. Defaults to false."
                    ),
                },
                "extreme": {
                    "type": "string",
                    "enum": ["max", "min", "both"],
                    "description": (
                        "Only for min_max. 'max' if the customer asked "
                        "specifically for the highest/maximum reading, "
                        "'min' for lowest/minimum, 'both' if they asked "
                        "for the range or didn't specify (e.g. 'what was "
                        "the max temperature yesterday?' -> max; 'what "
                        "was the range of humidity this week?' -> both)."
                    ),
                },
                "sentiment": {
                    "type": "string",
                    "enum": ["neutral", "positive", "frustrated_or_negative"],
                    "description": (
                        "The customer's emotional tone. 'positive' = happy, "
                        "excited, grateful. 'frustrated_or_negative' = "
                        "annoyed, angry, dismissive, telling the bot to go "
                        "away/be quiet, or otherwise hostile. 'neutral' = "
                        "no clear emotional tone either way. ALWAYS include "
                        "this field, for every query_type, not just "
                        "smalltalk — a real question can still carry "
                        "frustration."
                    ),
                },
            },
            "required": ["query_type", "sentiment"],
        },
    }


# ============================================================
# SECTION 6: LLM PARSER
# ------------------------------------------------------------
# call_chat_completion() calls DeepSeek V3.2 through OpenRouter, using
# the OpenAI-compatible /chat/completions endpoint (OpenRouter mirrors
# the OpenAI SDK/wire format, so no separate SDK is needed — plain
# `requests` is enough). Both parse_question() and
# answer_general_question() call it exactly as before; the function
# signature is unchanged so nothing downstream needed to be touched.
#
# >>> MANUAL: set your real API key via an environment variable (do NOT
# hardcode it as the fallback default below — this file may get pasted,
# shared, or committed).
#   - Get a key at openrouter.ai/keys, then:
#       export OPENROUTER_API_KEY="your_real_key"
#
# >>> COST NOTE: deepseek/deepseek-v3.2 is priced (as of writing) around
# $0.20/M input tokens and $0.31/M output tokens — each parse_question()
# call is well under 1K tokens total, and answer_general_question() is
# a few thousand at most, so on a $5 credit you have a lot of headroom
# (thousands of questions). Still, keep an eye on openrouter.ai/activity
# if you're running this a lot, since actual usage can add up.
# ============================================================

import requests

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")  # >>> MANUAL
if not OPENROUTER_API_KEY:
    raise ValueError(
        "No API key set. Set the OPENROUTER_API_KEY environment variable before running."
    )

OPENROUTER_MODEL = "deepseek/deepseek-v3.2"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Kept as an alias so the DEBUG_PRINT_PARSED line and the __main__ block
# below (which still reference GEMINI_MODEL) don't need separate edits.
GEMINI_MODEL = OPENROUTER_MODEL


def call_chat_completion(messages: list[dict], temperature: float = 0,
                          json_mode: bool = False, max_retries: int = 3,
                          max_tokens: int = 600) -> str:
    """
    Chat completion via OpenRouter (DeepSeek V3.2). Returns the raw text
    response (a string — json_mode still returns a string; caller does
    json.loads(), same as before).
    """
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.post(OPENROUTER_URL, headers=headers,
                                      json=payload, timeout=60)
            if response.status_code >= 400:
                # Raise with the body included — OpenRouter's error
                # messages (bad model slug, insufficient credit, etc.)
                # are far more useful than a bare status code.
                raise RuntimeError(
                    f"OpenRouter returned {response.status_code}: {response.text}"
                )
            data = response.json()
            content = data["choices"][0]["message"]["content"]
            if content is None:
                raise RuntimeError(f"OpenRouter returned no text. Full response: {data}")
            return content
        except Exception as e:
            last_error = e
            msg = str(e).lower()
            # Transient-vs-fatal is judged from the message text since
            # both HTTP-level and request-level exceptions land here.
            is_transient = any(code in msg for code in
                                ("429", "500", "502", "503", "504",
                                 "overloaded", "unavailable", "timeout",
                                 "timed out"))
            if is_transient and attempt < max_retries - 1:
                wait = 2 ** attempt  # 1s, 2s, 4s
                print(f"  (transient error on '{OPENROUTER_MODEL}', "
                      f"retry {attempt + 1}/{max_retries} in {wait}s: {e})")
                time.sleep(wait)
                continue
            raise

    raise last_error


def generate_mood_reply(question: str, mood: str) -> str:
    """Used by the regex-based mood layer (detect_mood_response, up in
    the file above SECTION 2). Generates a short, natural reply tailored
    to the customer's actual wording and detected mood, instead of
    always returning the one fixed sentence from _MOOD_RESPONSES.
    Falls back to that fixed sentence if the API call fails for any
    reason (no credit, network issue, etc.) — a working canned reply
    beats a broken turn."""
    system_prompt = (
        "You are a warm, concise customer support assistant for Watchman, "
        "an IoT sensor monitoring product. The customer just sent a short, "
        "purely conversational/emotional message (not a real support "
        f"question). Their detected mood is: '{mood}'. Write ONE short, "
        "natural reply (1-2 sentences max) that acknowledges how they "
        "seem to feel and gently invites them to ask about their "
        "Watchman if they'd like. Don't ask more than one question, "
        "don't mention that you 'detected' anything — just respond the "
        "way a thoughtful person would."
    )
    try:
        content = call_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.6,  # some natural variation is fine here — not a JSON/factual task
            max_tokens=80,
        )
        return content.strip()
    except Exception as e:
        print(f"  (warning: mood-reply generation failed, using canned fallback: {e})")
        return _MOOD_RESPONSES[mood]


def generate_smalltalk_reply(question: str, sentiment: str) -> str:
    """Used by the LLM-classified 'smalltalk' query_type fallback inside
    _answer_question_core — the safety net for smalltalk phrasings the
    regex mood layer above didn't already catch. Same idea as
    generate_mood_reply(): a dynamic, natural reply instead of always
    the same fixed sentence per sentiment bucket, with the original
    fixed sentence kept as a fallback if the API call fails.

    For frustrated_or_negative specifically, the real contact details
    are handed to the model as a fact to relay VERBATIM (never invented
    or paraphrased into a different email/phone) — same "don't let the
    model make up facts" principle used everywhere else in this file."""
    fallback = {
        "frustrated_or_negative": (
            "Understood — I'll back off. If you'd like help with your "
            "Watchman later, or want to reach a person, tech@pp-code.com "
            "or (732) 410-6771 are there for you."
        ),
        "positive": "Glad to help! Is there anything else I can help you with regarding your Watchman?",
    }.get(sentiment, "Hi there! What can I help you with regarding your Watchman?")

    contact_instruction = ""
    if sentiment == "frustrated_or_negative":
        contact_instruction = (
            " Offer these exact contact details verbatim in case they'd "
            "rather reach a person: tech@pp-code.com or (732) 410-6771. "
            "Do not alter, abbreviate, or invent any other contact method."
        )

    system_prompt = (
        "You are a warm, concise customer support assistant for Watchman, "
        "an IoT sensor monitoring product. The customer just sent a short "
        "smalltalk message (greeting, thanks, acknowledgment, or similar "
        f"small talk). Their detected sentiment is: '{sentiment}'. Write "
        "ONE short, natural reply (1-2 sentences) matching that "
        "sentiment, and invite them to ask about their Watchman."
        + contact_instruction
    )
    try:
        content = call_chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            temperature=0.6,
            max_tokens=80,
        )
        return content.strip()
    except Exception as e:
        print(f"  (warning: smalltalk-reply generation failed, using canned fallback: {e})")
        return fallback


def parse_question(question: str, schema: dict, max_retries: int = 3) -> dict:
    # Few-shot examples: smaller/weaker models are noticeably more reliable
    # at picking the right query_type when shown a couple of concrete
    # examples rather than relying on the enum descriptions alone —
    # especially for general_question vs not_applicable, which read as
    # similar to a model without examples (both are "not about this unit's
    # own sensor history" on the surface).
    #
    # UPDATED (round 2): observed the SAME empty-{} failure on two clean,
    # full-sentence questions ("Is there a mobile app for iOS?" and battery
    # life on a power bank) — so it isn't specifically about terse phrasing.
    # Common thread: both are questions where the ANSWER is uncertain/not
    # confirmed in the knowledge base. Hypothesis: the model is conflating
    # "classify this question" with "do I know the answer", and hedges by
    # not classifying at all when it's unsure of the actual answer — even
    # though those are two separate steps (this function only routes;
    # answer_general_question() is the one responsible for saying "I don't
    # know"). Added an explicit instruction plus two matching examples.
    examples = (
        "Examples:\n"
        'Q: "Does Watchman support 5GHz WiFi?" -> '
        '{"query_type": "general_question", "sentiment": "neutral"}\n'
        'Q: "wifi 5ghz support??" -> '
        '{"query_type": "general_question", "sentiment": "neutral"}\n'
        'Q: "How much does the premium plan cost?" -> '
        '{"query_type": "general_question", "sentiment": "neutral"}\n'
        'Q: "Is there a mobile app for iOS?" -> '
        '{"query_type": "general_question", "sentiment": "neutral"}\n'
        'Q: "What\'s the battery life on a power bank instead of USB?" -> '
        '{"query_type": "general_question", "sentiment": "neutral"}\n'
        'Q: "great" -> '
        '{"query_type": "smalltalk", "sentiment": "positive"}\n'
        'Q: "thanks!" -> '
        '{"query_type": "smalltalk", "sentiment": "positive"}\n'
        'Q: "hi there" -> '
        '{"query_type": "smalltalk", "sentiment": "neutral"}\n'
        'Q: "get lost" -> '
        '{"query_type": "smalltalk", "sentiment": "frustrated_or_negative"}\n'
        'Q: "this bot is useless" -> '
        '{"query_type": "smalltalk", "sentiment": "frustrated_or_negative"}\n'
        'Q: "why is this thing never working, is the temperature above 30 right now" -> '
        '{"query_type": "current_status", "metric": "temperature", '
        '"operator": "gt", "threshold": 30, "sentiment": "frustrated_or_negative"}\n'
        'Q: "What\'s the weather like today?" -> '
        '{"query_type": "not_applicable", "sentiment": "neutral"}\n'
        'Q: "lol nvm, can you write me a poem" -> '
        '{"query_type": "not_applicable", "sentiment": "neutral"}\n'
        'Q: "How many times did the temperature go above 30 this week?" -> '
        '{"query_type": "count_threshold_crossings", "metric": "temperature", '
        '"operator": "gt", "threshold": 30, "time_range_days": 7, '
        '"time_range_phrase": "this week", "sentiment": "neutral"}\n'
        'Q: "what was the maximum temperature yesterday?" -> '
        '{"query_type": "min_max", "metric": "temperature", "extreme": "max", '
        '"time_range_days": 1, "time_range_phrase": "yesterday", "sentiment": "neutral"}\n'
        'Q: "what\'s the lowest humidity been this week?" -> '
        '{"query_type": "min_max", "metric": "humidity", "extreme": "min", '
        '"time_range_days": 7, "time_range_phrase": "this week", "sentiment": "neutral"}\n'
        'Q: "what was the range of temperature this week?" -> '
        '{"query_type": "min_max", "metric": "temperature", "extreme": "both", '
        '"time_range_days": 7, "time_range_phrase": "this week", "sentiment": "neutral"}\n'
        'Q: "what was the max temp in the last 2 months?" -> '
        '{"query_type": "min_max", "metric": "temperature", "extreme": "max", '
        '"time_range_days": 60, "time_range_phrase": "the last 2 months", "sentiment": "neutral"}\n'
        'Q: "what\'s the highest temperature this thing has EVER recorded?" -> '
        '{"query_type": "min_max", "metric": "temperature", "extreme": "max", '
        '"all_time": true, "sentiment": "neutral"}\n'
        'Q: "on which day did the temperature go above 75?" -> '
        '{"query_type": "count_threshold_crossings", "metric": "temperature", '
        '"operator": "gt", "threshold": 75, "sentiment": "neutral"}\n'
        'Q: "which day had the biggest temperature swing this month?" -> '
        '{"query_type": "largest_change", "metric": "temperature", '
        '"time_range_days": 30, "time_range_phrase": "this month", "sentiment": "neutral"}\n\n'
    )
    system_prompt = (
        "You are a query parser for a sensor monitoring system. "
        "Read the customer's question and respond with ONLY a JSON object "
        "matching this schema, no other text:\n\n"
        f"{json.dumps(schema['parameters'], indent=2)}\n\n"
        f"{examples}"
        "IMPORTANT: Your job is ONLY to classify what KIND of question this "
        "is — you do NOT need to know the actual answer, and you should "
        "classify confidently even if you're unsure whether the product "
        "supports what's being asked about or what the correct answer is. "
        "Any question about the Watchman product, its features, pricing, "
        "compatibility, or capabilities is general_question, REGARDLESS of "
        "whether you personally know the answer — a separate step handles "
        "answering (and will say 'I don't know' there if needed, not here). "
        "query_type is REQUIRED in every response, even for short, "
        "informal, fragment-style, or ambiguous input. Never return an "
        "empty object.\n\n"
        "Now classify the actual customer question the same way."
    )
    content = call_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        temperature=0,
        json_mode=True,
        max_retries=max_retries,
        max_tokens=200,  # a query_type + a few short fields never needs more than this
    )
    return json.loads(content)


def answer_general_question(question: str, unit_id: int = None) -> str:
    """
    Handles questions about the Watchman product/website itself,
    grounded ONLY in WATCHMAN_KNOWLEDGE_BASE — never the model's own
    general knowledge, so it can't invent a wrong price, policy, or
    setup step. Same "never trust the model with facts it wasn't
    given" principle as the sensor-data pipeline, just applied to
    prose instead of numbers.

    unit_id (optional): if provided, the customer's actual unit type
    is looked up and given to the model, so it can scope its answer
    to features that are actually relevant to THAT unit rather than
    listing every possible option across all Watchman types (e.g.
    don't bring up relay control for a customer whose unit has no
    relay, unless they specifically ask about relays).
    """
    unit_context = ""
    if unit_id is not None:
        try:
            unit_type = _get_unit_table_config(unit_id)["type"]
            unit_context = (
                f"\n\nThis customer's specific unit is of type '{unit_type}'. "
                "Prefer information relevant to units like theirs, and avoid "
                "listing features/options for OTHER Watchman types that "
                "would likely confuse them (e.g. relay controls for a "
                "non-relay unit), unless they specifically ask about a "
                "different unit type or a general product comparison."
            )
        except Exception:
            # Unit lookup failing shouldn't block a general-question
            # answer — just proceed without the extra scoping context.
            pass

    system_prompt = (
        "You are a customer support assistant for Watchman, an IoT sensor "
        "monitoring product. Answer the customer's question using ONLY the "
        "information below. If the answer isn't covered here, say you "
        "don't know and suggest they check watchman.online or contact "
        "support — do NOT guess or make up an answer.\n\n"
        "NEVER reveal internal-only information: don't mention that "
        "content came from 'internal support responses', internal "
        "documentation, screenshots, or any other internal source — just "
        "answer naturally as a support assistant would. NEVER disclose any "
        "other customer's personal information (names, emails, phone "
        "numbers, device IDs, account details) even if such data appears "
        "anywhere in your context — you have no legitimate reason to share "
        "another customer's data with this customer.\n\n"
        "Keep answers appropriately GENERIC — don't list out every "
        "possible option, feature, or Watchman-type variation unless the "
        "customer's question or context calls for it. Showing irrelevant "
        "options the customer's own unit may not even have is more "
        "confusing than helpful.\n\n"
        "FORMAT any links as markdown hyperlinks, e.g. "
        "[buy.watchman.online](https://buy.watchman.online) rather than "
        "a bare URL, so they render as clickable links.\n\n"
        "It's fine to COMBINE or connect multiple facts that are both "
        "explicitly stated below (e.g. if fact A says a subscription is "
        "tied to a unit's ID, and fact B says transferring a unit moves "
        "everything with it, you may conclude the subscription transfers "
        "too). But do NOT add explanatory rationale, justifications, or "
        "'why this feature exists' commentary that isn't itself stated "
        "below — even if it sounds plausible and helpful. If you're not "
        "citing or directly combining stated facts, leave it out rather "
        "than rounding out the answer with invented color.\n\n"
        "If a block below is explicitly labeled a 'PREFERRED SIMPLE "
        "ANSWER' for the kind of question being asked, use THAT answer "
        "as your response almost verbatim (light rewording for tone is "
        "fine) and do NOT pad it out with extra detail from a longer "
        "'expanded'/FAQ section covering the same topic elsewhere in "
        "the document, even if that longer section is also in your "
        "context below. Only pull in the additional detail if the "
        "customer's question specifically asks for it (e.g. asks about "
        "remote access specifically, or asks a follow-up after already "
        "getting the simple answer).\n\n"
        "If a fact below explicitly says to 'always include' a link or "
        "detail when a certain topic comes up, treat that as a hard "
        "requirement, not an optional nice-to-have — include it even if "
        "you feel your answer is already complete without it. This "
        "applies regardless of how much of the surrounding explanation "
        "you used.\n\n"
        "CRITICAL: some information below is explicitly marked as "
        "uncertain or unconfirmed (e.g. 'not independently confirmed', "
        "'the exact behavior isn't confirmed', 'not yet verified'). When "
        "you use content like this, you MUST preserve that same "
        "uncertainty in your answer — say something like 'this likely "
        "means...' or 'this is believed to...' rather than stating it as "
        "settled fact. Never invent a specific date, version number, or "
        "named update/source to make an answer sound more authoritative "
        "than what's actually written below — only cite a date or version "
        "if it is written verbatim in the information below.\n\n"
        "Watch for this specific pattern: after correctly stating a fact "
        "that's given below, DO NOT add a follow-up sentence guessing what "
        "happens next or how it works mechanically, if that mechanism "
        "isn't stated. Example of what NOT to do — if asked whether logs "
        "come through during an internet outage, and the information below "
        "only says logs can't reach the portal without internet: it is "
        "WRONG to add 'logs will be stored locally and sent once "
        "internet is restored' unless that buffering/retry behavior is "
        "itself written below. Stop as soon as you've stated what's "
        "actually given — a shorter, correct answer beats a longer one "
        "padded with an invented explanation of what happens next."
        f"{unit_context}\n\n"
        f"{select_relevant_kb_sections(question)}"
    )
    content = call_chat_completion(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ],
        temperature=0.2,  # slightly higher than the parser — this is generating prose, not JSON
        max_tokens=800,  # enough for a multi-paragraph answer combining several KB sections
    )
    return content.strip()


# ============================================================
# SECTION 7: QUERY ENGINE
# ------------------------------------------------------------
# UPDATED: build_count_query now also returns the individual
# matching readings (timestamp + value), not just the count — so
# the answer can list out exactly when each crossing happened.
# ============================================================

OPERATORS = {"lt": lambda v, t: v < t, "gt": lambda v, t: v > t, "eq": lambda v, t: v == t}


def _date_window(time_range_days, as_of=None):
    if as_of is None:
        as_of = datetime.now(timezone.utc).replace(tzinfo=None)
    elif as_of.tzinfo is not None:
        as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)

    if time_range_days is None:
        return None, as_of

    start = as_of - timedelta(days=time_range_days)
    return start, as_of


def build_count_query(unit_id, metric, operator, threshold, time_range_days, as_of=None, **_):
    start, end = _date_window(time_range_days, as_of)
    readings = [(r["timestamp"], r["value"]) for r in fetch_logs(unit_id, start, end) if r["metric"] == metric]
    op = OPERATORS[operator]
    matches = [(ts, v) for ts, v in readings if op(v, threshold)]
    return {"count": len(matches), "metric": metric, "operator": operator,
            "threshold": threshold, "days": time_range_days, "matches": matches}


def build_largest_change_query(unit_id, metric, time_range_days, as_of=None, **_):
    start, end = _date_window(time_range_days, as_of)
    readings = [(r["timestamp"], r["value"]) for r in fetch_logs(unit_id, start, end) if r["metric"] == metric]
    by_day = {}
    for ts, val in readings:
        by_day.setdefault(ts.split(" ")[0], []).append(val)
    best_day, best_change, best_low, best_high = None, 0, None, None
    for day, values in by_day.items():
        change = max(values) - min(values)
        if change >= best_change:
            best_day, best_change = day, change
            best_low, best_high = min(values), max(values)
    return {"day": best_day, "change": round(best_change, 2), "low": best_low, "high": best_high,
            "metric": metric, "days": time_range_days}


def build_min_max_query(unit_id, metric, time_range_days, extreme="both", as_of=None, **_):
    start, end = _date_window(time_range_days, as_of)
    readings = [(r["timestamp"], r["value"]) for r in fetch_logs(unit_id, start, end) if r["metric"] == metric]
    if not readings:
        return {"min": None, "max": None, "metric": metric, "days": time_range_days, "extreme": extreme}
    min_ts, min_val = min(readings, key=lambda r: r[1])
    max_ts, max_val = max(readings, key=lambda r: r[1])
    return {"min": min_val, "min_timestamp": min_ts, "max": max_val, "max_timestamp": max_ts,
            "metric": metric, "days": time_range_days, "extreme": extreme}


def build_average_query(unit_id, metric, time_range_days, as_of=None, **_):
    start, end = _date_window(time_range_days, as_of)
    readings = [r["value"] for r in fetch_logs(unit_id, start, end) if r["metric"] == metric]
    avg = round(sum(readings) / len(readings), 2) if readings else None
    return {"average": avg, "count": len(readings), "metric": metric, "days": time_range_days}


def build_current_status_query(unit_id, metric, operator=None, threshold=None, as_of=None, **_):
    start, end = _date_window(1, as_of)
    matching = [r for r in fetch_logs(unit_id, start, end) if r["metric"] == metric]
    if not matching:
        return {"value": None, "metric": metric}
    latest = max(matching, key=lambda r: r["timestamp"])
    result = {"value": latest["value"], "metric": metric, "timestamp": latest["timestamp"]}
    if operator and threshold is not None:
        result["meets_condition"] = OPERATORS[operator](latest["value"], threshold)
        result["operator"], result["threshold"] = operator, threshold
    return result


ROUTER = {
    "count_threshold_crossings": build_count_query,
    "largest_change": build_largest_change_query,
    "min_max": build_min_max_query,
    "average": build_average_query,
    "current_status": build_current_status_query,
}


def run_query(unit_id: int, parsed: dict, as_of=None) -> dict:
    query_type = parsed["query_type"]
    # Only pass through keys the query-builder functions actually expect.
    # "sentiment" (and any other future metadata field on `parsed`) must
    # NOT be forwarded here — it broke build_count_query() previously,
    # since that's the one builder without a **_ catch-all.
    QUERY_PARAM_KEYS = {"metric", "operator", "threshold", "time_range_days", "extreme"}
    kwargs = {k: v for k, v in parsed.items() if k in QUERY_PARAM_KEYS}

    # Every query type except current_status needs a time window, but
    # the LLM sometimes omits time_range_days when the customer didn't
    # mention a period at all (e.g. "on which day does it go above 75?"
    # with no "this week"/"in the last month"). Rather than trust the
    # model to always remember to fill in a default, enforce one here
    # deterministically — same "code computes, LLM only classifies"
    # principle as everywhere else in this pipeline. all_time overrides
    # this entirely: time_range_days becomes None, which _date_window /
    # fetch_logs treat as "no lower bound at all".
    if query_type != "current_status":
        if parsed.get("all_time"):
            kwargs["time_range_days"] = None
        elif "time_range_days" not in kwargs:
            kwargs["time_range_days"] = 30

    result = ROUTER[query_type](unit_id=unit_id, as_of=as_of, **kwargs)
    # Pure display metadata — not used by any builder's math, only by
    # format_answer() below, so it's attached after the call rather than
    # being part of QUERY_PARAM_KEYS.
    result["time_range_phrase"] = parsed.get("time_range_phrase")
    result["all_time"] = bool(parsed.get("all_time"))
    return {"query_type": query_type, "result": result}


# ============================================================
# SECTION 8: FORMATTING
# ------------------------------------------------------------
# UPDATED: count_threshold_crossings now lists each matching
# reading's date/time, not just a bare count.
# ============================================================

OPERATOR_WORDS = {"lt": "below", "gt": "above", "eq": "equal to"}


def _format_timestamp(ts_str: str) -> str:
    """'2026-07-27 08:26:27.123456' -> 'Jul 27, 2026 at 08:26 AM'"""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(ts_str, fmt)
            return parsed.strftime("%b %d, %Y at %I:%M %p")
        except ValueError:
            continue
    return ts_str  # fallback: show it raw rather than fail


def _format_date(date_str: str) -> str:
    """'2026-07-29' -> 'Jul 29, 2026'"""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").strftime("%b %d, %Y")
    except ValueError:
        return date_str


def _period_text(result: dict) -> str:
    """Natural-language period text for the answer sentence, meant to be
    used as 'over {_period_text(result)}'. Prefers the LLM-supplied
    phrase (in the customer's own words, e.g. 'the last 2 months') over
    a raw day count — this is what stops '2 months' from coming back as
    'the last 60 days'. Falls back to a day count only if the phrase is
    somehow missing, and always says 'all time' for all_time queries."""
    if result.get("all_time"):
        return "all time"
    phrase = result.get("time_range_phrase")
    if phrase:
        return phrase.strip()
    days = result.get("days")
    return f"the last {days} days" if days is not None else "all time"


def format_answer(query_type: str, result: dict) -> str:
    if query_type == "count_threshold_crossings":
        header = (f"{result['metric'].capitalize()} went {OPERATOR_WORDS[result['operator']]} "
                   f"{result['threshold']} {result['count']} time(s) over {_period_text(result)}.")
        if result["count"] == 0:
            return header
        lines = [f"  • {_format_timestamp(ts)} — {value}" for ts, value in result["matches"]]
        return header + "\n" + "\n".join(lines)
    if query_type == "largest_change":
        if result["day"] is None:
            return f"No {result['metric']} data found over {_period_text(result)}."
        return (f"The largest {result['metric']} change was {result['change']} on {_format_date(result['day'])} "
                f"(went from {result['low']} to {result['high']} that day, "
                f"over {_period_text(result)}).")
    if query_type == "min_max":
        if result["min"] is None:
            return f"No {result['metric']} data found over {_period_text(result)}."
        extreme = result.get("extreme", "both")
        if extreme == "max":
            return (f"The maximum {result['metric']} over {_period_text(result)} was "
                    f"{result['max']} (on {_format_timestamp(result['max_timestamp'])}).")
        if extreme == "min":
            return (f"The minimum {result['metric']} over {_period_text(result)} was "
                    f"{result['min']} (on {_format_timestamp(result['min_timestamp'])}).")
        return (f"Over {_period_text(result)}, {result['metric']} ranged from "
                f"{result['min']} (on {_format_timestamp(result['min_timestamp'])}) to "
                f"{result['max']} (on {_format_timestamp(result['max_timestamp'])}).")
    if query_type == "average":
        if result["average"] is None:
            return f"No {result['metric']} data found over {_period_text(result)}."
        return (f"The average {result['metric']} over {_period_text(result)} was "
                f"{result['average']}, based on {result['count']} reading(s).")
    if query_type == "current_status":
        if result["value"] is None:
            return f"No recent {result['metric']} reading found."
        base = (f"The most recent {result['metric']} reading was {result['value']} "
                f"(at {_format_timestamp(result['timestamp'])}).")
        if "meets_condition" in result:
            verdict = "is" if result["meets_condition"] else "is not"
            base += f" That {verdict} {OPERATOR_WORDS[result['operator']]} {result['threshold']}."
        return base
    return "Sorry, I couldn't compute an answer for that question."


# ============================================================
# SECTION 9: MAIN PIPELINE
# ============================================================

ALLOWED_KEYS = {"query_type", "metric", "operator", "threshold", "time_range_days",
                 "time_range_phrase", "all_time", "sentiment", "extreme"}
ALLOWED_QUERY_TYPES = set(ROUTER.keys())
ALLOWED_OPERATORS = set(OPERATORS.keys())

# >>> MANUAL: where chat logs get saved. Each line is one JSON record:
# timestamp, user_id, unit_id, question, parsed classification, and the
# final answer. This is a simple log file, not a training mechanism —
# there's no fine-tuning happening here. "Training" the chatbot in this
# architecture means: review these logs, spot a bad answer, and manually
# fix/add the relevant fact in knowledge_base.py (exactly the workflow
# used throughout this project so far) or adjust the prompts in this file.
CHAT_LOG_PATH = "chat_logs.jsonl"


def log_interaction(user_id: str, unit_id: int, question: str,
                     parsed: dict, answer: str) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "unit_id": unit_id,
        "question": question,
        "parsed": parsed,
        "answer": answer,
    }
    try:
        with open(CHAT_LOG_PATH, "a") as f:
            f.write(json.dumps(record) + "\n")
    except OSError as e:
        # Logging failure should never break the actual chat response.
        print(f"  (warning: failed to write chat log: {e})")


def _answer_question_core(user_id: str, unit_id: int, question: str) -> tuple:
    """Returns (answer, parsed_dict) — parsed_dict is None for the
    early-exit cases (unit ownership check) that never reach the parser."""
    if not unit_belongs_to_user(unit_id, user_id):
        return "That unit isn't associated with your account.", None

    schema = build_schema_for_unit(unit_id)
    parsed = parse_question(question, schema)

    if DEBUG_PRINT_PARSED:
        print(f"  [parsed by {GEMINI_MODEL}]: {parsed}")

    if not ALLOWED_KEYS.issuperset(parsed.keys()):
        return "Sorry, I couldn't understand that question — try rephrasing?", parsed

    query_type = parsed.get("query_type")

    # Model returned valid JSON but omitted query_type entirely (observed
    # with nemotron-3-super on terse/fragment-style input like "wifi 5ghz
    # support??" before the few-shot examples above were added to cover
    # this). Distinct from a genuine not_applicable classification — the
    # model didn't classify at all, so say so rather than giving the
    # unrelated "counts, averages, extremes" message that follows below.
    if query_type is None:
        return "Sorry, I couldn't understand that question — try rephrasing?", parsed

    # The LLM explicitly said this question isn't about sensor history —
    # decline cleanly. Checked BEFORE the ALLOWED_QUERY_TYPES check below,
    # since not_applicable/general_question are valid choices but have no
    # matching query-engine function in ROUTER.
    if query_type == "not_applicable":
        return "I can only answer questions about this unit's sensor history, or general questions about Watchman — try rephrasing?", parsed

    # Smalltalk (greetings, thanks, acknowledgments) — tone depends on
    # sentiment, since a hostile "get lost" and a warm "thanks!" both land
    # here if the earlier regex-based mood layer didn't already catch them.
    # This is the fallback net for phrasings not in that hardcoded list.
    if query_type == "smalltalk":
        sentiment = parsed.get("sentiment", "neutral")
        if sentiment == "frustrated_or_negative":
            return ("Understood — I'll back off. If you'd like help with your "
                    "Watchman later, or want to reach a person, tech@pp-code.com "
                    "or (732) 410-6771 are there for you."), parsed
        if sentiment == "positive":
            return "Glad to help! Is there anything else I can help you with regarding your Watchman?", parsed
        return "Hi there! What can I help you with regarding your Watchman?", parsed

    # >>> DESIGN DECISION (flagged for confirmation, not silently assumed):
    # general product/website questions do NOT require an active
    # subscription — they don't touch any customer data, so there's no
    # reason to paywall basic support. Only the unit's own sensor-data
    # features (below) are gated. Worth confirming this is the intended
    # policy before shipping.
    if query_type == "general_question":
        return answer_general_question(question, unit_id=unit_id), parsed

    # Everything below this line is a real sensor-data question —
    # THIS is what the subscription gate protects.
    if get_subscription_status(user_id) != "active":
        return "This feature requires an active subscription on this unit.", parsed

    if query_type not in ALLOWED_QUERY_TYPES:
        return "I can currently answer questions about counts, averages, extremes, and changes — try rephrasing?", parsed
    if "operator" in parsed and parsed["operator"] not in ALLOWED_OPERATORS:
        return "Sorry, I couldn't understand that question — try rephrasing?", parsed

    # Never trust that the LLM actually stayed within the metric enum it
    # was given — response_format="json_object" only guarantees valid
    # JSON syntax, not that it respected the enum.
    valid_metrics = set(schema["parameters"]["properties"]["metric"]["enum"])
    if not valid_metrics:
        return "I don't have sensor-data tracking set up for this unit yet.", parsed
    if parsed.get("metric") not in valid_metrics:
        return (f"This unit doesn't track '{parsed.get('metric')}' — "
                f"it tracks: {', '.join(sorted(valid_metrics))}."), parsed

    outcome = run_query(unit_id=unit_id, parsed=parsed)
    return format_answer(outcome["query_type"], outcome["result"]), parsed


def answer_question(user_id: str, unit_id: int, question: str) -> str:
    """Thin wrapper: runs the real logic, with a lightweight conversational
    mood layer for purely social/emotional messages, then logs every
    interaction. Watchman/database questions still use the existing core
    pipeline unchanged."""
    mood_reply = detect_mood_response(question)
    if mood_reply is not None:
        parsed = {"query_type": "smalltalk", "mood_response": True}
        answer = mood_reply
    else:
        answer, parsed = _answer_question_core(user_id, unit_id, question)

    log_interaction(user_id, unit_id, question, parsed, answer)
    return answer


# ============================================================
# SECTION 10: RUN
# ============================================================

if __name__ == "__main__":
    print(f"[model: {GEMINI_MODEL}]")
    print(f"Question: {QUESTION}")
    print(f"Answer:")
    print(answer_question(user_id=USER_ID, unit_id=UNIT_ID, question=QUESTION))