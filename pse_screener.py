"""
Loop PSE Resume Screener
Pulls New Candidates from Manatal, scores via Claude, posts to Slack.
Lives in scripts/ alongside check_sentiment.py and slack_sentiment_scan.py

Three Slack messages posted per run:
  🔥 Hot Matches        — tick all or most of the ideal criteria
  ✅ Eligible           — suitable, worth approaching
  ❌ Non-Eligible       — not a fit, with reason
"""

import os
import json
import time
import requests
import tempfile
import datetime
import subprocess
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────

MANATAL_API_KEY   = os.environ["MANATAL_API_KEY"]   # saved as MANATAL_API_KEY in GitHub secrets
SLACK_BOT_TOKEN   = os.environ["SLACK_BOT_TOKEN"]
PSE_SLACK_CHANNEL = os.environ["PSE_SLACK_CHANNEL"]

JOB_ID    = "3109699"   # Product Support Engineer
STAGE_NEW = "1593030"   # New Candidates stage ID

MANATAL_BASE    = "https://mcp.api.manatal.com/open/v3"
HEADERS_MANATAL = {
    "Authorization": f"Token {MANATAL_API_KEY}",
    "Content-Type":  "application/json",
}

STATE_FILE  = Path(__file__).parent.parent / "pse_state.json"
REPORTS_DIR = Path(__file__).parent.parent / "pse_reports"
REPORTS_DIR.mkdir(exist_ok=True)

# ── State ─────────────────────────────────────────────────────────────────────

def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    cutoff = (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).isoformat() + "Z"
    return {"last_run": cutoff, "seen_candidate_ids": []}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Manatal API with retry ────────────────────────────────────────────────────

def manatal_get(url: str, params: dict = None, max_retries: int = 5) -> dict:
    """
    GET a Manatal URL with exponential backoff on 429.
    Returns the parsed JSON response.
    """
    for attempt in range(max_retries):
        r = requests.get(url, headers=HEADERS_MANATAL, params=params, timeout=20)
        if r.status_code == 429:
            wait = 2 ** attempt  # 1, 2, 4, 8, 16 seconds
            print(f"    [rate limit] waiting {wait}s...")
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise RuntimeError(f"Failed after {max_retries} retries: {url}")

def get_new_candidates(since: str = None) -> list:
    url    = f"{MANATAL_BASE}/matches/"
    params = {
        "job_id":    JOB_ID,
        "stage__in": STAGE_NEW,
        "page_size": 100,
        "ordering":  "-submitted_at",
    }
    if since:
        params["submitted_at__gte"] = since
    results = []
    while url:
        data = manatal_get(url, params)
        results.extend(data["results"])
        url    = data.get("next")
        params = None   # next URL already includes params

    # Client-side filter: only return matches whose current stage is New Candidates
    # stage is a nested object: {"id": 1593030, "name": "New Candidates"}
    before = len(results)
    results = [m for m in results if str(m.get("stage", {}).get("id", "")) == STAGE_NEW]
    after = len(results)
    if before != after:
        print(f"  Stage filter: {before} total → {after} currently in New Candidates (skipped {before - after} moved to other stages)")
    return results

def get_candidate(cid: int) -> dict:
    return manatal_get(f"{MANATAL_BASE}/candidates/{cid}/")

def get_experiences(cid: int) -> list:
    try:
        data = manatal_get(f"{MANATAL_BASE}/candidates/{cid}/experiences/")
        # API returns either {"result": [...]} or a list directly
        if isinstance(data, list):
            return data
        return data.get("result", [])
    except Exception:
        return []

def get_educations(cid: int) -> list:
    try:
        data = manatal_get(f"{MANATAL_BASE}/candidates/{cid}/educations/")
        if isinstance(data, list):
            return data
        return data.get("result", [])
    except Exception:
        return []

# ── Resume extraction ─────────────────────────────────────────────────────────

def extract_resume(url: str) -> str:
    if not url:
        return ""
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            tmp = f.name
        subprocess.run(["curl", "-s", "-L", url, "-o", tmp], timeout=30, check=True)
        result = subprocess.run(
            ["pdftotext", "-layout", tmp, "-"],
            capture_output=True, text=True, timeout=15
        )
        os.unlink(tmp)
        return result.stdout.strip()[:6000]
    except Exception as e:
        return f"[Resume extraction failed: {e}]"

# ═══════════════════════════════════════════════════════════════════════════════
# KEYWORD BANKS — all tuned to Indian job market terminology and real Manatal
# resume data observed across 700+ PSE applicants.
# ═══════════════════════════════════════════════════════════════════════════════

# ── INTERNAL IT — hard-reject signals ────────────────────────────────────────
# These indicate the person supports internal employees, not external customers.
INTERNAL_IT_KEYWORDS = [
    # Technologies that are purely internal IT
    "azure ad", "active directory", "azure active directory", "ad ds",
    "password reset", "password unlock", "account unlock",
    "vpn support", "vpn setup", "vpn configuration", "vpn troubleshoot",
    "laptop imaging", "laptop setup", "laptop configuration", "laptop provisioning",
    "desktop support", "desktop engineer", "desktop technician",
    "hardware support", "hardware troubleshoot", "hardware maintenance",
    "printer support", "printer configuration", "printer troubleshoot",
    "sccm", "intune", "mdm management", "mobile device management",
    "patch management", "os deployment", "windows deployment",
    "group policy", "gpo management",
    "bitlocker", "antivirus management", "endpoint management",
    # Roles that are clearly internal IT
    "it helpdesk", "helpdesk technician", "helpdesk engineer",
    "it support executive", "it support engineer", "it support analyst",
    "it support specialist", "it support officer",
    "end user support", "end user computing", "euc support",
    "l1 it support", "l2 it support", "level 1 it", "level 2 it",
    "network administrator", "network admin", "network engineer",
    "system administrator", "systems administrator", "sysadmin",
    "infrastructure support", "it infrastructure",
    "field support engineer", "field technician", "field engineer",
    "deskside support", "onsite support technician",
    # Internal-facing language
    "internal stakeholder", "internal users", "internal employees",
    "employee tickets", "employee support", "staff support",
    "resolving employee", "supporting employees",
    "user account creation", "user account management", "user provisioning",
    "access control management", "directory services",
    "it asset management", "asset tagging", "asset inventory",
    # Tools used exclusively for internal IT
    "anydesk to employees", "teamviewer to employees",
    "remote desktop to employees", "remote assistance to staff",
    "service desk (internal)", "itsm for internal",
    "servicenow (internal)", "remedy (internal)", "manage engine",
    "manageengine", "spiceworks",
]

# These OVERRIDE internal IT if present — indicates external customers alongside
INTERNAL_IT_OVERRIDE_KEYWORDS = [
    "external customer", "paying customer", "merchant support",
    "b2b client", "saas customer", "end client", "client-facing",
    "customer onboarding", "customer success", "csat",
    "intercom", "freshdesk", "zendesk", "gorgias", "re:amaze",
    "helpscout", "kayako", "zoho desk", "freshservice for customers",
]

# ── STRONG EXTERNAL-ONLY SIGNALS ─────────────────────────────────────────────
# Used as a stricter override for internal_it: these terms only make sense for
# external customer-facing support and never describe internal IT helpdesk work.
# Generic words like "technical support", "support associate", "support tickets"
# are excluded here because internal IT helpdesk roles use identical vocabulary.
STRONG_EXTERNAL_ONLY_KEYWORDS = [
    "csat", "nps", "net promoter", "customer satisfaction score",
    "merchant support", "merchant success", "merchant experience",
    "merchant onboarding", "merchant queries",
    "customer onboarding", "client onboarding",
    "customer success", "client relations", "customer relations",
    "billing support", "billing queries", "payment support",
    "subscription support", "refund handling", "dispute resolution",
    "intercom", "freshdesk", "zendesk", "freshservice",
    "gorgias", "re:amaze", "reamaze", "helpscout", "help scout",
    "kayako", "zoho desk", "desk.com", "groove", "dixa",
    "kustomer", "gladly", "salesforce service cloud",
    "livechat", "live agent", "tawk.to", "olark",
    "saas support", "b2b support", "b2c support",
    "product support", "product queries",
    "external customer", "paying customer", "b2b client", "saas customer",
    "end client", "client-facing",
]

# ── EXTERNAL CUSTOMER SUPPORT — strong positive signals ──────────────────────
EXTERNAL_SUPPORT_KEYWORDS = [
    # Direct role keywords
    "customer support", "customer service", "customer care",
    "customer success", "client support", "client service",
    "merchant support", "merchant success", "merchant experience",
    "product support", "technical support", "application support",
    "saas support", "b2b support", "b2c support",
    "support engineer", "support specialist", "support executive",
    "support associate", "support analyst", "support representative",
    "support consultant", "support agent", "customer representative",
    "customer experience", "cx specialist", "cx associate",
    "customer relations", "client relations", "account support",
    # Metrics and processes that only exist in external support
    "csat", "nps", "net promoter", "customer satisfaction score",
    "first response time", "frt", "first contact resolution", "fcr",
    "sla compliance", "sla adherence", "response sla", "resolution sla",
    "ticket resolution", "ticket management", "support tickets",
    "case management", "case resolution", "case handling",
    "escalation management", "customer escalation",
    # Onboarding / lifecycle
    "customer onboarding", "merchant onboarding", "client onboarding",
    "user onboarding", "product onboarding",
    "account management", "relationship management",
    "customer retention", "churn prevention", "renewal management",
    # Queries / issues from external customers
    "merchant queries", "customer queries", "client queries",
    "billing support", "billing queries", "payment support",
    "subscription support", "order support", "order management",
    "refund handling", "dispute resolution", "complaint handling",
    "product queries", "feature requests from customers",
    # Tools primarily used for external customer support
    "intercom", "freshdesk", "zendesk", "freshservice",
    "gorgias", "re:amaze", "reamaze", "helpscout", "help scout",
    "kayako", "zoho desk", "desk.com", "groove", "dixa",
    "kustomer", "gladly", "salesforce service cloud",
    "hubspot service", "hubspot crm for support",
    "freshworks crm", "zoho crm for support",
    "livechat", "live agent", "tawk.to", "olark",
    "chat support tool", "support platform",
]

# ── LIVE CHAT — strong hot-match signal ──────────────────────────────────────
LIVE_CHAT_KEYWORDS = [
    "live chat", "livechat", "live-chat",
    "chat support", "chat-based support", "real-time chat",
    "instant chat", "chat handling", "concurrent chats",
    "intercom", "intercom chat", "freshchat", "freshdesk chat",
    "zendesk chat", "zendesk messaging",
    "hubspot live chat", "tidio", "crisp chat", "drift chat",
    "olark", "pure chat", "tawk.to", "smartsupp",
    "whatsapp support", "whatsapp business support",
    "messenger support", "instagram dm support",
    "chat queue", "chat volume", "chats per hour",
    "concurrent chat", "multiple chats simultaneously",
    "real time support", "real-time support",
    "instant messaging support",
]

# ── EXCEL / SHEETS — mandatory gate ──────────────────────────────────────────
EXCEL_KEYWORDS = [
    "excel", "ms excel", "microsoft excel", "advanced excel",
    "google sheets", "google sheet", "gsheets",
    "spreadsheet", "spreadsheets",
    "pivot table", "pivot tables", "pivottable",
    "vlookup", "xlookup", "hlookup", "index match",
    "excel macros", "excel vba", "vba macro",
    "excel reporting", "excel dashboard", "excel analysis",
    "data entry excel", "excel formulas", "conditional formatting",
]

# ── SHOPIFY — highest-value signal ───────────────────────────────────────────
SHOPIFY_KEYWORDS = [
    "shopify", "shopify plus", "shopify partner",
    "shopify merchant", "shopify store", "shopify app",
    "shopify admin", "shopify liquid", "shopify theme",
    "shopify ecosystem", "shopify platform",
]

# ── LOCATION — Delhi NCR ─────────────────────────────────────────────────────
DELHI_NCR_KEYWORDS = [
    "delhi", "new delhi", "ncr", "delhi ncr",
    "gurgaon", "gurugram", "noida", "greater noida",
    "faridabad", "ghaziabad", "manesar", "dwarka",
    "rohini", "janakpuri", "saket", "lajpat nagar",
    "connaught place", "cp delhi", "south delhi", "east delhi",
]

# ── NIGHT SHIFT / US HOURS ───────────────────────────────────────────────────
NIGHT_SHIFT_KEYWORDS = [
    "night shift", "night-shift", "graveyard shift",
    "us shift", "us hours", "us timings", "us time zone",
    "usa shift", "american shift", "night hours",
    "rotational shift", "rotating shift", "24x7", "24/7 support",
    "round the clock", "anz shift", "australia shift",
    "uk shift", "uk hours", "us/uk shift",
    "pst", "est shift", "cst shift",
    "flexible shift", "willing to work night",
    "comfortable with night", "open to night",
]

# ── SLA / FRT ────────────────────────────────────────────────────────────────
SLA_FRT_KEYWORDS = [
    "sla", "service level agreement", "sla compliance", "sla breach",
    "frt", "first response time", "response time",
    "first contact resolution", "fcr",
    "tat", "turnaround time",
    "response sla", "resolution sla",
    "mttr", "mean time to resolve",
    "queue management", "ticket sla",
    "within sla", "sla targets", "sla metrics",
    "aht", "average handling time",
    "response rate", "resolution rate",
]

# ── HTML / CSS ───────────────────────────────────────────────────────────────
HTML_CSS_KEYWORDS = [
    "html", "html5", "css", "css3",
    "html/css", "html & css", "html and css",
    "front-end", "frontend", "web design",
    "bootstrap", "tailwind",
    "inspect element", "browser console",
    "liquid template", "shopify liquid",
    "template editing", "widget customization",
]

# ── JAVASCRIPT ───────────────────────────────────────────────────────────────
JS_KEYWORDS = [
    "javascript", "js ", "es6", "es2015",
    "node.js", "nodejs", "node js",
    "typescript", "ts ",
    "react", "reactjs", "react.js",
    "vue", "vue.js", "vuejs",
    "angular", "jquery",
    "api integration", "rest api", "restful api",
    "json", "xml parsing",
    "browser devtools", "chrome devtools",
    "console debugging", "js debugging",
]

# ── SUPPORT TOOLS ────────────────────────────────────────────────────────────
SUPPORT_TOOLS = {
    # Helpdesk / ticketing
    "Freshdesk": ["freshdesk"],
    "Zendesk":   ["zendesk"],
    "Intercom":  ["intercom"],
    "Gorgias":   ["gorgias"],
    "Hubspot":   ["hubspot"],
    "Salesforce":["salesforce", "sfdc", "service cloud"],
    "Zoho Desk": ["zoho desk", "zoho support"],
    "Freshworks":["freshworks"],
    "Helpscout": ["helpscout", "help scout"],
    "Kayako":    ["kayako"],
    "Reamaze":   ["reamaze", "re:amaze"],
    "Kustomer":  ["kustomer"],
    "Gladly":    ["gladly"],
    "Dixa":      ["dixa"],
    # Project / work management
    "Jira":      ["jira", "jira service"],
    "Asana":     ["asana"],
    "Clickup":   ["clickup", "click up"],
    "Notion":    ["notion"],
    "Trello":    ["trello"],
    "Monday":    ["monday.com", "monday com"],
    # Analytics / reporting
    "Tableau":   ["tableau"],
    "Power BI":  ["power bi", "powerbi"],
    "Looker":    ["looker", "looker studio"],
    "Mixpanel":  ["mixpanel"],
    "Amplitude": ["amplitude"],
    # CRM
    "Zoho CRM":  ["zoho crm"],
    "Pipedrive": ["pipedrive"],
    "Clearbit":  ["clearbit"],
    # Communication
    "Slack":     ["slack"],
    "Teams":     ["microsoft teams", "ms teams"],
}

# ── HUMAN IMPACT — ownership signals ─────────────────────────────────────────
IMPACT_BUILD_KEYWORDS = [
    "built", "build", "created", "developed", "designed", "implemented",
    "set up", "set-up", "established", "launched", "deployed",
    "automated", "automation", "streamlined", "optimized", "improved",
    "reduced", "increased", "saved", "scaled",
]
IMPACT_KB_KEYWORDS = [
    "knowledge base", "knowledge article", "kb article",
    "documentation", "sop", "standard operating procedure",
    "runbook", "playbook", "wiki", "confluence",
    "internal guide", "help article", "faq", "help center",
    "support article", "user guide", "training material",
]
IMPACT_TRAINING_KEYWORDS = [
    "train", "trained", "training", "mentor", "mentored", "mentoring",
    "coached", "coaching", "onboarded", "onboarding new",
    "conducted training", "knowledge transfer", "shadow", "handhold",
    "junior", "new joinee", "new hire training",
]
IMPACT_REPORTING_KEYWORDS = [
    "report", "reporting", "dashboard", "analytics", "analysis",
    "metrics", "kpi", "weekly report", "monthly report",
    "trend analysis", "data analysis", "insights",
    "performance report", "mis", "mis report",
    "csat report", "ticket analysis", "volume analysis",
]

# ── PURE DEV / ENGINEERING — negative signal ──────────────────────────────────
PURE_DEV_KEYWORDS = [
    "software developer", "software engineer", "full stack developer",
    "backend developer", "backend engineer", "frontend developer",
    "frontend engineer", "mobile developer", "ios developer",
    "android developer", "devops engineer", "cloud engineer",
    "data engineer", "ml engineer", "ai engineer",
    "machine learning", "deep learning", "data scientist",
    "blockchain developer", "web3 developer",
    "java developer", "python developer", "react developer",
    "node developer", "golang developer", "rust developer",
]
# Override: dev roles with customer-facing context are still valid
PURE_DEV_OVERRIDE = [
    "developer support", "developer relations", "devrel",
    "technical support", "application support",
    "customer success engineer", "solutions engineer",
    "implementation engineer", "integration engineer",
    "saas support", "product support engineer",
]

# ── INTERNSHIP ROLES ─────────────────────────────────────────────────────────
INTERN_ROLE_KEYWORDS = [
    "intern", "internship", "trainee", "apprentice",
    "graduate trainee", "management trainee",
    "fresher", "entry level", "junior trainee",
]

# ── CURRENT ROLE MISMATCH — hard reject if current/most recent role is clearly off-track
# These people may have had support experience before but have moved away from it
CURRENT_ROLE_MISMATCH_KEYWORDS = [
    "warehouse supervisor", "warehouse manager", "warehouse executive",
    "store manager", "retail manager", "shop manager",
    "driver", "delivery executive", "logistics executive",
    "civil engineer", "mechanical engineer", "electrical engineer",
    "site engineer", "site supervisor", "production engineer",
    "nurse", "staff nurse", "doctor", "medical officer",
    "teacher", "lecturer", "professor",
    "accounts", "accountant", "auditor", "ca ", "chartered accountant",
    "hr manager", "human resources manager", "recruiter",
    "graphic designer", "motion graphic", "video editor",
    "cook", "chef", "hotel management",
]

# ── SENIOR / MANAGEMENT TITLES — hard reject regardless of experience
# We want IC individual contributors in support roles, not people managers or leads
SENIOR_TITLE_KEYWORDS = [
    "team lead", "team leader", "tech lead", "technical lead",
    "lead engineer", "lead analyst", "lead specialist", "lead support",
    "senior manager", "support manager", "customer success manager",
    "assistant manager", "deputy manager", "general manager",
    "director", "vp ", "vice president", "head of", "chief ",
    "manager", " lead ", "team manager", "operations manager",
    "project manager", "product manager", "program manager",
    "delivery manager", "account manager", "client manager",
    "engagement manager", "practice manager",
    # Infosys/TCS/Wipro style levels — not support roles
    "system engineer", "senior system engineer", "senior engineer",
    "systems engineer",
    # Implementation/TAM roles — wrong function for PSE
    "implementation consultant", "technical account",
    "project consultant", "solutions consultant", "solutioning",
]
# These override the senior title check — IC titles that contain "lead" or "manager" words legitimately
SENIOR_TITLE_OVERRIDE = [
    "management trainee",    # entry level
    "team lead support",     # ambiguous but ok
    "technical account",     # TAM = IC role
    "knowledge lead",        # IC
    "shift lead",            # IC ops role
]

# ── CTC BANDS (INR LPA) — with 2 LPA flexibility buffer ──────────────────────
# Designation bands based on YOE
# PSE1 (0-3y): 6-9 LPA  | PSE2 (3-5y): 9-11 LPA  | Sr PSE (5+y): 11-14 LPA
# Lead (5+y): 14-18 LPA | Intern: 3 LPA
# We allow up to 2 LPA above band max before flagging
CTC_FLEXIBILITY_LPA = 2.0
CTC_BANDS = [
    (0.0, 3.0,  9.0  + CTC_FLEXIBILITY_LPA),   # PSE1
    (3.0, 5.0, 11.0  + CTC_FLEXIBILITY_LPA),   # PSE2
    (5.0, 99.0, 14.0 + CTC_FLEXIBILITY_LPA),   # Sr PSE / Lead (use Sr PSE as upper — if they expect >16 they're Lead-track)
]

def check_ctc(candidate: dict, total_yrs: float) -> tuple:
    """
    Returns (ctc_lpa, ctc_ok, ctc_note).
    Extracts expected/current salary from Manatal candidate fields.
    Returns (None, True, '') if no salary data found — don't penalise.
    """
    import re
    # Manatal may store salary in various fields
    raw = None
    for field in ["salary_expectation", "expected_salary", "current_salary",
                  "salary", "expected_ctc", "ctc"]:
        val = candidate.get(field)
        if val:
            raw = str(val)
            break
    # Also check custom_fields dict if present
    custom = candidate.get("custom_fields") or {}
    for key, val in custom.items():
        if any(k in key.lower() for k in ["salary", "ctc", "expected"]):
            if val:
                raw = str(val)
                break

    if not raw:
        return None, True, ""

    # Extract numeric value — handle "12 LPA", "12,00,000", "1200000", "12L", "12 lakhs"
    raw_clean = raw.lower().replace(",", "").strip()
    # Already in LPA format
    m = re.search(r'([\d.]+)\s*(?:lpa|l\.p\.a|lacs?|lakhs?)', raw_clean)
    if m:
        lpa = float(m.group(1))
    else:
        # Try plain number — if > 1000, assume annual rupees, convert
        m = re.search(r'([\d.]+)', raw_clean)
        if not m:
            return None, True, ""
        val = float(m.group(1))
        if val > 100:
            lpa = val / 100000  # paise/rupees to LPA
        else:
            lpa = val  # already LPA

    # Find applicable band
    ctc_ok = True
    ctc_note = ""
    for min_yrs, max_yrs, max_lpa in CTC_BANDS:
        if min_yrs <= total_yrs < max_yrs:
            if lpa > max_lpa:
                ctc_ok = False
                ctc_note = f"Expected CTC {lpa:.1f} LPA exceeds band max {max_lpa - CTC_FLEXIBILITY_LPA:.0f} LPA (+2L buffer = {max_lpa:.0f} LPA)"
            else:
                ctc_note = f"CTC {lpa:.1f} LPA — within band"
            break

    return lpa, ctc_ok, ctc_note

# ── VALID EDUCATION DEGREES ───────────────────────────────────────────────────
# Acceptable: B.Tech, B.E, B.Sc, BCA, M.Tech, M.Sc, MS
VALID_DEGREE_KEYWORDS = [
    # Root words — broadest match
    "engineering",           # B.E, B.Tech, Bachelors/Bachelor of Engineering, etc.
    "science",               # B.Sc, M.Sc, BSc, MSc, Bachelor/Master of Science, etc.
    "bca", "mca",            # computer application degrees
    "computer application",
    "information technology",
    # Explicit B.Tech variants
    "b.tech", "b tech", "btech",
    "bachelor of technology", "bachelors of technology",
    # Explicit B.E variants
    "b.e.", " b.e ", "bachelor of engineering", "bachelors of engineering",
    # Explicit B.Sc variants
    "b.sc", "bsc", "bachelor of science", "bachelors of science",
    # Explicit M.Tech variants
    "m.tech", "m tech", "mtech",
    "master of technology", "masters of technology",
    # Explicit M.Sc / MS variants
    "m.sc", "msc", "master of science", "masters of science",
    "m.s.", " m.s ", " ms ",
    "ms computer", "ms information", "ms data", "ms software",
    # MCA / BCA explicit
    "bachelor of computer application", "bachelors of computer application",
    "master of computer application", "masters of computer application",
]
# Degrees to reject outright (unless Shopify present)
INVALID_DEGREE_KEYWORDS = [
    "b.com", "mba", "llb", "ba ", "b.a.", "b.a ",
    "bba", "diploma", "polytechnic",
    "b.arch", "b.pharm", "nursing", "mbbs",
]

# ── PREFERRED ROLE TITLES ─────────────────────────────────────────────────────
PREFERRED_ROLE_TITLES = [
    "product support engineer", "product support specialist",
    "technical support engineer", "technical support specialist",
    "customer support engineer", "customer support specialist",
    "application support engineer", "application support analyst",
    "merchant support engineer", "merchant success engineer",
    "customer success engineer", "customer success specialist",
    "customer success associate", "customer success manager",
    "support engineer", "support specialist", "support analyst",
    "saas support", "product support associate",
    "developer support engineer", "developer support specialist",
    "implementation engineer", "implementation specialist",
    "solutions engineer", "solution engineer",
    "software support engineer", "software support analyst",
]

# ── CSAT / KPI / FRT QUALITY SIGNALS ─────────────────────────────────────────
# These confirm the person worked in a metrics-driven support role
QUALITY_SIGNALS_KEYWORDS = [
    "csat", "customer satisfaction score", "customer satisfaction",
    "frt", "first response time", "first response",
    "kpi", "kpis", "key performance indicator",
    "knowledge base", "kb article", "help article",
    "nps", "net promoter",
    "tat", "turnaround time",
    "queueing", "ticket queue", "ticket volume",
    "response rate", "resolution rate", "resolution time",
    "aht", "average handle time",
    "best performer", "top performer", "exceeded kpi",
]

# ═══════════════════════════════════════════════════════════════════════════════
# SCORER
# ═══════════════════════════════════════════════════════════════════════════════

def _txt(*parts) -> str:
    """Combine all text fields into one lowercase searchable blob."""
    return " ".join(str(p) for p in parts if p).lower()

def _has(blob: str, *keywords) -> bool:
    return any(k in blob for k in keywords)

def _has_any(blob: str, keyword_list: list) -> bool:
    return any(k in blob for k in keyword_list)

def _count_hits(blob: str, keyword_list: list) -> int:
    return sum(1 for k in keyword_list if k in blob)

def calc_years(exps: list) -> tuple:
    """Return (total_relevant_years, internship_only, has_fulltime)."""
    total        = 0.0
    has_fulltime = False
    has_intern   = False
    for e in exps:
        pos       = (e.get("position") or "").lower()
        is_intern = _has_any(pos, INTERN_ROLE_KEYWORDS)
        s         = e.get("started_at")
        en        = e.get("ended_at")
        if not s:
            continue
        try:
            sy = int(s[:4])
            sm = int(s[5:7]) if len(s) >= 7 else 1
        except Exception:
            continue
        if en:
            try:
                ey = int(en[:4])
                em = int(en[5:7]) if len(en) >= 7 else 12
            except Exception:
                now = datetime.datetime.utcnow()
                ey, em = now.year, now.month
        else:
            now = datetime.datetime.utcnow()
            ey, em = now.year, now.month
        yrs = max(0.0, (ey - sy) + (em - sm) / 12.0)
        if is_intern:
            has_intern = True
            total += yrs * 0.4
        else:
            has_fulltime = True
            total += yrs
    return round(total, 1), (has_intern and not has_fulltime), has_fulltime


def check_grad_year(edus: list, blob: str) -> tuple:
    """
    Returns (grad_year, grad_year_ok).
    grad_year_ok = True if graduation year >= 2020 OR if we can't determine it.
    """
    grad_year = None
    # Try education entries first
    for e in edus:
        end = e.get("ended_at") or e.get("graduation_date") or ""
        if end and len(end) >= 4:
            try:
                y = int(end[:4])
                if 2015 <= y <= 2030:
                    if grad_year is None or y > grad_year:
                        grad_year = y
            except Exception:
                pass
    # Fallback: scan blob for graduation year patterns
    if grad_year is None:
        import re
        # Look for patterns like "2019", "2020", "2021" near degree keywords
        for m in re.finditer(r'\b(201[5-9]|202[0-9])\b', blob):
            y = int(m.group())
            # Only trust year if it's near education context
            ctx = blob[max(0, m.start()-80):m.end()+80]
            if any(k in ctx for k in ["graduate", "graduation", "b.tech", "b tech", "b.e", "btech",
                                       "university", "college", "degree", "engineering", "bsc", "bca",
                                       "m.tech", "mtech", "msc", "cgpa", "gpa", "batch"]):
                if grad_year is None or y > grad_year:
                    grad_year = y
    if grad_year is None:
        return None, True  # unknown grad year — give benefit of the doubt
    return grad_year, grad_year >= 2019


def check_valid_education(edus: list, blob: str) -> bool:
    """Returns True if candidate has an acceptable degree."""
    for e in edus:
        degree = (e.get("degree") or e.get("study_field") or "").lower().strip()
        if _has_any(degree, VALID_DEGREE_KEYWORDS):
            return True
        # Catch standalone "ms", "msc", "bca", "mca" as full degree field value
        if degree in ("ms", "msc", "bsc", "b.sc", "b.sc.", "m.sc", "m.sc.", "bca", "mca", "m.s", "m.s."):
            return True
        # Catch "ms <subject>" pattern (MS from abroad)
        if degree.startswith("ms ") or degree.startswith("m.s "):
            return True
    # Fallback: check blob
    return _has_any(blob, VALID_DEGREE_KEYWORDS)


def check_min_tenure(exps: list) -> bool:
    """Returns True if at least one non-internship job lasted >= 11 months.
    11 months grace period — strict 12 penalises Nov-Oct roles unfairly."""
    for e in exps:
        pos = (e.get("position") or "").lower()
        if _has_any(pos, INTERN_ROLE_KEYWORDS):
            continue
        s  = e.get("started_at")
        en = e.get("ended_at")
        if not s:
            continue
        try:
            sy = int(s[:4]); sm = int(s[5:7]) if len(s) >= 7 else 1
        except Exception:
            continue
        if en:
            try:
                ey = int(en[:4]); em = int(en[5:7]) if len(en) >= 7 else 12
            except Exception:
                now = datetime.datetime.utcnow(); ey, em = now.year, now.month
        else:
            now = datetime.datetime.utcnow(); ey, em = now.year, now.month
        months = (ey - sy) * 12 + (em - sm)
        if months >= 11:
            return True
    return False


def check_preferred_title(blob: str, exps: list) -> bool:
    """Returns True if any role title matches preferred support titles."""
    if _has_any(blob, PREFERRED_ROLE_TITLES):
        return True
    for e in exps:
        pos = (e.get("position") or "").lower()
        if _has_any(pos, PREFERRED_ROLE_TITLES):
            return True
    return False

def check_senior_title(exps: list, current_pos: str) -> bool:
    """Returns True if current/most recent role is a senior/management title."""
    # Check current position first
    pos = current_pos.lower()
    # Check for override first
    if _has_any(pos, SENIOR_TITLE_OVERRIDE):
        return False
    if _has_any(pos, SENIOR_TITLE_KEYWORDS):
        return True
    # Also check most recent experience entry
    if exps:
        recent_pos = (exps[0].get("position") or "").lower()
        if _has_any(recent_pos, SENIOR_TITLE_OVERRIDE):
            return False
        if _has_any(recent_pos, SENIOR_TITLE_KEYWORDS):
            return True
    return False


def check_job_gaps(exps: list) -> tuple:
    """
    Returns (has_gap, max_gap_months).
    Checks for gaps > 6 months between consecutive non-internship jobs.
    Only flags if there are 2+ non-internship jobs to compare.
    """
    import datetime as dt
    dated_exps = []
    for e in exps:
        pos = (e.get("position") or "").lower()
        if _has_any(pos, INTERN_ROLE_KEYWORDS):
            continue
        s  = e.get("started_at")
        en = e.get("ended_at")
        if not s:
            continue
        try:
            sy = int(s[:4]); sm = int(s[5:7]) if len(s) >= 7 else 1
        except Exception:
            continue
        if en:
            try:
                ey = int(en[:4]); em = int(en[5:7]) if len(en) >= 7 else 12
            except Exception:
                now = dt.datetime.utcnow(); ey, em = now.year, now.month
        else:
            now = dt.datetime.utcnow(); ey, em = now.year, now.month
        dated_exps.append((sy * 12 + sm, ey * 12 + em))

    if len(dated_exps) < 2:
        return False, 0

    # Sort by start date descending (most recent first from Manatal)
    # Manatal returns exps most recent first, so we sort ascending to find gaps
    dated_exps.sort(key=lambda x: x[0])

    max_gap = 0
    for i in range(1, len(dated_exps)):
        prev_end   = dated_exps[i-1][1]
        curr_start = dated_exps[i][0]
        gap = curr_start - prev_end
        if gap > max_gap:
            max_gap = gap

    return max_gap > 6, max_gap


def detect_tools(blob: str) -> list:
    found = []
    for label, keywords in SUPPORT_TOOLS.items():
        if _has_any(blob, keywords):
            found.append(label)
    return found

def score_candidate(candidate: dict, exps: list, edus: list, resume: str) -> dict:
    """
    Pure rule-based scoring — zero API calls, runs in milliseconds.
    Built from 700+ observed Indian PSE applicant profiles.

    Hard gates (all must pass unless Shopify overrides education):
      1. Graduation year >= 2020
      2. Valid degree (B.Tech/BE/BSc/BCA/MTech/MSc/MS)
      3. At least one job >= 12 months tenure
      4. >= 6 months external customer support

    Shopify override: if shopify=True, education gate (1+2) is waived.
    """

    # ── Build unified text blob ───────────────────────────────────────────
    exp_text = " ".join(
        f"{e.get('position','')} {e.get('employer','')} {e.get('description','')}"
        for e in exps
    )
    blob = _txt(
        candidate.get("full_name", ""),
        candidate.get("current_position", ""),
        candidate.get("current_company", ""),
        candidate.get("candidate_location", ""),
        candidate.get("description", ""),
        exp_text,
        resume,
    )

    # ── Years of experience ───────────────────────────────────────────────
    total_yrs, intern_only, has_fulltime = calc_years(exps)

    # ── Tool detection ────────────────────────────────────────────────────
    tools = detect_tools(blob)

    # ── Signal flags ──────────────────────────────────────────────────────
    live_chat    = _has_any(blob, LIVE_CHAT_KEYWORDS)
    excel        = _has_any(blob, EXCEL_KEYWORDS)
    # Shopify check: require merchant/support context — not just tech stack listing
    # "Shopify" in a tools list (e.g. "Shopify, Magento, Stripe") doesn't count
    _shopify_raw = _has_any(blob, SHOPIFY_KEYWORDS)
    if _shopify_raw:
        # Check if Shopify appears with support/merchant context nearby
        import re as _re
        _shopify_ctx = False
        for _m in _re.finditer(r'shopify', blob):
            _ctx = blob[max(0, _m.start()-120):_m.end()+120]
            if any(k in _ctx for k in ["merchant", "store", "app", "subscription", "order",
                                        "support", "customer", "checkout", "listing",
                                        "theme", "liquid", "ecommerce", "e-commerce",
                                        "plugin", "partner"]):
                _shopify_ctx = True
                break
        shopify = _shopify_ctx
    else:
        shopify = False
    delhi_ncr    = _has_any(blob, DELHI_NCR_KEYWORDS)
    night_shift  = _has_any(blob, NIGHT_SHIFT_KEYWORDS)
    sla_frt      = _has_any(blob, SLA_FRT_KEYWORDS)
    html_css     = _has_any(blob, HTML_CSS_KEYWORDS)
    js           = _has_any(blob, JS_KEYWORDS)
    quality_sigs = _has_any(blob, QUALITY_SIGNALS_KEYWORDS)
    good_title   = check_preferred_title(blob, exps)

    # ── Education & graduation checks ─────────────────────────────────────
    grad_year, grad_ok   = check_grad_year(edus, blob)
    valid_edu            = check_valid_education(edus, blob)
    # Shopify waives education gates entirely
    if shopify:
        grad_ok   = True
        valid_edu = True
    # Experience override: 6+ years of verified support waives the grad year gate
    # Someone with that much real-world experience has proven more than a year can show
    if total_yrs >= 6 and valid_edu:
        grad_ok = True

    # ── Current role — use experience entries, not Manatal headline ─────────
    # Manatal's current_position is often auto-populated from resume headline
    # or LinkedIn import and can be stale/wrong. Use the most recent experience
    # entry as the source of truth for current role checks.
    if exps:
        current_pos = (exps[0].get("position") or "").lower()
    else:
        # Fallback to headline only if no experience entries at all
        current_pos = (candidate.get("current_position") or "").lower()
    role_mismatch  = _has_any(current_pos, CURRENT_ROLE_MISMATCH_KEYWORDS)
    senior_title   = check_senior_title(exps, current_pos)
    has_gap, max_gap_months = check_job_gaps(exps)
    # Experience ceiling: reject if > 7 years (too senior for IC support role)
    over_experienced = total_yrs > 3.0
    ctc_lpa, ctc_ok, ctc_note = check_ctc(candidate, total_yrs)

    # ── Minimum tenure check ──────────────────────────────────────────────
    has_min_tenure = check_min_tenure(exps)

    # ── Internal IT detection (with override) ─────────────────────────────
    raw_internal_it = _has_any(blob, INTERNAL_IT_KEYWORDS)
    has_override    = _has_any(blob, INTERNAL_IT_OVERRIDE_KEYWORDS)
    for e in exps:
        pos  = (e.get("position") or "").lower()
        desc = (e.get("description") or "").lower()
        emp  = (e.get("employer") or "").lower()
        if _has_any(pos, ["it support", "desktop support", "system admin",
                           "network admin", "it executive", "it engineer",
                           "helpdesk", "help desk"]):
            if _has_any(desc + " " + emp, ["employee", "internal staff",
                                            "end user", "user account",
                                            "active directory", "azure ad",
                                            "laptop", "hardware"]):
                raw_internal_it = True
    internal_it = raw_internal_it and not has_override

    # ── Pure dev detection (with override) ────────────────────────────────
    raw_pure_dev = _has_any(blob, PURE_DEV_KEYWORDS)
    dev_override = _has_any(blob, PURE_DEV_OVERRIDE)
    pure_dev = raw_pure_dev and not dev_override and not _has_any(blob, EXTERNAL_SUPPORT_KEYWORDS)

    # ── Support-role-specific experience check ───────────────────────────────
    # Only count years from experiences where the POSITION itself is support-facing
    support_role_yrs = 0.0
    for _e in exps:
        _pos = (_e.get("position") or "").lower()
        if _has_any(_pos, INTERN_ROLE_KEYWORDS): continue
        if _has_any(_pos, PURE_DEV_KEYWORDS) and not _has_any(_pos, EXTERNAL_SUPPORT_KEYWORDS): continue
        if _has_any(_pos, SENIOR_TITLE_KEYWORDS) and not _has_any(_pos, EXTERNAL_SUPPORT_KEYWORDS): continue
        _s = _e.get("started_at"); _en = _e.get("ended_at")
        if not _s: continue
        try: _sy = int(_s[:4]); _sm = int(_s[5:7]) if len(_s) >= 7 else 1
        except Exception: continue
        if _en:
            try: _ey = int(_en[:4]); _em = int(_en[5:7]) if len(_en) >= 7 else 12
            except Exception: import datetime as _dt; _now = _dt.datetime.utcnow(); _ey, _em = _now.year, _now.month
        else: import datetime as _dt; _now = _dt.datetime.utcnow(); _ey, _em = _now.year, _now.month
        _months = (_ey - _sy) * 12 + (_em - _sm)
        support_role_yrs += max(0, _months) / 12.0
    support_role_yrs = round(support_role_yrs, 1)
    # Shopify also waives the support_role_yrs minimum
    if shopify:
        support_role_yrs = max(support_role_yrs, 2.0)

    # ── External customer support strength ────────────────────────────────
    ext_hits = _count_hits(blob, EXTERNAL_SUPPORT_KEYWORDS)
    if internal_it:
        # Internal IT roles use the same generic "support"/"technical support"/
        # "support tickets" vocabulary as external support. Require strong
        # external-only signals (CSAT, merchant, Intercom/Zendesk/Freshdesk,
        # billing, subscription, etc.) — at least 2 — to override.
        strong_ext_hits = _count_hits(blob, STRONG_EXTERNAL_ONLY_KEYWORDS)
        has_ext_support = strong_ext_hits >= 2
    else:
        # Use support_role_yrs: require at least 2 years in actual support-titled roles
        has_ext_support = (ext_hits >= 2 or (ext_hits == 1 and support_role_yrs >= 2)) and support_role_yrs >= 1.5
    if internal_it and ext_hits <= 1:
        has_ext_support = False

    # ── CHAT SUPPORT SCORE (1-10) ─────────────────────────────────────────
    if internal_it and not has_ext_support:
        chat_score = 2
    elif pure_dev and not has_ext_support:
        chat_score = 2
    elif has_ext_support and live_chat:
        if total_yrs >= 4:   chat_score = 10
        elif total_yrs >= 2: chat_score = 9
        else:                chat_score = 7
    elif has_ext_support:
        if total_yrs >= 4:   chat_score = 8
        elif total_yrs >= 2: chat_score = 6
        elif total_yrs >= 1: chat_score = 5
        else:                chat_score = 4
    elif _has(blob, "customer", "client") and total_yrs >= 1:
        chat_score = 3
    else:
        chat_score = 2

    if intern_only:
        chat_score = min(chat_score, 5)

    # Bonus: quality signals (CSAT, KPI, FRT, KB) lift chat score by 1
    if quality_sigs and chat_score >= 5:
        chat_score = min(chat_score + 1, 10)

    # Bonus: preferred role title lifts by 1
    if good_title and chat_score >= 5:
        chat_score = min(chat_score + 1, 10)

    # ── TECH & TOOLS SCORE (1-10) ─────────────────────────────────────────
    # Excel no longer affects scoring or eligibility
    tech_score = 5
    if excel:     tech_score += 1  # bonus if present, not penalised if absent
    if html_css:  tech_score += 1
    if js:        tech_score += 1
    if shopify:   tech_score += 1
    tech_score += min(len(tools), 2)
    tech_score = min(tech_score, 10)

    # ── HUMAN IMPACT SCORE (1-10) ─────────────────────────────────────────
    impact_score = 2

    build_hits = _count_hits(blob, IMPACT_BUILD_KEYWORDS)
    if build_hits >= 3:    impact_score += 3
    elif build_hits >= 1:  impact_score += 2

    if _has_any(blob, IMPACT_KB_KEYWORDS):        impact_score += 2
    if _has_any(blob, IMPACT_TRAINING_KEYWORDS):  impact_score += 1
    if _has_any(blob, IMPACT_REPORTING_KEYWORDS): impact_score += 1
    if sla_frt:                                   impact_score += 1

    if _has(blob, "ownership", "own the", "led", "led the", "drove",
            "spearheaded", "championed", "managed end to end",
            "independently", "proactively"):
        impact_score += 1

    impact_score = min(impact_score, 10)

    # ── HARD GATE FAILURES ────────────────────────────────────────────────
    fail_grad     = not grad_ok
    fail_edu      = not valid_edu
    fail_tenure   = not has_min_tenure
    # Collect hard gate failure reasons
    hard_failures = []
    if fail_grad:
        yr_str = str(grad_year) if grad_year else "unknown"
        hard_failures.append(f"Graduation year {yr_str} — must be 2019 or later")
    if fail_edu:
        hard_failures.append("Degree not in accepted list (B.Tech/BE/BSc/BCA/MTech/MSc/MS)")
    if fail_tenure:
        hard_failures.append("No single job >= 11 months tenure")
    if role_mismatch:
        hard_failures.append(f"Current role '{current_pos.title()}' is not support-related")
    if senior_title:
        hard_failures.append(f"Current role appears to be a management/lead title — looking for IC support roles")
    if over_experienced:
        hard_failures.append(f"Over {total_yrs} years experience — seeking candidates in the 0-3 year range")
    if not ctc_ok and ctc_lpa is not None:
        hard_failures.append(ctc_note)
    # Job gaps are informational only — not a hard gate

    # ── ELIGIBILITY ───────────────────────────────────────────────────────
    eligible = (
        has_ext_support
        and support_role_yrs >= 0.5
        and not (internal_it and not has_ext_support)
        and not (pure_dev and not has_ext_support)
        and grad_ok
        and valid_edu
        and has_min_tenure
        and not role_mismatch
        and not senior_title
        and not over_experienced
        and ctc_ok
    )

    # ── HOT MATCH ─────────────────────────────────────────────────────────
    bonus_signals = sum([
        sla_frt,
        html_css,
        bool(tools),
        delhi_ncr,
        night_shift,
        js,
        shopify,
        quality_sigs,
        good_title,
    ])
    required_bonus = 1 if shopify else 2
    hot = (
        eligible
        and live_chat
        and 0.5 <= total_yrs <= 3.0
        and bonus_signals >= required_bonus
    )

    # ── OVERALL MATCH LABEL ───────────────────────────────────────────────
    if hot:
        overall = "Hot"
    elif eligible and chat_score >= 7:
        overall = "Strong"
    elif eligible:
        overall = "Moderate"
    else:
        overall = "Weak"

    # ── STRENGTHS & GAPS ─────────────────────────────────────────────────
    strengths, gaps = [], []

    if live_chat:      strengths.append("Live chat confirmed")
    if excel:          strengths.append("Excel/Sheets confirmed")
    if shopify:        strengths.append("Shopify experience — directly relevant")
    if html_css:       strengths.append("HTML/CSS skills")
    if js:             strengths.append("JavaScript/scripting")
    if sla_frt:        strengths.append("SLA/FRT discipline")
    if quality_sigs:   strengths.append("CSAT/KPI/FRT/KB quality signals")
    if good_title:     strengths.append("Strong role title match")
    if delhi_ncr:      strengths.append("Delhi NCR location")
    if night_shift:    strengths.append("Night shift / US hours willing")
    if tools:          strengths.append(f"Tools: {', '.join(tools[:4])}")
    if total_yrs >= 2: strengths.append(f"{total_yrs} years relevant experience")

    # Hard gate gaps first
    gaps.extend(hard_failures)
    # Excel no longer a gate — not added to gaps
    if not live_chat:
        gaps.append("No live chat evidence")
    # Under 2 years no longer shown as rejection reason
    if internal_it and not has_ext_support:
        gaps.append("Internal IT helpdesk only — not external customer-facing")
    if pure_dev and not has_ext_support:
        gaps.append("Pure development role — no customer-facing experience")
    if intern_only:
        gaps.append("Experience is entirely internships")
    if role_mismatch:
        gaps.append("Current role is not support-related — has moved away from support")
    if senior_title:
        gaps.append("Management/lead title — looking for individual contributors")
    if over_experienced:
        gaps.append(f"{total_yrs} years — seeking 0-3 year range for this role")
    if not ctc_ok and ctc_lpa is not None:
        gaps.append(ctc_note)
    elif ctc_lpa is not None and ctc_ok:
        strengths.append(ctc_note)
    # Job gap excluded from display

    # ── NARRATIVE ─────────────────────────────────────────────────────────
    why_match, why_reject = "", ""
    if eligible:
        why_match = (
            f"{total_yrs} years external customer support. "
            + ("Live chat confirmed. " if live_chat else "")
            + ("Shopify experience is directly relevant. " if shopify else "")
            + (f"Uses {', '.join(tools[:2])}. " if tools else "")
        ).strip()
    else:
        primary_gaps = hard_failures[:1] + [g for g in gaps if g not in hard_failures][:1]
        why_reject = ". ".join(primary_gaps) + "." if primary_gaps else "Profile does not meet must-haves."

    return {
        "total_years_experience":  total_yrs,
        "internship_only":         intern_only,
        "internship_note":         "Mostly internship experience" if intern_only else "",
        "live_chat_confirmed":     live_chat,
        "excel_confirmed":         excel,
        "shopify_experience":      shopify,
        "delhi_ncr":               delhi_ncr,
        "night_shift_mentioned":   night_shift,
        "sla_frt_mentioned":       sla_frt,
        "html_css_confirmed":      html_css,
        "js_confirmed":            js,
        "tools_mentioned":         tools,
        "quality_signals":         quality_sigs,
        "preferred_title":         good_title,
        "grad_year":               grad_year,
        "valid_education":         valid_edu,
        "has_min_tenure":          has_min_tenure,
        "chat_support_score":      chat_score,
        "tech_tools_score":        tech_score,
        "human_impact_score":      impact_score,
        "overall_match":           overall,
        "hot_match":               hot,
        "eligible":                eligible,
        "why_match":               why_match,
        "why_reject":              why_reject,
        "shopify_highlight":       "Has Shopify experience — directly relevant." if shopify else "",
        "key_strengths":           strengths[:6],
        "key_gaps":                gaps[:4],
        "role_mismatch":           role_mismatch,
        "ctc_lpa":                 ctc_lpa,
        "ctc_ok":                  ctc_ok,
        "ctc_note":                ctc_note,
        "senior_title":            senior_title,
        "over_experienced":        over_experienced,
        "has_job_gap":             has_gap,
        "max_gap_months":          max_gap_months,
    }


# ── Slack helpers ─────────────────────────────────────────────────────────────

def post_slack(blocks: list, text: str = "") -> dict:
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
        json={"channel": PSE_SLACK_CHANNEL, "text": text, "blocks": blocks},
        timeout=15,
    )
    data = r.json()
    if not data.get("ok"):
        print(f"  Slack error: {data.get('error')} | channel={PSE_SLACK_CHANNEL}")
    return data

def post_reply(thread_ts: str, blocks: list, text: str = "") -> dict:
    r = requests.post(
        "https://slack.com/api/chat.postMessage",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}", "Content-Type": "application/json"},
        json={"channel": PSE_SLACK_CHANNEL, "text": text, "blocks": blocks, "thread_ts": thread_ts},
        timeout=15,
    )
    return r.json()

def bar(val: int) -> str:
    filled = round(val / 10 * 8)
    b = "█" * filled + "░" * (8 - filled)
    return f"{b}  {val}/10"

def signal_tags(s: dict) -> str:
    parts = []
    # Live chat — most important signal, gets a checkmark or warning
    if s.get("live_chat_confirmed"):
        parts.append("✅ Live Chat")
    else:
        parts.append("⚠️ No live chat")
    # Shopify — highest value, gets its own callout
    if s.get("shopify_experience"):
        parts.append("✅ Shopify")
    # Other confirmed signals — plain text, no emoji noise
    if s.get("excel_confirmed"):       parts.append("Excel")
    if s.get("html_css_confirmed"):    parts.append("HTML/CSS")
    if s.get("js_confirmed"):          parts.append("JS")
    if s.get("sla_frt_mentioned"):     parts.append("SLA/FRT")
    if s.get("delhi_ncr"):             parts.append("Delhi NCR")
    if s.get("night_shift_mentioned"): parts.append("Night shift")
    return "  |  ".join(parts) if parts else "No signals detected"

def candidate_thread_block(r: dict, section: str) -> list:
    c   = r["candidate"]
    s   = r["score"]
    cid = c.get("id", "")

    name        = c.get("full_name", "Unknown")
    email       = c.get("email") or ""
    phone       = c.get("phone_number") or ""
    # Signed resume URLs expire — link to Manatal profile instead (always accessible)
    resume_link = f"<https://app.manatal.com/candidates/{cid}|View in Manatal>"

    yrs         = s.get("total_years_experience", "?")
    intern_note = s.get("internship_note", "")
    tools       = ", ".join(s.get("tools_mentioned", [])) or "none detected"
    signals     = signal_tags(s)
    strengths   = "\n".join(f"• {x}" for x in s.get("key_strengths", [])) or "none noted"
    gaps        = "\n".join(f"• {x}" for x in s.get("key_gaps", [])) or "none"

    match_label = s.get("overall_match", "?")
    exp_line    = f"{yrs} yrs exp"
    if intern_note:
        exp_line += f"  ({intern_note})"

    # Reason text only — no duplicate exp line
    reason_text   = s.get("why_match", "") if s.get("eligible") else s.get("why_reject", "")
    reason_suffix = f"\n_{reason_text}_" if reason_text else ""

    contact_parts = [p for p in [email, phone] if p]
    contact_line  = "  |  ".join(contact_parts) if contact_parts else "not provided"

    shopify_note = "  |  ✅ Shopify" if s.get("shopify_experience") else ""
    ctc_note_display = ""
    if s.get("ctc_lpa") is not None:
        ctc_note_display = f"\n_{s.get('ctc_note','')}_"

    # Score summary — compact, single line
    scores_line = (
        f"Chat {s.get('chat_support_score',0)}/10  ·  "
        f"Tech {s.get('tech_tools_score',0)}/10  ·  "
        f"Impact {s.get('human_impact_score',0)}/10"
    )

    blocks = [
        # ── Name + position + exp (reason_text removed from header, shown separately)
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"👤 *<https://app.manatal.com/candidates/{cid}|{name}>*  `{match_label}`\n"
                    f"{c.get('_display_position', c.get('current_position','N/A'))} @ {c.get('current_company','N/A')}\n"
                    f"{exp_line}  |  Applied {r.get('submitted_at','')[:10]}"
                    f"{reason_suffix}"
                )
            }
        },
        # ── Contact + location + resume
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"📬 *Contact*\n{contact_line}"},
                {"type": "mrkdwn", "text": f"*Location*\n{c.get('candidate_location') or 'N/A'}"},
                {"type": "mrkdwn", "text": f"*Resume*\n{resume_link}"},
            ]
        },
        # ── Signals (live chat / shopify flags) + scores on same block
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Signals*\n{signals}{shopify_note}{ctc_note_display}\n_{scores_line}_"
            }
        },
        # ── Tools + Strengths
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"🛠 *Tools*\n{tools}"},
                {"type": "mrkdwn", "text": f"💪 *Strengths*\n{strengths}"},
            ]
        },
        # ── Rejection reasons (only if any)
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Rejection Reasons*\n{gaps}"}
        },
        # ── Thick visual separator between candidates
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "─" * 40}]
        },
        {"type": "divider"},
    ]
    return blocks

# ── CSV export ───────────────────────────────────────────────────────────────

def export_csv(results: list, run_date: str):
    """Write a flat CSV of all scored candidates to pse_reports/."""
    import csv
    filename = f"pse_results_{datetime.datetime.now().strftime('%Y-%m-%d_%H%M')}.csv"
    filepath = REPORTS_DIR / filename

    fieldnames = [
        "overall_match", "hot_match", "eligible",
        "full_name", "current_position", "current_company", "location",
        "total_years_experience", "internship_only",
        "chat_support_score", "tech_tools_score", "human_impact_score",
        "live_chat_confirmed", "excel_confirmed", "shopify_experience",
        "delhi_ncr", "night_shift_mentioned", "sla_frt_mentioned",
        "html_css_confirmed", "js_confirmed", "tools_mentioned",
        "key_strengths", "key_gaps",
        "why_match", "why_reject", "shopify_highlight",
        "manatal_id", "submitted_at",
    ]

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in results:
            c = r["candidate"]
            s = r["score"]
            row = {
                "overall_match":          s.get("overall_match", ""),
                "hot_match":              s.get("hot_match", False),
                "eligible":               s.get("eligible", False),
                "full_name":              c.get("full_name", ""),
                "current_position":       c.get("current_position", ""),
                "current_company":        c.get("current_company", ""),
                "location":               c.get("candidate_location", ""),
                "total_years_experience": s.get("total_years_experience", ""),
                "internship_only":        s.get("internship_only", False),
                "chat_support_score":     s.get("chat_support_score", ""),
                "tech_tools_score":       s.get("tech_tools_score", ""),
                "human_impact_score":     s.get("human_impact_score", ""),
                "live_chat_confirmed":    s.get("live_chat_confirmed", False),
                "excel_confirmed":        s.get("excel_confirmed", False),
                "shopify_experience":     s.get("shopify_experience", False),
                "delhi_ncr":              s.get("delhi_ncr", False),
                "night_shift_mentioned":  s.get("night_shift_mentioned", False),
                "sla_frt_mentioned":      s.get("sla_frt_mentioned", False),
                "html_css_confirmed":     s.get("html_css_confirmed", False),
                "js_confirmed":           s.get("js_confirmed", False),
                "tools_mentioned":        ", ".join(s.get("tools_mentioned", [])),
                "key_strengths":          " | ".join(s.get("key_strengths", [])),
                "key_gaps":               " | ".join(s.get("key_gaps", [])),
                "why_match":              s.get("why_match", ""),
                "why_reject":             s.get("why_reject", ""),
                "shopify_highlight":      s.get("shopify_highlight", ""),
                "manatal_id":             c.get("id", ""),
                "submitted_at":           r.get("submitted_at", ""),
            }
            w.writerow(row)

    print(f"  CSV saved: {filepath} ({len(results)} rows)")
    return filepath

# ── Slack report ──────────────────────────────────────────────────────────────

def send_slack_report(results: list, run_date: str):
    hot      = [r for r in results if r["score"].get("hot_match")]
    eligible = [r for r in results if r["score"].get("eligible") and not r["score"].get("hot_match")]
    rejected = [r for r in results if not r["score"].get("eligible")]

    hot_cnt  = len(hot)
    eli_cnt  = len(eligible)
    rej_cnt  = len(rejected)
    total    = len(results)

    # ── Single summary message ────────────────────────────────────────────────
    summary = post_slack(
        blocks=[
            {"type": "header", "text": {"type": "plain_text", "text": f"PSE Screener — {run_date}"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Total Scanned*\n{total}"},
                    {"type": "mrkdwn", "text": f"*🔥 Hot*\n{hot_cnt}"},
                    {"type": "mrkdwn", "text": f"*✅ Eligible*\n{eli_cnt}"},
                    {"type": "mrkdwn", "text": f"*❌ Rejected*\n{rej_cnt}"},
                ]
            },
            {"type": "divider"},
        ],
        text=f"PSE Screener {run_date} — {total} scanned | {hot_cnt} hot | {eli_cnt} eligible | {rej_cnt} rejected"
    )

    ts = summary.get("ts")
    if not ts:
        print("  Could not post summary. Slack error above.")
        return

    print(f"  Summary posted (thread {ts})")

    # ── All candidates as replies in one thread, sorted Hot → Eligible → Rejected
    def post_section_label(label: str, count: int):
        post_reply(ts, blocks=[
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{label}*  ·  {count} candidate{'s' if count != 1 else ''}"}},
            {"type": "divider"},
        ], text=label)
        time.sleep(0.2)

    sections = [
        ("🔥 Hot Matches",         hot,      "hot"),
        ("✅ Eligible Candidates", eligible, "eligible"),
        ("❌ Rejected",            rejected, "rejected"),
    ]

    for label, items, key in sections:
        post_section_label(label, len(items))
        for r in items:
            blocks = candidate_thread_block(r, key)
            name   = r["candidate"].get("full_name", "?")
            post_reply(ts, blocks, text=name)
            time.sleep(0.3)

    print(f"  {total} candidates posted to thread {ts}")

# ── Main ──────────────────────────────────────────────────────────────────────

def run(full_scan: bool = False):
    print(f"\n{'='*60}")
    print(f"Loop PSE Screener  {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*60)

    if datetime.datetime.utcnow().weekday() >= 5 and not full_scan:
        print("Weekend. Skipping.")
        return

    state    = load_state()
    since    = None if full_scan else state["last_run"]
    seen_ids = set(state.get("seen_candidate_ids", []))

    if full_scan:
        print("Full scan — fetching ALL current New Candidates (no date filter)...")
    else:
        print(f"Fetching New Candidates since {since[:10]}...")
    matches = get_new_candidates(since)
    new     = [m for m in matches if m["candidate"] not in seen_ids]
    print(f"  {len(matches)} in New Candidates stage · {len(new)} not yet scanned")

    if not new:
        print("Nothing new today. Exiting.")
        state["last_run"] = datetime.datetime.utcnow().isoformat() + "Z"
        save_state(state)
        return

    results = []
    for i, match in enumerate(new, 1):
        cid = match["candidate"]
        print(f"\n[{i}/{len(new)}] Candidate {cid}")
        try:
            c    = get_candidate(cid)
            exps = get_experiences(cid)
            edus = get_educations(cid)
            print(f"  {c.get('full_name')} — {c.get('current_position','?')} @ {c.get('current_company','?')}")
            txt  = extract_resume(c.get("resume", ""))
            print(f"  Resume: {len(txt)} chars")
            # Enrich candidate dict with experience-derived title for display
            if exps:
                c["_display_position"] = exps[0].get("position") or c.get("current_position", "N/A")
            s    = score_candidate(c, exps, edus, txt)
            hot  = s.get("hot_match", False)
            eli  = s.get("eligible", False)
            print(
                f"  → {s.get('overall_match')} | Hot:{hot} Eligible:{eli} | "
                f"Chat:{s.get('chat_support_score')} Tech:{s.get('tech_tools_score')} "
                f"Impact:{s.get('human_impact_score')} | "
                f"Excel:{s.get('excel_confirmed')} LiveChat:{s.get('live_chat_confirmed')} "
                f"Shopify:{s.get('shopify_experience')} Yrs:{s.get('total_years_experience')}"
            )
            results.append({"candidate": c, "score": s, "submitted_at": match.get("submitted_at", "")})
            seen_ids.add(cid)
        except Exception as e:
            print(f"  ERROR: {e}")
            # Still mark as seen so we don't retry indefinitely on broken profiles
            seen_ids.add(cid)
            continue

    run_date = datetime.datetime.now().strftime("%d %b %Y")

    if not results:
        print("\nNo new candidates to process.")
        slack_post(SLACK_CHANNEL, [{
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📭 *PSE Screener — {run_date}*\nNo new applications since last run."
            }
        }])
    else:
        REPORTS_DIR.mkdir(exist_ok=True)
        print(f"\nExporting CSV...")
        export_csv(results, run_date)
        print(f"\nPosting to Slack ({len(results)} candidates)...")
        send_slack_report(results, run_date)

    state["last_run"]           = datetime.datetime.utcnow().isoformat() + "Z"
    state["seen_candidate_ids"] = list(seen_ids)
    save_state(state)

    hot_cnt = sum(1 for r in results if r["score"].get("hot_match"))
    eli_cnt = sum(1 for r in results if r["score"].get("eligible") and not r["score"].get("hot_match"))
    rej_cnt = sum(1 for r in results if not r["score"].get("eligible"))
    print(f"\nDone — Scanned:{len(results)}  Hot:{hot_cnt}  Eligible:{eli_cnt}  Rejected:{rej_cnt}")
    print("="*60)


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--full-scan", action="store_true")
    args = p.parse_args()
    run(full_scan=args.full_scan)
