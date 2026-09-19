
import html
import json
import os
import random
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup


# ============================================================
# CONFIG
# ============================================================

COMPANIES_FILE = "./workday/companies.json"
TENANTS_FILE = "tenants.json"
DATABASE_FILE = "jobs.db"

SEARCH_TERMS = ["Software Engineer"]

MAX_YEARS_EXPERIENCE = 5
SCORE_THRESHOLD = 5

# None = scan every matching result.
# Set to an integer if you want to reduce runtime.
MAX_JOBS_PER_COMPANY = None

PAGE_SIZE = 20

# Polite global Workday request throttling.
MIN_REQUEST_DELAY = 0.5
MAX_REQUEST_DELAY = 1.0

REQUEST_TIMEOUT = 20

DISCOVERY_WORKERS = 3
WORKDAY_WORKERS = 3

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


# ============================================================
# SESSION
# ============================================================

session = requests.Session()

session.headers.update(
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
    }
)


# ============================================================
# GLOBAL RATE LIMITER
# ============================================================

_rate_lock = threading.Lock()
_last_request_time = 0.0


def throttle():
    global _last_request_time

    with _rate_lock:
        now = time.monotonic()
        elapsed = now - _last_request_time

        delay = random.uniform(
            MIN_REQUEST_DELAY,
            MAX_REQUEST_DELAY,
        )

        if elapsed < delay:
            time.sleep(delay - elapsed)

        _last_request_time = time.monotonic()


# ============================================================
# HTTP HELPERS
# ============================================================

def workday_request(method, url, **kwargs):
    """
    Workday request with throttling and retries for
    rate limits / temporary server failures.
    """

    max_attempts = 4

    for attempt in range(max_attempts):
        throttle()

        try:
            response = session.request(
                method,
                url,
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )

            if response.status_code in (429, 500, 502, 503, 504):
                if attempt < max_attempts - 1:
                    retry_after = response.headers.get("Retry-After")

                    if retry_after:
                        try:
                            sleep_time = float(retry_after)
                        except ValueError:
                            sleep_time = 2 ** attempt
                    else:
                        sleep_time = 2 ** attempt

                    time.sleep(sleep_time)
                    continue

            return response

        except requests.RequestException:
            if attempt < max_attempts - 1:
                time.sleep(2 ** attempt)
                continue

            return None

    return None


# ============================================================
# FILE HELPERS
# ============================================================

def load_json(path, default):
    if not os.path.exists(path):
        return default

    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        print(f"[WARN] Could not read {path}: {exc}")
        return default


def save_json(path, data):
    temp_path = f"{path}.tmp"

    with open(temp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    os.replace(temp_path, path)


# ============================================================
# COMPANY LIST
# ============================================================

def load_companies():
    data = load_json(COMPANIES_FILE, [])

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        return list(data.keys())

    raise ValueError(
        f"{COMPANIES_FILE} must contain either a list or object."
    )


# ============================================================
# URL / WORKDAY DISCOVERY
# ============================================================

def unwrap_search_url(url):
    """
    Convert search-engine redirect URLs into the actual URL.

    Handles:
      - DuckDuckGo uddg=
      - Google /url?q=
      - Bing /ck/a?...&u=
    """

    if not url:
        return url

    try:
        # Keep unwrapping until the URL stops changing.
        for _ in range(3):
            original = url

            parsed = urlparse(url)
            query = parse_qs(parsed.query)

            # ------------------------------------------------
            # DuckDuckGo / Google
            # ------------------------------------------------

            for key in ("uddg", "q", "url"):
                if key in query and query[key]:
                    candidate = unquote(query[key][0])

                    if candidate.startswith("http"):
                        url = candidate
                        break

            # ------------------------------------------------
            # Bing
            #
            # Bing commonly uses:
            #
            # &u=a1<base64 encoded URL>
            # ------------------------------------------------

            if url == original:
                if "u" in query and query["u"]:

                    encoded = query["u"][0]

                    # Bing's value often starts with "a1"
                    if encoded.startswith("a1"):
                        encoded = encoded[2:]

                    try:
                        import base64

                        # Restore missing base64 padding.
                        encoded += "=" * (
                            (-len(encoded)) % 4
                        )

                        decoded = base64.urlsafe_b64decode(
                            encoded
                        ).decode(
                            "utf-8",
                            errors="ignore",
                        )

                        if decoded.startswith("http"):
                            url = decoded

                    except Exception:
                        pass

            if url == original:
                break

        return url

    except Exception:
        return url

def parse_workday_url(url):
    """
    Extract:

        host
        tenant
        shard
        site

    from URLs such as:

        https://company.wd5.myworkdayjobs.com/en-US/Careers
    """

    url = unwrap_search_url(url)

    try:
        parsed = urlparse(url)

        host = parsed.netloc.lower()

        match = re.match(
            r"^([^.]+)\.(wd\d+)\.myworkdayjobs\.com$",
            host,
        )

        if not match:
            return None

        tenant = match.group(1)
        shard = match.group(2)

        parts = [
            p
            for p in parsed.path.split("/")
            if p
        ]

        if not parts:
            return None

        # Usually:
        #
        # /en-US/Careers
        #
        # or:
        #
        # /Careers

        if parts[0].lower() in {
            "en-us",
            "en-gb",
            "en-ca",
            "de-de",
            "fr-fr",
            "es-es",
            "it-it",
            "nl-nl",
            "pt-br",
            "ja-jp",
            "zh-cn",
        }:
            locale = parts[0]
            site = parts[1] if len(parts) > 1 else None
        else:
            locale = None
            site = parts[0]

        if not site:
            return None

        return {
            "host": host,
            "tenant": tenant,
            "shard": shard,
            "site": site,
            "locale": locale,
        }

    except Exception:
        return None


def validate_workday_site(info):
    """
    Validates the discovered Workday tenant/site by calling
    the public CXS jobs endpoint.
    """

    url = (
        f"https://{info['host']}"
        f"/wday/cxs/{info['tenant']}/{info['site']}/jobs"
    )

    payload = {
        "appliedFacets": {},
        "limit": 1,
        "offset": 0,
        "searchText": "",
    }

    response = workday_request(
        "POST",
        url,
        json=payload,
    )

    if not response:
        return False

    if response.status_code != 200:
        return False

    try:
        response.json()
        return True
    except Exception:
        return False


# ============================================================
# ROBUST WORKDAY DISCOVERY
# ============================================================

def extract_workday_urls_from_html(raw_html):
    """
    Extract Workday URLs from search-engine HTML.

    Handles direct URLs as well as search-engine redirect
    links such as Bing /ck/a URLs.
    """

    if not raw_html:
        return []

    candidates = []

    # --------------------------------------------------------
    # Extract hrefs
    # --------------------------------------------------------

    hrefs = re.findall(
        r'href=["\']([^"\']+)["\']',
        raw_html,
        flags=re.IGNORECASE,
    )

    candidates.extend(hrefs)

    # --------------------------------------------------------
    # Extract plain URLs as a fallback
    # --------------------------------------------------------

    plain_urls = re.findall(
        r'https?://[^"\'>\s<>]+',
        raw_html,
        flags=re.IGNORECASE,
    )

    candidates.extend(plain_urls)

    results = []
    seen = set()

    for candidate in candidates:

        candidate = html.unescape(
            candidate
        )

        candidate = unwrap_search_url(
            candidate
        )

        if not candidate.startswith("http"):
            continue

        if "myworkdayjobs.com" not in candidate.lower():
            continue

        # Clean trailing punctuation.
        candidate = candidate.rstrip(
            '.,);\'">'
        )

        # Make sure it really parses as a Workday URL.
        info = parse_workday_url(
            candidate
        )

        if not info:
            continue

        key = (
            info["host"].lower(),
            info["site"].lower(),
        )

        if key in seen:
            continue

        seen.add(key)
        results.append(candidate)

    return results

def search_duckduckgo(query):
    try:
        response = session.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/139.0 Safari/537.36"
                )
            },
        )

        if response.status_code != 200:
            print(
                f"[DDG] HTTP {response.status_code}"
            )
            return []

        return extract_workday_urls_from_html(
            response.text
        )

    except requests.RequestException as exc:
        print(f"[DDG] Request failed: {exc}")
        return []


def search_bing(query):
    try:
        response = session.get(
            "https://www.bing.com/search",
            params={"q": query},
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/139.0 Safari/537.36"
                )
            },
        )

        if response.status_code != 200:
            print(
                f"[BING] HTTP {response.status_code}"
            )
            return []

        return extract_workday_urls_from_html(
            response.text
        )

    except requests.RequestException as exc:
        print(f"[BING] Request failed: {exc}")
        return []


def search_google(query):
    try:
        response = session.get(
            "https://www.google.com/search",
            params={
                "q": query,
                "num": 10,
            },
            timeout=REQUEST_TIMEOUT,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/139.0 Safari/537.36"
                )
            },
        )

        if response.status_code != 200:
            print(
                f"[GOOGLE] HTTP {response.status_code}"
            )
            return []

        return extract_workday_urls_from_html(
            response.text
        )

    except requests.RequestException as exc:
        print(f"[GOOGLE] Request failed: {exc}")
        return []


def discover_company(company):
    """
    Discover and validate a Workday tenant/site.

    Uses multiple search engines because GitHub Actions
    runners can receive different results from search engines
    than a normal browser.
    """

    existing = load_json(
        TENANTS_FILE,
        {},
    )

    # --------------------------------------------------------
    # Try cached tenant first
    # --------------------------------------------------------

    if company in existing:
        info = existing[company]

        print(
            f"[CACHE CHECK] {company} -> "
            f"{info.get('host')}/{info.get('site')}"
        )

        if validate_workday_site(info):
            print(f"[CACHE] {company}")
            return company, info

        print(
            f"[CACHE INVALID] {company}"
        )

    # --------------------------------------------------------
    # Search queries
    # --------------------------------------------------------

    queries = [
        f'"{company}" site:myworkdayjobs.com',
        f'"{company}" Workday careers',
    ]

    discovered_urls = []

    # --------------------------------------------------------
    # Search engines
    # --------------------------------------------------------

    for query in queries:

        print(
            f"[DISCOVER] {company} | {query}"
        )

        # DuckDuckGo
        throttle()

        urls = search_duckduckgo(query)

        if urls:
            print(
                f"[DDG] {company}: "
                f"{len(urls)} Workday URL(s)"
            )

        discovered_urls.extend(urls)

        # Bing
        if not urls:
            throttle()

            urls = search_bing(query)

            if urls:
                print(
                    f"[BING] {company}: "
                    f"{len(urls)} Workday URL(s)"
                )

            discovered_urls.extend(urls)

        # Google
        if not urls:
            throttle()

            urls = search_google(query)

            if urls:
                print(
                    f"[GOOGLE] {company}: "
                    f"{len(urls)} Workday URL(s)"
                )

            discovered_urls.extend(urls)

        # We have something useful.
        if discovered_urls:
            break

    # --------------------------------------------------------
    # Convert URLs into Workday tenant info
    # --------------------------------------------------------

    discovered = []

    seen = set()

    for url in discovered_urls:

        info = parse_workday_url(url)
        print(f"[PARSED] {company}: {url} -> {info}")
        
        if not info:
            continue

        key = (
            info["host"].lower(),
            info["site"].lower(),
        )

        if key in seen:
            continue

        seen.add(key)
        discovered.append(info)

    # --------------------------------------------------------
    # Validate candidates
    # --------------------------------------------------------

    for info in discovered:

        print(
            f"[CHECK] {company} -> "
            f"{info['host']}/{info['site']}"
        )

        if validate_workday_site(info):

            existing[company] = info

            save_json(
                TENANTS_FILE,
                existing,
            )

            print(
                f"[FOUND] {company}: "
                f"{info['host']}/{info['site']}"
            )

            return company, info

    print(
        f"[MISS] {company}"
    )

    return company, None

# ============================================================
# WORKDAY API
# ============================================================

def build_jobs_endpoint(info):
    return (
        f"https://{info['host']}"
        f"/wday/cxs/{info['tenant']}/{info['site']}/jobs"
    )


def build_detail_url(info, external_path):
    if not external_path:
        return None

    if not external_path.startswith("/"):
        external_path = "/" + external_path

    return (
        f"https://{info['host']}"
        f"/wday/cxs/{info['tenant']}"
        f"/{info['site']}"
        f"/job{external_path}"
    )


def public_job_url(info, external_path):
    if not external_path:
        return None

    if not external_path.startswith("/"):
        external_path = "/" + external_path

    locale = info.get("locale") or "en-US"

    return (
        f"https://{info['host']}"
        f"/{locale}/{info['site']}"
        f"{external_path}"
    )


def search_jobs(info, search_text):
    """
    Retrieves all pages for a search term.
    """

    endpoint = build_jobs_endpoint(info)

    offset = 0
    results = []

    while True:
        payload = {
            "appliedFacets": {},
            "limit": PAGE_SIZE,
            "offset": offset,
            "searchText": search_text,
        }

        response = workday_request(
            "POST",
            endpoint,
            json=payload,
        )

        if not response:
            break

        if response.status_code != 200:
            print(
                f"[WARN] Search failed "
                f"{response.status_code}: {endpoint}"
            )
            break

        try:
            data = response.json()
        except Exception:
            break

        jobs = data.get("jobPostings", [])

        if not jobs:
            break

        results.extend(jobs)

        total = data.get("total")

        if MAX_JOBS_PER_COMPANY is not None:
            if len(results) >= MAX_JOBS_PER_COMPANY:
                results = results[:MAX_JOBS_PER_COMPANY]
                break

        if total is not None and offset + len(jobs) >= total:
            break

        if len(jobs) < PAGE_SIZE:
            break

        offset += PAGE_SIZE

    return results


def fetch_job_detail(info, external_path):
    url = build_detail_url(
        info,
        external_path,
    )

    if not url:
        return None

    response = workday_request(
        "GET",
        url,
    )

    if not response:
        return None

    if response.status_code != 200:
        return None

    try:
        return response.json()
    except Exception:
        return None


# ============================================================
# HTML / TEXT
# ============================================================

def html_to_text(value):
    if not value:
        return ""

    soup = BeautifulSoup(
        value,
        "html.parser",
    )

    text_value = soup.get_text(
        " ",
        strip=True,
    )

    return re.sub(
        r"\s+",
        " ",
        text_value,
    ).strip()


# ============================================================
# JOB ID
# ============================================================

def extract_job_id(job):
    candidates = [
        job.get("jobId"),
        job.get("bulletFields", [None])[0]
        if isinstance(job.get("bulletFields"), list)
        else None,
        job.get("externalPath"),
    ]

    for value in candidates:
        if value:
            return str(value)

    return None


# ============================================================
# SCORING
# ============================================================

TECH_PATTERNS = {
    "React": re.compile(
        r"\breact(?:\.js)?\b",
        re.IGNORECASE,
    ),
    "JavaScript": re.compile(
        r"\bjavascript\b",
        re.IGNORECASE,
    ),
    "TypeScript": re.compile(
        r"\btypescript\b",
        re.IGNORECASE,
    ),
    "Java": re.compile(
        r"\bjava\b",
        re.IGNORECASE,
    ),
    "Spring Boot": re.compile(
        r"\bspring\s+boot\b",
        re.IGNORECASE,
    ),
    "Node.js": re.compile(
        r"\bnode(?:\.js)?\b",
        re.IGNORECASE,
    ),
    "AWS": re.compile(
        r"\baws\b|\bamazon web services\b",
        re.IGNORECASE,
    ),
}


TECH_SCORES = {
    "React": 4,
    "JavaScript": 3,
    "TypeScript": 4,
    "Java": 3,
    "Spring Boot": 4,
    "Node.js": 4,
    "AWS": 3,
}


DOTNET_PATTERNS = [
    re.compile(r"\.net\b", re.IGNORECASE),
    re.compile(r"\basp\.net\b", re.IGNORECASE),
    re.compile(r"\bc#\b", re.IGNORECASE),
    re.compile(r"\bcsharp\b", re.IGNORECASE),
]


TITLE_PENALTIES = {
    "staff": -5,
    "principal": -6,
    "architect": -6,
    "director": -8,
    "manager": -8,
    "senior": -2,
}


def count_pattern(pattern, text):
    return len(pattern.findall(text))


def score_job(title, description):
    title_lower = title.lower()
    body_lower = description.lower()

    score = 0
    reasons = []

    # --------------------------------------------------------
    # Desired technologies
    # --------------------------------------------------------

    for tech, pattern in TECH_PATTERNS.items():
        if pattern.search(description):
            points = TECH_SCORES[tech]
            score += points
            reasons.append(f"+{points} {tech}")

    # --------------------------------------------------------
    # .NET / C# conflict
    # --------------------------------------------------------

    dotnet_title_hits = 0

    for pattern in DOTNET_PATTERNS:
        dotnet_title_hits += count_pattern(
            pattern,
            title,
        )

    dotnet_body_hits = 0

    for pattern in DOTNET_PATTERNS:
        dotnet_body_hits += count_pattern(
            pattern,
            description,
        )

    if dotnet_title_hits > 0:
        score -= 10
        reasons.append("-10 C#/.NET in title")

    elif dotnet_body_hits >= 3:
        score -= 7
        reasons.append("-7 strong C#/.NET emphasis")

    elif dotnet_body_hits >= 1:
        score -= 2
        reasons.append("-2 incidental C#/.NET mention")

    # --------------------------------------------------------
    # Title penalties
    # --------------------------------------------------------

    for word, penalty in TITLE_PENALTIES.items():
        if re.search(
            rf"\b{re.escape(word)}\b",
            title_lower,
        ):
            score += penalty
            reasons.append(
                f"{penalty} {word.title()} title"
            )

    # --------------------------------------------------------
    # Experience
    # --------------------------------------------------------

    experience_penalty = get_experience_penalty(
        description,
    )

    if experience_penalty:
        score += experience_penalty
        reasons.append(
            f"{experience_penalty} experience requirement"
        )

    return score, reasons


# ============================================================
# EXPERIENCE PARSING
# ============================================================

EXPERIENCE_PATTERNS = [
    re.compile(
        r"(\d+)\s*(?:\+|or more)?\s*years?"
        r"(?:\s+of)?\s+(?:professional\s+)?experience",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:minimum|at least)\s+(\d+)\s*years?",
        re.IGNORECASE,
    ),
    re.compile(
        r"(\d+)\s*[-–]\s*(\d+)\s*years?"
        r"(?:\s+of)?\s+(?:professional\s+)?experience",
        re.IGNORECASE,
    ),
]


def get_experience_penalty(description):
    """
    Finds explicit experience requirements.

    0-5 years -> acceptable
    6 years    -> -5
    7 years    -> -7
    8 years    -> -9
    9-10       -> -12
    >10        -> -15
    """

    years_found = []

    for pattern in EXPERIENCE_PATTERNS:
        for match in pattern.finditer(description):
            groups = match.groups()

            try:
                if len(groups) == 1:
                    years = int(groups[0])
                    years_found.append(years)

                elif len(groups) == 2:
                    low = int(groups[0])
                    high = int(groups[1])

                    # A range such as 2-5 is okay.
                    # Penalize based on the upper bound.
                    years_found.append(high)

            except (ValueError, TypeError):
                continue

    if not years_found:
        return 0

    max_years = max(years_found)

    if max_years <= MAX_YEARS_EXPERIENCE:
        return 0

    if max_years == 6:
        return -5

    if max_years == 7:
        return -7

    if max_years == 8:
        return -9

    if max_years <= 10:
        return -12

    return -15


# ============================================================
# JOB NORMALIZATION
# ============================================================

def normalize_job(company, info, listing, detail):
    posting_info = {}

    if isinstance(detail, dict):
        posting_info = detail.get(
            "jobPostingInfo",
            {},
        )

    if not isinstance(posting_info, dict):
        posting_info = {}

    external_path = (
        posting_info.get("externalPath")
        or listing.get("externalPath")
    )

    title = (
        posting_info.get("title")
        or listing.get("title")
        or ""
    )

    description_html = (
        posting_info.get("jobDescription")
        or detail.get("jobDescription", "")
        if isinstance(detail, dict)
        else ""
    )

    description = html_to_text(
        description_html,
    )

    location = (
        posting_info.get("location")
        or listing.get("locationsText")
        or listing.get("location")
        or ""
    )

    date_posted = (
        posting_info.get("postedOn")
        or listing.get("postedOn")
        or ""
    )

    start_date = (
        posting_info.get("startDate")
        or ""
    )

    job_id = (
        extract_job_id(detail)
        if isinstance(detail, dict)
        else None
    )

    if not job_id:
        job_id = extract_job_id(listing)

    if not job_id:
        job_id = external_path

    if not job_id:
        return None

    score, reasons = score_job(
        title,
        description,
    )

    url = public_job_url(
        info,
        external_path,
    )

    return {
        "job_id": str(job_id),
        "company": company,
        "job_title": title.strip(),
        "url": url or "",
        "score": score,
        "date_posted": date_posted,
        "start_date": start_date,
        "location": location,
        "description": description,
        "scoring_reasons": reasons,
    }


# ============================================================
# SQLITE
# ============================================================

def init_db():
    conn = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )

    conn.execute(
        """
        PRAGMA journal_mode=WAL
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id TEXT UNIQUE NOT NULL,
            company TEXT,
            job_title TEXT,
            url TEXT,
            score INTEGER,
            date_posted TEXT,
            start_date TEXT,
            location TEXT,
            description TEXT,
            scoring_reasons TEXT,
            discovered_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def job_exists(job_id):
    conn = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )

    try:
        cursor = conn.execute(
            "SELECT 1 FROM jobs WHERE job_id = ? LIMIT 1",
            (job_id,),
        )

        return cursor.fetchone() is not None

    finally:
        conn.close()


def save_job(job):
    conn = sqlite3.connect(
        DATABASE_FILE,
        timeout=30,
    )

    try:
        existing = conn.execute(
            """
            SELECT score
            FROM jobs
            WHERE job_id = ?
            """,
            (job["job_id"],),
        ).fetchone()

        now = datetime.now(
            timezone.utc
        ).isoformat()

        if existing is None:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id,
                    company,
                    job_title,
                    url,
                    score,
                    date_posted,
                    start_date,
                    location,
                    description,
                    scoring_reasons,
                    discovered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job["job_id"],
                    job["company"],
                    job["job_title"],
                    job["url"],
                    job["score"],
                    job["date_posted"],
                    job["start_date"],
                    job["location"],
                    job["description"],
                    json.dumps(
                        job["scoring_reasons"],
                        ensure_ascii=False,
                    ),
                    now,
                ),
            )

            conn.commit()
            return True

        # Update existing job only if the new score is better.
        if job["score"] >= existing[0]:
            conn.execute(
                """
                UPDATE jobs
                SET
                    company = ?,
                    job_title = ?,
                    url = ?,
                    score = ?,
                    date_posted = ?,
                    start_date = ?,
                    location = ?,
                    description = ?,
                    scoring_reasons = ?
                WHERE job_id = ?
                """,
                (
                    job["company"],
                    job["job_title"],
                    job["url"],
                    job["score"],
                    job["date_posted"],
                    job["start_date"],
                    job["location"],
                    job["description"],
                    json.dumps(
                        job["scoring_reasons"],
                        ensure_ascii=False,
                    ),
                    job["job_id"],
                ),
            )

            conn.commit()

        return False

    finally:
        conn.close()


# ============================================================
# TELEGRAM
# ============================================================

def telegram_enabled():
    return bool(
        TELEGRAM_BOT_TOKEN
        and TELEGRAM_CHAT_ID
    )


def escape_html(value):
    return html.escape(
        str(value or ""),
        quote=True,
    )


def truncate(text_value, max_length):
    text_value = text_value or ""

    if len(text_value) <= max_length:
        return text_value

    return text_value[: max_length - 3] + "..."


def build_telegram_message(jobs, scanned_companies):
    if not jobs:
        return (
            "🔎 <b>Workday Job Scanner</b>\n\n"
            "No new matching Software Engineer roles today.\n\n"
            f"Companies scanned: {scanned_companies}"
        )

    sorted_jobs = sorted(
        jobs,
        key=lambda job: job["score"],
        reverse=True,
    )

    lines = [
        "🚀 <b>New Software Engineer Jobs</b>",
        "",
        f"<b>{len(sorted_jobs)} new matches</b>",
        "",
    ]

    for index, job in enumerate(
        sorted_jobs,
        start=1,
    ):
        title = escape_html(
            truncate(
                job["job_title"],
                100,
            )
        )

        company = escape_html(
            job["company"]
        )

        location = escape_html(
            truncate(
                job["location"],
                120,
            )
        )

        score = job["score"]

        lines.append(
            f"<b>{index}. {title}</b>"
        )

        lines.append(
            f"{company}"
        )

        if location:
            lines.append(
                f"📍 {location}"
            )

        lines.append(
            f"⭐ Score: {score}"
        )

        if job["url"]:
            safe_url = html.escape(
                job["url"],
                quote=True,
            )

            lines.append(
                f'🔗 <a href="{safe_url}">Apply</a>'
            )

        lines.append("")

    lines.append(
        f"Companies scanned: {scanned_companies}"
    )

    return "\n".join(lines)


def send_telegram_message(message):
    if not telegram_enabled():
        print(
            "[INFO] Telegram credentials not configured. "
            "Skipping Telegram notification."
        )
        return False

    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    )

    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        response = requests.post(
            url,
            json=payload,
            timeout=20,
        )

        if response.status_code != 200:
            print(
                "[WARN] Telegram failed:",
                response.text,
            )
            return False

        print("[TELEGRAM] Notification sent.")

        return True

    except requests.RequestException as exc:
        print(
            f"[WARN] Telegram request failed: {exc}"
        )
        return False


# ============================================================
# SCAN ONE COMPANY
# ============================================================

def scan_company(company, info):
    print(f"[SCAN] {company}")

    all_listings = {}

    for search_term in SEARCH_TERMS:
        listings = search_jobs(
            info,
            search_term,
        )

        for listing in listings:
            job_id = extract_job_id(listing)

            if job_id:
                all_listings[job_id] = listing

    print(
        f"[SCAN] {company}: "
        f"{len(all_listings)} postings"
    )

    matches = []

    for listing in all_listings.values():
        external_path = listing.get(
            "externalPath"
        )

        if not external_path:
            continue

        detail = fetch_job_detail(
            info,
            external_path,
        )

        if not detail:
            continue

        job = normalize_job(
            company,
            info,
            listing,
            detail,
        )

        if not job:
            continue

        if job["score"] < SCORE_THRESHOLD:
            continue

        # Save only after passing the score threshold.
        is_new = save_job(job)

        if is_new:
            matches.append(job)

            print(
                f"[MATCH] {company} | "
                f"{job['job_title']} | "
                f"score={job['score']}"
            )

    return matches


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("WORKDAY SOFTWARE ENGINEER SCANNER")
    print("=" * 60)

    if not os.path.exists(COMPANIES_FILE):
        raise FileNotFoundError(
            f"Missing {COMPANIES_FILE}"
        )

    init_db()

    companies = load_companies()

    print(
        f"[INFO] Companies loaded: "
        f"{len(companies)}"
    )

    if telegram_enabled():
        print("[INFO] Telegram notifications: ENABLED")
    else:
        print("[INFO] Telegram notifications: DISABLED")

    # --------------------------------------------------------
    # Discover / validate Workday tenants
    # --------------------------------------------------------

    tenant_results = {}

    with ThreadPoolExecutor(
        max_workers=DISCOVERY_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                discover_company,
                company,
            ): company
            for company in companies
        }

        for future in as_completed(futures):
            company = futures[future]

            try:
                company_name, info = future.result()

                if info:
                    tenant_results[company_name] = info

            except Exception as exc:
                print(
                    f"[ERROR] Discovery failed for "
                    f"{company}: {exc}"
                )

    print(
        f"[INFO] Valid Workday sites: "
        f"{len(tenant_results)}"
    )

    # --------------------------------------------------------
    # Scan jobs
    # --------------------------------------------------------

    new_matches = []

    with ThreadPoolExecutor(
        max_workers=WORKDAY_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                scan_company,
                company,
                info,
            ): company
            for company, info in tenant_results.items()
        }

        for future in as_completed(futures):
            company = futures[future]

            try:
                matches = future.result()
                new_matches.extend(matches)

            except Exception as exc:
                print(
                    f"[ERROR] Scan failed for "
                    f"{company}: {exc}"
                )

    # --------------------------------------------------------
    # Telegram
    # --------------------------------------------------------

    message = build_telegram_message(
        new_matches,
        len(tenant_results),
    )

    # Telegram messages have a size limit.
    # Split into multiple messages if necessary.
    max_message_length = 4000

    if len(message) <= max_message_length:
        send_telegram_message(message)

    else:
        # Send the header first.
        header = (
            "🚀 <b>New Software Engineer Jobs</b>\n\n"
            f"<b>{len(new_matches)} new matches</b>\n"
        )

        send_telegram_message(header)

        sorted_jobs = sorted(
            new_matches,
            key=lambda job: job["score"],
            reverse=True,
        )

        current_message = ""

        for index, job in enumerate(
            sorted_jobs,
            start=1,
        ):
            title = escape_html(
                truncate(
                    job["job_title"],
                    100,
                )
            )

            company = escape_html(
                job["company"]
            )

            location = escape_html(
                truncate(
                    job["location"],
                    120,
                )
            )

            safe_url = html.escape(
                job["url"],
                quote=True,
            )

            block = (
                f"<b>{index}. {title}</b>\n"
                f"{company}\n"
            )

            if location:
                block += f"📍 {location}\n"

            block += (
                f"⭐ Score: {job['score']}\n"
                f'🔗 <a href="{safe_url}">Apply</a>\n\n'
            )

            if (
                len(current_message) + len(block)
                > max_message_length
            ):
                send_telegram_message(
                    current_message
                )
                current_message = ""

            current_message += block

        if current_message:
            send_telegram_message(
                current_message
            )

    # --------------------------------------------------------
    # Summary
    # --------------------------------------------------------

    print("=" * 60)
    print("SCAN COMPLETE")
    print("=" * 60)

    print(
        f"Companies scanned: {len(tenant_results)}"
    )

    print(
        f"New matching jobs: {len(new_matches)}"
    )

    for job in sorted(
        new_matches,
        key=lambda x: x["score"],
        reverse=True,
    ):
        print(
            f"- [{job['score']}] "
            f"{job['company']} | "
            f"{job['job_title']}"
        )


if __name__ == "__main__":
    main()
