"""Diagnostic — try different NSE warming sequences to find one that yields data."""
import sys, time, requests
sys.path.insert(0, r"C:\Users\pc\xauusdagent")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

def attempt(label, warming_urls, api_url, headers):
    print(f"\n=== {label} ===")
    s = requests.Session()
    s.headers.update(headers)
    for u in warming_urls:
        try:
            r = s.get(u, timeout=10)
            print(f"  warm {u} -> {r.status_code} ({len(r.content)} bytes)")
        except Exception as e:
            print(f"  warm {u} FAILED: {e}")
        time.sleep(0.6)
    try:
        r = s.get(api_url, timeout=10)
        print(f"  API {api_url}")
        print(f"  status={r.status_code} len={len(r.content)} preview={r.text[:200]!r}")
    except Exception as e:
        print(f"  API FAILED: {e}")

base_headers = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}

# Attempt A — original
attempt("A: home + option-chain page",
        ["https://www.nseindia.com", "https://www.nseindia.com/option-chain"],
        "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
        {**base_headers, "Referer": "https://www.nseindia.com/option-chain"})

# Attempt B — different referer chain
attempt("B: home + market-data + option-chain page",
        ["https://www.nseindia.com",
         "https://www.nseindia.com/market-data/live-equity-market",
         "https://www.nseindia.com/option-chain"],
        "https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY",
        {**base_headers, "Referer": "https://www.nseindia.com/option-chain",
         "X-Requested-With": "XMLHttpRequest"})

# Attempt C — try marketStatus first to validate API path
attempt("C: marketStatus probe",
        ["https://www.nseindia.com", "https://www.nseindia.com/market-data/live-equity-market"],
        "https://www.nseindia.com/api/marketStatus",
        {**base_headers, "Referer": "https://www.nseindia.com/market-data/live-equity-market"})

# Attempt D — try fii/dii again as control
attempt("D: FII/DII control",
        ["https://www.nseindia.com",
         "https://www.nseindia.com/reports/fii-dii"],
        "https://www.nseindia.com/api/fiidiiTradeReact",
        {**base_headers, "Referer": "https://www.nseindia.com/reports/fii-dii"})
