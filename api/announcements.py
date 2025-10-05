from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import json
import datetime
import requests

# --- BSE endpoints (primary + fallback) ---
BSE_PRIMARY_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_FALLBACK_URL = "https://api.bseindia.com/BseIndiaAPI/api/AnnGetData/w"

# Construct full attachment URL from returned name (if any)
def _attachment_url(name: str) -> str:
    # Many rows provide AttachmentName like '1234567abcd.pdf'
    # Historical convention is /xml-data/corpfiling/AttachLive/<name>
    # We keep it as a best-effort convenience field.
    return f"https://www.bseindia.com/xml-data/corpfiling/AttachLive/{name}"

def _iso_today_india():
    # Asia/Kolkata date for "today's default data"
    # Vercel environment may be UTC; we convert to IST (UTC+5:30).
    now_utc = datetime.datetime.utcnow()
    ist = now_utc + datetime.timedelta(hours=5, minutes=30)
    return ist.date()

def _fmt_bse_date(date_obj: datetime.date) -> str:
    # BSE endpoints use yyyymmdd
    return date_obj.strftime("%Y%m%d")

def _fetch_page(session: requests.Session, url: str, payload: dict, headers: dict) -> dict:
    r = session.get(url, params=payload, headers=headers, timeout=15)
    # Some deployments may require GET; historically both GET with params and POST with JSON worked.
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        # BSE sometimes returns JSON as text with BOM; attempt text->json
        return json.loads(r.text)

def _gather_for_date(date_yyyymmdd: str, page_limit: int = 50) -> list:
    """
    Paginate BSE announcements for the given date.
    Returns a list of rows (dicts) exactly as BSE returns, with a few convenience fields added.
    """
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.bseindia.com/",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection": "keep-alive",
    }

    # Base payload matching the ann.html defaults:
    # - strCat: -1 (all categories)
    # - strType: C (Announcement)
    # - strSearch: P (briefly: "P" behaves like default recent posts filter seen in page calls)
    # - subcategory: '' (none)
    # - strPrevDate, strToDate: same date to get "today's default" bucket
    base_payload = {
        "strCat": "-1",
        "strPrevDate": date_yyyymmdd,
        "strScrip": "",
        "strSearch": "P",
        "strToDate": date_yyyymmdd,
        "strType": "C",
        "subcategory": ""
    }

    rows = []
    with requests.Session() as s:
        # Try primary endpoint with required headers; fallback to older endpoint if the first fails
        endpoint_chain = [BSE_PRIMARY_URL, BSE_FALLBACK_URL]

        for endpoint in endpoint_chain:
            try:
                # Probe page 1 quickly to verify endpoint works
                probe = dict(base_payload)
                probe["pageno"] = 1
                data = _fetch_page(s, endpoint, probe, headers)

                # On success, paginate
                for p in range(1, page_limit + 1):
                    payload = dict(base_payload)
                    payload["pageno"] = p
                    page_data = _fetch_page(s, endpoint, payload, headers)

                    # BSE commonly returns keys like "Table", "Table1" etc.
                    # We unify to the "Table" list if present.
                    table = page_data.get("Table") or page_data.get("table") or []
                    if not table:
                        break

                    # Enrich rows with a convenience pdf_url when ATTACHMENTNAME exists
                    for item in table:
                        attach = item.get("ATTACHMENTNAME") or item.get("AttachmentName")
                        if attach:
                            item["pdf_url"] = _attachment_url(attach)
                        rows.append(item)

                # If we got any rows, stop trying further endpoints
                if rows:
                    break
            except Exception:
                # Try next endpoint in chain
                continue

    return rows

def _ok_json(body: dict, code: int = 200):
    return code, {"Content-Type": "application/json; charset=utf-8"}, json.dumps(body, ensure_ascii=False)

class handler(BaseHTTPRequestHandler):
    """
    Vercel Python Serverless Function:
    - GET /api/announcements
    - Optional query params:
        date=YYYY-MM-DD   (defaults to Asia/Kolkata "today")
        limit_pages=N     (cap pagination; default 50)
    - Response:
        {
          "date": "YYYY-MM-DD",
          "count": <int>,
          "rows": [ ... original BSE fields + pdf_url when available ... ]
        }
    """

    def do_GET(self):
        try:
            parsed = urlparse(self.path)
            qs = parse_qs(parsed.query)

            # Parse date
            if "date" in qs and qs["date"]:
                try:
                    date_obj = datetime.date.fromisoformat(qs["date"][0])
                except Exception:
                    # If bad date, send 400
                    body = {"error": "Invalid date. Use YYYY-MM-DD."}
                    code, headers, text = _ok_json(body, 400)
                    self.send_response(code)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.end_headers()
                    self.wfile.write(text.encode("utf-8"))
                    return
            else:
                date_obj = _iso_today_india()

            page_limit = 50
            if "limit_pages" in qs and qs["limit_pages"]:
                try:
                    page_limit = max(1, min(200, int(qs["limit_pages"][0])))
                except Exception:
                    page_limit = 50

            date_yyyymmdd = _fmt_bse_date(date_obj)
            rows = _gather_for_date(date_yyyymmdd, page_limit=page_limit)

            response = {
                "date": date_obj.isoformat(),
                "count": len(rows),
                "rows": rows
            }

            code, headers, text = _ok_json(response, 200)
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            # Allow your Flutter app (and local dev) to call this
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))
        except Exception as e:
            body = {"error": str(e)}
            code, headers, text = _ok_json(body, 500)
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(text.encode("utf-8"))

    # CORS preflight (if ever needed)
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
