#!/usr/bin/env python3
"""
Entertainment & Media Job Alert
================================
Scrapes 14 company career sites and writes new matching jobs to all_jobs.csv.
Run via GitHub Actions daily — no email, no API keys needed.

Output: all_jobs.csv (append-only) + seen_jobs.txt (dedup tracker)
"""

import csv
import os
import re
import time
from datetime import date

import requests
from bs4 import BeautifulSoup


# ── KEYWORDS ──────────────────────────────────────────────────────────────────

TITLE_KEYWORDS = [
    'distribution', 'partnership', 'partnerships', 'partner',
    'licensing', 'media', 'entertainment', 'content',
]

SENIORITY_KEYWORDS = [
    'manager', 'senior manager', 'sr. manager', 'sr manager',
    'director', 'head of', 'lead', 'vp', 'vice president',
]

EXCLUDE_KEYWORDS = [
    'software engineer', 'data engineer', 'backend', 'frontend',
    'devops', 'machine learning', 'infrastructure engineer',
    'security engineer', 'qa engineer', 'site reliability',
    'content producer', 'content creator', 'content writer', 'copywriter',
]


# ── COMPANIES ─────────────────────────────────────────────────────────────────

COMPANIES = [
    # Workday (CXS JSON API)
    {'name': 'AMC Networks',          'ats': 'workday',         'tenant': 'amcn',       'instance': 'wd5', 'job_board': 'amcnetworks'},
    {'name': 'Disney',                'ats': 'workday',         'tenant': 'disney',     'instance': 'wd5', 'job_board': 'disneycareer'},
    {'name': 'Fox',                   'ats': 'workday',         'tenant': 'fox',        'instance': 'wd1', 'job_board': 'Domestic'},
    {'name': 'Netflix',               'ats': 'workday',         'tenant': 'netflix',    'instance': 'wd1', 'job_board': 'Netflix'},
    {'name': 'Warner Bros Discovery', 'ats': 'workday',         'tenant': 'warnerbros', 'instance': 'wd5', 'job_board': 'global'},
    # Greenhouse
    {'name': 'A24',                   'ats': 'greenhouse',      'slug': 'a24'},
    {'name': 'Crunchyroll',           'ats': 'greenhouse',      'slug': 'crunchyroll'},
    # Lever
    {'name': 'Spotify',               'ats': 'lever',           'slug': 'spotify'},
    # SmartRecruiters
    {'name': 'NBCUniversal',          'ats': 'smartrecruiters', 'company_id': 'NBCUniversal3'},
    {'name': 'Versant',               'ats': 'smartrecruiters', 'company_id': 'Versant3'},
    # ClinchTalent (Roku's career platform — jobs at /jobs/search?page=N)
    {'name': 'Roku',                  'ats': 'clinch',          'url': 'https://www.weareroku.com'},
    # Teamtailor
    {'name': 'BritBox',               'ats': 'teamtailor',      'url': 'https://jointheteam.britboxinternational.com'},
    # SAP SuccessFactors (Paramount — links are /job/title/ID/, NOT /go/title/ID/)
    {'name': 'Paramount',             'ats': 'successfactors',  'url': 'https://careers.paramount.com/go/All-Current-Job-Opportunities/8710000/'},
    # Apple (search page HTML scrape)
    {'name': 'Apple',                 'ats': 'apple'},
]


CSV_PATH      = 'all_jobs.csv'
SEEN_IDS_PATH = 'seen_jobs.txt'

BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    )
}


# ── MATCHING ──────────────────────────────────────────────────────────────────

def is_match(title: str) -> bool:
    if not title:
        return False
    t = title.lower()
    if any(kw in t for kw in EXCLUDE_KEYWORDS):
        return False
    if not any(kw in t for kw in TITLE_KEYWORDS):
        return False
    if not any(kw in t for kw in SENIORITY_KEYWORDS):
        return False
    return True


def is_us_location(loc: str) -> bool:
    """Return True if the location string is blank (unknown) or clearly US-based."""
    if not loc or not loc.strip():
        return True
    l = loc.lower()
    if re.search(r'united states?|\busa\b|\bu\.s\.', l):
        return True
    if re.match(r'^us[-,\s]', l):          # e.g. "US-CA-Burbank"
        return True
    # Two-letter state abbreviation preceded by comma/space
    states = (r'[,\s](al|ak|az|ar|ca|co|ct|de|fl|ga|hi|id|il|in|ia|ks|ky|la|me|md|ma|mi|mn'
              r'|ms|mo|mt|ne|nv|nh|nj|nm|ny|nc|nd|oh|ok|or|pa|ri|sc|sd|tn|tx|ut|vt|va|wa'
              r'|wv|wi|wy|dc)[,\s.]')
    if re.search(states, l, re.I):
        return True
    state_names = [
        'california', 'new york', 'new jersey', 'texas', 'florida', 'washington',
        'illinois', 'pennsylvania', 'georgia', 'north carolina', 'colorado',
        'massachusetts', 'tennessee', 'michigan', 'ohio', 'nevada', 'connecticut',
    ]
    if any(s in l for s in state_names):
        return True
    cities = [
        'new york', 'los angeles', 'culver city', 'santa monica', 'burbank',
        'universal city', 'chicago', 'seattle', 'austin', 'boston',
        'san francisco', 'nashville', 'atlanta', 'stamford', 'englewood cliffs',
        'manhattan', 'brooklyn', 'hoboken', 'jersey city', 'remote',
    ]
    return any(c in l for c in cities)


# ── FETCHERS ──────────────────────────────────────────────────────────────────

def fetch_jobs(company: dict) -> list:
    ats = company['ats']
    if ats == 'workday':         return fetch_workday(company)
    if ats == 'greenhouse':      return fetch_greenhouse(company['slug'])
    if ats == 'lever':           return fetch_lever(company['slug'])
    if ats == 'smartrecruiters': return fetch_smartrecruiters(company['company_id'])
    if ats == 'clinch':          return fetch_clinch(company['url'])
    if ats == 'teamtailor':      return fetch_teamtailor(company['url'])
    if ats == 'successfactors':  return fetch_successfactors(company['url'])
    if ats == 'apple':           return fetch_apple()
    return []


# ── Workday ───────────────────────────────────────────────────────────────────

def fetch_workday(company: dict) -> list:
    """
    Use Playwright (headless Chromium) to establish a real browser session on
    the Workday jobs page — this handles the JS-based CSRF/cookie setup that
    plain requests cannot replicate.  Then paginate via fetch() injected into
    the live browser context so the browser automatically sends the correct
    cookies and CSRF headers; no manual token extraction needed.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    except ImportError:
        print("    Playwright not installed — skipping Workday")
        return []

    tenant    = company['tenant']
    instance  = company['instance']
    job_board = company['job_board']
    base_url  = f"https://{tenant}.{instance}.myworkdayjobs.com"
    api_url   = f"{base_url}/wday/cxs/{tenant}/{job_board}/jobs"
    prime_url = f"{base_url}/en-US/{job_board}/jobs"

    all_postings = []
    seen_paths   = set()
    limit        = 100

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
        )
        context = browser.new_context(
            user_agent=(
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            )
        )
        page = context.new_page()

        # Load the jobs listing page — Workday sets all necessary cookies/CSRF here
        try:
            page.goto(prime_url, wait_until='networkidle', timeout=45000)
            page.wait_for_timeout(2000)
            print(f"    Browser loaded: {prime_url}")
        except PwTimeout:
            print(f"    Timeout loading {prime_url} (continuing anyway)")
        except Exception as e:
            print(f"    Page load error: {e}")

        # Paginate by calling fetch() from within the browser context.
        # Because the browser already has valid session cookies + CSRF state,
        # the Workday API accepts these requests without any manual token handling.
        for offset in range(0, 2000, limit):
            try:
                result = page.evaluate(f"""
                    async () => {{
                        const resp = await fetch('{api_url}', {{
                            method: 'POST',
                            credentials: 'include',
                            headers: {{
                                'Accept':       'application/json',
                                'Content-Type': 'application/json',
                            }},
                            body: JSON.stringify({{
                                appliedFacets: {{}},
                                limit:  {limit},
                                offset: {offset},
                                searchText: ''
                            }})
                        }});
                        if (!resp.ok) return {{ error: resp.status, body: await resp.text() }};
                        return await resp.json();
                    }}
                """)
                if not result:
                    print(f"    Offset {offset}: null result")
                    break
                if 'error' in result:
                    print(f"    Offset {offset}: HTTP {result.get('error')} — {str(result.get('body',''))[:200]}")
                    break
                postings = result.get('jobPostings', [])
                print(f"    Offset {offset}: {len(postings)} postings")
                if not postings:
                    break
                for j in postings:
                    path = j.get('externalPath', '')
                    if not path or path in seen_paths:
                        continue
                    seen_paths.add(path)
                    all_postings.append({
                        'title':      j.get('title', ''),
                        'location':   j.get('locationsText', ''),
                        'url':        base_url + path,
                        'posted':     j.get('postedOn', ''),
                        'department': '',
                        'id':         path.split('/')[-1] or path[:60],
                    })
                if len(postings) < limit:
                    break
                time.sleep(0.3)
            except Exception as e:
                print(f"    Offset {offset} error: {e}")
                break

        browser.close()

    print(f"    Total collected: {len(all_postings)}")
    return all_postings


# ── Greenhouse ────────────────────────────────────────────────────────────────

def fetch_greenhouse(slug: str) -> list:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"    Greenhouse {slug}: HTTP {resp.status_code}")
            return []
        return [{
            'title':      j.get('title', ''),
            'location':   (j.get('location') or {}).get('name', ''),
            'url':        j.get('absolute_url', ''),
            'posted':     (j.get('updated_at') or '')[:10],
            'department': ', '.join(d['name'] for d in (j.get('departments') or [])),
            'id':         str(j.get('id', '')),
        } for j in resp.json().get('jobs', [])]
    except Exception as e:
        print(f"    Greenhouse {slug}: {e}")
        return []


# ── Lever ─────────────────────────────────────────────────────────────────────

def fetch_lever(slug: str) -> list:
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    try:
        resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"    Lever {slug}: HTTP {resp.status_code}")
            return []
        return [{
            'title':      j.get('text', ''),
            'location':   (j.get('categories') or {}).get('location', ''),
            'url':        j.get('hostedUrl', ''),
            'posted':     '',
            'department': (j.get('categories') or {}).get('team', ''),
            'id':         j.get('id', ''),
        } for j in (resp.json() or [])]
    except Exception as e:
        print(f"    Lever {slug}: {e}")
        return []


# ── SmartRecruiters ───────────────────────────────────────────────────────────

def fetch_smartrecruiters(company_id: str) -> list:
    queries = ['distribution', 'partnership', 'partner', 'licensing',
               'media', 'entertainment', 'content']
    seen    = set()
    results = []
    for q in queries:
        try:
            url  = (f"https://api.smartrecruiters.com/v1/companies/{company_id}/postings"
                    f"?limit=100&q={requests.utils.quote(q)}")
            resp = requests.get(url, headers={**BROWSER_HEADERS, 'Accept': 'application/json'}, timeout=15)
            if resp.status_code != 200:
                print(f"    SmartRecruiters {company_id} '{q}': HTTP {resp.status_code}")
                continue
            for j in resp.json().get('content', []):
                jid = j.get('id')
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                loc = j.get('location') or {}
                results.append({
                    'title':      j.get('name', ''),
                    'location':   ', '.join(filter(None, [loc.get('city'), loc.get('region'), loc.get('country')])),
                    'url':        f"https://careers.smartrecruiters.com/{company_id}/{jid}",
                    'posted':     (j.get('releasedDate') or '')[:10],
                    'department': (j.get('department') or {}).get('label', ''),
                    'id':         jid,
                })
        except Exception as e:
            print(f"    SmartRecruiters {company_id} '{q}': {e}")
    return results


# ── ClinchTalent (Roku) ───────────────────────────────────────────────────────

def fetch_clinch(base_url: str) -> list:
    """
    ClinchTalent career sites (used by Roku).
    Jobs are listed at /jobs/search?page=N in a paginated HTML table.
    Job detail links match /jobs/[slug-with-location].
    """
    clean   = base_url.rstrip('/')
    results = []
    seen    = set()

    for page in range(1, 100):
        url = f"{clean}/jobs/search?page={page}"
        try:
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"    Clinch {clean}: HTTP {resp.status_code} on page {page}")
                break
            soup   = BeautifulSoup(resp.text, 'html.parser')
            before = len(results)

            for a in soup.find_all('a', href=re.compile(r'/jobs/[^?#]+')):
                href  = a.get('href', '')
                title = a.get_text(strip=True)
                if not title or not (5 <= len(title) <= 200):
                    continue
                link  = href if href.startswith('http') else clean + href
                jid   = href.rstrip('/').split('/')[-1]
                if not jid or len(jid) < 5 or jid in seen:
                    continue
                seen.add(jid)

                # Location lives in a sibling <td> in the same table row
                parent_row = a.find_parent('tr')
                location   = ''
                if parent_row:
                    cells = parent_row.find_all('td')
                    if len(cells) >= 2:
                        location = cells[1].get_text(strip=True)

                results.append({
                    'title': title, 'location': location, 'url': link,
                    'posted': '', 'department': '', 'id': jid,
                })

            if len(results) == before:
                break    # no new jobs on this page — done
            time.sleep(0.3)

        except Exception as e:
            print(f"    Clinch {clean}: {e}")
            break

    return results


# ── Teamtailor ────────────────────────────────────────────────────────────────

def fetch_teamtailor(base_url: str) -> list:
    clean = base_url.rstrip('/')

    # Try /jobs.json (available on some Teamtailor sites)
    try:
        resp = requests.get(clean + '/jobs.json', headers=BROWSER_HEADERS, timeout=15)
        if resp.status_code == 200:
            jobs = resp.json()
            if not isinstance(jobs, list):
                jobs = jobs.get('data') or jobs.get('jobs') or []
            if jobs:
                return [{
                    'title':      (j.get('attributes') or j).get('title', ''),
                    'location':   (j.get('attributes') or j).get('location', ''),
                    'url':        (j.get('attributes') or j).get('career-page-url', clean),
                    'posted':     ((j.get('attributes') or j).get('created-at') or '')[:10],
                    'department': (j.get('attributes') or j).get('department', ''),
                    'id':         str(j.get('id', '')),
                } for j in jobs]
    except Exception:
        pass

    # HTML fallback — parse /jobs page for job links
    try:
        resp = requests.get(clean + '/jobs', headers=BROWSER_HEADERS, timeout=15)
        if resp.status_code != 200:
            print(f"    Teamtailor {clean}: HTTP {resp.status_code}")
            return []
        soup    = BeautifulSoup(resp.text, 'html.parser')
        results = []
        seen    = set()
        for a in soup.find_all('a', href=re.compile(r'/jobs/[^?#]+')):
            href  = a.get('href', '')
            title = a.get_text(strip=True)
            if not title or not (5 <= len(title) <= 150):
                continue
            url   = href if href.startswith('http') else clean + href
            jid   = url.split('/')[-1]
            if jid in seen or len(jid) < 5:
                continue
            seen.add(jid)
            results.append({'title': title, 'location': '', 'url': url,
                            'posted': '', 'department': '', 'id': jid})
        return results
    except Exception as e:
        print(f"    Teamtailor {clean}: {e}")
        return []


# ── SAP SuccessFactors (Paramount) ────────────────────────────────────────────

def fetch_successfactors(all_jobs_url: str) -> list:
    """
    SAP SuccessFactors career sites (used by Paramount).
    Job links match /job/[url-encoded-title-location]/[numeric-id]/
    (NOT /go/... which is navigation — the old Taleo fetcher had the wrong regex.)
    Paginated via ?start=N at 25 jobs per page.
    """
    seen    = set()
    results = []

    for start in range(0, 1000, 25):
        url = all_jobs_url if start == 0 else f"{all_jobs_url}?start={start}"
        try:
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"    SuccessFactors: HTTP {resp.status_code}")
                break
            soup   = BeautifulSoup(resp.text, 'html.parser')
            before = len(results)

            for a in soup.find_all('a', href=re.compile(r'/job/.+/\d+')):
                href  = a.get('href', '')
                title = a.get_text(strip=True)
                if not title or len(title) < 5:
                    continue

                # Extract numeric job ID
                jid_match = re.search(r'/(\d{6,})/?$', href)
                if not jid_match:
                    continue
                jid = jid_match.group(1)
                if jid in seen:
                    continue
                seen.add(jid)

                full_url = href if href.startswith('http') else 'https://careers.paramount.com' + href

                # Pull location and date from the enclosing list item text
                parent   = a.find_parent('li')
                location = ''
                posted   = ''
                if parent:
                    text = parent.get_text(' ', strip=True)
                    # Locations look like "New York, NY, US, 10036" or "Los Angeles, CA, US, 90038"
                    loc_m = re.search(
                        r'([A-Za-z][A-Za-z\s\-]+,\s*[A-Z]{2},\s*(?:US|CA|GB|AU|PL|MX)[,\s])',
                        text
                    )
                    if loc_m:
                        location = loc_m.group(1).strip().rstrip(',')
                    # Dates look like "May 9, 2026"
                    date_m = re.search(
                        r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2},\s+\d{4}',
                        text
                    )
                    if date_m:
                        posted = date_m.group(0)

                results.append({
                    'title': title, 'location': location, 'url': full_url,
                    'posted': posted, 'department': '', 'id': jid,
                })

            if len(results) == before:
                break          # no new jobs on this page — done
            time.sleep(0.5)

        except Exception as e:
            print(f"    SuccessFactors: {e}")
            break

    return results


# ── Apple (HTML scrape) ───────────────────────────────────────────────────────

def fetch_apple() -> list:
    """
    Apple's careers search page is server-rendered for the initial HTML,
    so we can scrape job links from the search results pages.
    """
    keywords = ['distribution', 'partnerships', 'licensing', 'entertainment', 'content strategy']
    seen     = set()
    results  = []

    for kw in keywords:
        try:
            url  = f"https://jobs.apple.com/en-us/search?search={requests.utils.quote(kw)}&sort=relevance"
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=15)
            if resp.status_code != 200:
                print(f"    Apple '{kw}': HTTP {resp.status_code}")
                continue
            soup = BeautifulSoup(resp.text, 'html.parser')
            for a in soup.find_all('a', href=re.compile(r'/en-us/details/\d+')):
                href  = a.get('href', '')
                m     = re.search(r'/details/(\d+)', href)
                if not m:
                    continue
                jid   = m.group(1)
                if jid in seen:
                    continue
                seen.add(jid)
                title    = a.get_text(strip=True)
                full_url = f"https://jobs.apple.com{href}" if not href.startswith('http') else href
                parent   = a.find_parent()
                loc_el   = parent.find(class_=re.compile(r'location|city', re.I)) if parent else None
                location = loc_el.get_text(strip=True) if loc_el else ''
                results.append({
                    'title': title, 'location': location,
                    'url': full_url, 'posted': '', 'department': '', 'id': jid,
                })
        except Exception as e:
            print(f"    Apple '{kw}': {e}")

    return results


# ── STATE / CSV ───────────────────────────────────────────────────────────────

def load_seen_ids() -> set:
    if not os.path.exists(SEEN_IDS_PATH):
        return set()
    with open(SEEN_IDS_PATH, 'r') as f:
        return set(line.strip() for line in f if line.strip())


def save_seen_ids(ids: set) -> None:
    with open(SEEN_IDS_PATH, 'w') as f:
        f.write('\n'.join(sorted(ids)) + '\n')


def append_jobs_to_csv(rows: list) -> None:
    is_new = not os.path.exists(CSV_PATH) or os.path.getsize(CSV_PATH) == 0
    with open(CSV_PATH, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if is_new:
            writer.writerow(['Date Added', 'Company', 'Role', 'URL', 'Location', 'Date Posted', 'Department'])
        for r in rows:
            j = r['job']
            writer.writerow([
                date.today().isoformat(),
                r['company'],
                j['title'],
                j['url'],
                j['location'] or '—',
                j['posted']   or '—',
                j['department'] or '—',
            ])


# ── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    seen_ids = load_seen_ids()
    new_rows = []
    new_ids  = []

    for co in COMPANIES:
        try:
            print(f"→ {co['name']} [{co['ats']}]")
            jobs    = fetch_jobs(co)
            matched = [j for j in jobs if is_match(j['title']) and is_us_location(j['location'])]
            added   = 0

            for job in matched:
                key = f"{co['name']}::{(job['id'] or job['title'])[:80]}"
                if key in seen_ids:
                    continue
                seen_ids.add(key)
                new_ids.append(key)
                new_rows.append({'company': co['name'], 'job': job})
                added += 1

            print(f"   {len(jobs)} fetched | {len(matched)} matched | {added} new")

        except Exception as e:
            import traceback
            print(f"   ✗ {co['name']}: {e}")
            traceback.print_exc()

    if new_rows:
        append_jobs_to_csv(new_rows)
        save_seen_ids(seen_ids)

    print(f"\nDone — {len(new_rows)} new job(s) added to {CSV_PATH}.")


if __name__ == '__main__':
    main()
