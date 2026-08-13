from __future__ import annotations

import io
import json
import math
import os
import re
from dataclasses import dataclass, asdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import requests
from bs4 import BeautifulSoup

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/151 Safari/537.36"
    ),
    "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
}
TIMEOUT = 20

# Official/public URLs
VCB_FX_URL = "https://www.vietcombank.com.vn/api/exchangerates?date="
VCB_RATE_URL = "https://vietcombank.com.vn/vi-VN/KHCN/Cong-cu-Tien-ich/KHCN---Lai-suat"
VIETINBANK_URL = "https://www.vietinbank.vn/vi/ca-nhan"
NSO_CPI_INDEX = "https://www.nso.gov.vn/cpi-vi/"
BATDONGSAN_HCM_APT = "https://batdongsan.com.vn/ban-can-ho-chung-cu-tp-hcm"
NHATOT_HCM = "https://www.nhatot.com/mua-ban-bat-dong-san-tp-ho-chi-minh"

# Optional packages. Keep the app usable when one provider is temporarily unavailable.
try:
    from vnstock import Retail, Market
except Exception:
    Retail = None
    Market = None

try:
    from google import genai
except Exception:
    genai = None


@dataclass
class DataResult:
    name: str
    status: str  # OK / THIẾU / LỖI
    source: str
    updated_at: Optional[str]
    data: Any
    message: str = ""
    source_urls: Optional[List[str]] = None
    method: str = ""

    @property
    def ok(self) -> bool:
        if self.status != "OK":
            return False
        if self.data is None:
            return False
        if isinstance(self.data, pd.DataFrame):
            return not self.data.empty
        if isinstance(self.data, (list, dict)):
            return len(self.data) > 0
        return True

    def to_jsonable(self) -> Dict[str, Any]:
        payload = asdict(self)
        if isinstance(self.data, pd.DataFrame):
            d = self.data.copy()
            for c in d.columns:
                if pd.api.types.is_datetime64_any_dtype(d[c]):
                    d[c] = d[c].astype(str)
            payload["data"] = d.replace({np.nan: None}).to_dict(orient="records")
        return payload


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ok(name: str, source: str, data: Any, message: str = "", urls: Optional[List[str]] = None, method: str = "") -> DataResult:
    empty = data is None or (isinstance(data, pd.DataFrame) and data.empty) or (isinstance(data, (list, dict)) and len(data) == 0)
    if empty:
        return missing(name, source, message or "Nguồn không trả dữ liệu.", urls, method)
    return DataResult(name, "OK", source, now_iso(), data, message, urls or [], method)


def missing(name: str, source: str, message: str, urls: Optional[List[str]] = None, method: str = "") -> DataResult:
    return DataResult(name, "THIẾU", source, now_iso(), None, message, urls or [], method)


def failed(name: str, source: str, exc: Exception, urls: Optional[List[str]] = None, method: str = "") -> DataResult:
    return DataResult(name, "LỖI", source, now_iso(), None, f"{type(exc).__name__}: {exc}", urls or [], method)


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = [" ".join(str(x) for x in tup if str(x) != "nan").strip() for tup in out.columns]
    else:
        out.columns = [str(c) for c in out.columns]
    return out


def parse_num(x: Any) -> Optional[float]:
    if x is None:
        return None
    if isinstance(x, (int, float, np.number)):
        try:
            if np.isnan(x):
                return None
        except Exception:
            pass
        return float(x)
    s = str(x).strip().replace("\xa0", " ")
    if not s or s.lower() in {"nan", "none", "-", "--"}:
        return None
    s = re.sub(r"[^0-9,\.\-]", "", s)
    if not s:
        return None
    # Vietnamese format: 26.345 or 5,9. If both separators are present, final one is decimal.
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif s.count(".") > 1:
        s = s.replace(".", "")
    elif s.count(",") > 1:
        s = s.replace(",", "")
    elif "," in s:
        tail = s.split(",")[-1]
        s = s.replace(",", "") if len(tail) == 3 else s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def normalize_gold_df(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = flatten_columns(df)
    lower = {c: c.lower().strip() for c in d.columns}
    type_col = next((c for c, n in lower.items() if n in {"type", "product", "loại", "loai"} or "type" in n or "loại" in n), d.columns[0])
    buy_col = next((c for c, n in lower.items() if "buy" in n or "mua" in n), None)
    sell_col = next((c for c, n in lower.items() if "sell" in n or "bán" in n or "ban" in n), None)
    time_col = next((c for c, n in lower.items() if "time" in n or "date" in n or "ngày" in n), None)
    if not buy_col or not sell_col:
        return pd.DataFrame()
    out = pd.DataFrame({
        "type": d[type_col].astype(str),
        "buy": d[buy_col].map(parse_num),
        "sell": d[sell_col].map(parse_num),
    })
    out["time"] = pd.to_datetime(d[time_col], errors="coerce") if time_col else pd.Timestamp.now()
    out = out.dropna(subset=["buy", "sell"], how="all")
    # Some gold sources publish thousand VND. Normalize to VND/lượng conservatively.
    for c in ["buy", "sell"]:
        med = out[c].dropna().median() if out[c].notna().any() else np.nan
        if pd.notna(med) and 50_000 <= med < 1_000_000:
            out[c] = out[c] * 1000
    out["spread"] = out["sell"] - out["buy"]
    out["source"] = source_name
    return out


def normalize_fx_df(df: pd.DataFrame, source_name: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    d = flatten_columns(df)
    lower = {c: c.lower().strip() for c in d.columns}
    ccy_col = next((c for c, n in lower.items() if "currency" in n or "mã" in n or "code" in n), d.columns[0])
    buy_cash = next((c for c, n in lower.items() if "buy_cash" in n or "mua tiền mặt" in n), None)
    buy_transfer = next((c for c, n in lower.items() if "buy_transfer" in n or "chuyển khoản" in n), None)
    sell = next((c for c, n in lower.items() if "sell" in n or "bán" in n or n == "ban"), None)
    generic_buy = next((c for c, n in lower.items() if "buy" in n or "mua" in n), None)
    if sell is None:
        return pd.DataFrame()
    if buy_transfer is None:
        buy_transfer = generic_buy
    out = pd.DataFrame({
        "currency": d[ccy_col].astype(str).str.upper().str.strip(),
        "buy_cash": d[buy_cash].map(parse_num) if buy_cash else np.nan,
        "buy_transfer": d[buy_transfer].map(parse_num) if buy_transfer else np.nan,
        "sell": d[sell].map(parse_num),
    })
    out = out.dropna(subset=["buy_transfer", "sell"], how="all")
    out["spread"] = out["sell"] - out["buy_transfer"]
    out["source"] = source_name
    return out


def _market_ohlcv(kind: str, symbols: List[str], days: int = 730) -> pd.DataFrame:
    if Market is None:
        return pd.DataFrame()
    end = date.today()
    start = end - timedelta(days=days)
    last_error: Optional[Exception] = None
    for sym in symbols:
        try:
            mkt = Market()
            domain = getattr(mkt, kind)
            try:
                df = domain.ohlcv(
                    symbol=sym,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    interval="1D",
                )
            except TypeError:
                # Compatibility fallback for provider versions that do not accept interval.
                df = domain.ohlcv(
                    symbol=sym,
                    start=start.isoformat(),
                    end=end.isoformat(),
                )
            if df is not None and not df.empty:
                out = flatten_columns(df)
                out["symbol_used"] = sym
                return out
        except Exception as e:
            last_error = e
    if last_error:
        raise last_error
    return pd.DataFrame()


def fetch_gold_current() -> DataResult:
    if Retail is None:
        return missing("Vàng SJC", "Vnstock Retail", "Thiếu package vnstock trong requirements.txt.", method="API/library")
    errors: List[str] = []
    for src in ["sjc", "btmc"]:
        try:
            df = Retail().gold(source=src)
            out = normalize_gold_df(df, f"Vnstock Retail/{src.upper()}")
            if not out.empty:
                return ok("Vàng SJC", f"Vnstock Retail/{src.upper()}", out, urls=["https://vnstocks.com/docs/vnstock/du-lieu-thi-truong-hang-hoa-retail"], method="API/library")
        except Exception as e:
            errors.append(f"{src}: {e}")
    return missing("Vàng SJC", "Vnstock Retail", " | ".join(errors) or "Không nhận được dữ liệu.", method="API/library")


def fetch_fx_current() -> DataResult:
    # 1) Vnstock Retail (VCB) — designed for cloud and normalized schema.
    if Retail is not None:
        try:
            df = Retail().exchange_rate()
            out = normalize_fx_df(df, "Vnstock Retail/Vietcombank")
            if not out.empty:
                return ok("Tỷ giá ngân hàng", "Vnstock Retail/Vietcombank", out, urls=["https://vnstocks.com/docs/vnstock/du-lieu-thi-truong-hang-hoa-retail"], method="API/library")
        except Exception:
            pass
    # 2) Official VCB endpoint fallback.
    try:
        r = requests.get(VCB_FX_URL, headers=HTTP_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        js = r.json()
        rows = js.get("Data") or js.get("data") or js
        if isinstance(rows, dict):
            rows = rows.get("ExrateList") or rows.get("rates") or []
        df = pd.DataFrame(rows)
        if not df.empty:
            rename = {}
            for c in df.columns:
                n = c.lower()
                if "currencycode" in n or n == "currency": rename[c] = "currency"
                elif "cash" in n and "buy" in n: rename[c] = "buy_cash"
                elif ("transfer" in n or "transfer" in str(c).lower()) and "buy" in n: rename[c] = "buy_transfer"
                elif "sell" in n: rename[c] = "sell"
            df = df.rename(columns=rename)
            out = normalize_fx_df(df, "Vietcombank official")
            if not out.empty:
                return ok("Tỷ giá ngân hàng", "Vietcombank official", out, urls=["https://vietcombank.com.vn/vi-VN/KHCN/Cong-cu-Tien-ich/Ty-gia"], method="Official HTTP")
    except Exception as e:
        return failed("Tỷ giá ngân hàng", "Vietcombank official", e, method="Official HTTP")
    return missing("Tỷ giá ngân hàng", "Vietcombank official", "Không lấy được bảng tỷ giá.", method="Official HTTP")


def fetch_vnindex_history(days: int = 1100) -> DataResult:
    try:
        df = _market_ohlcv("index", ["VNINDEX"], days)
        return ok("VN-Index", "Vnstock Market", df, urls=["https://vnstocks.com/docs/vnstock/du-lieu-thi-truong-market-data"], method="API/library")
    except Exception as e:
        return failed("VN-Index", "Vnstock Market", e, method="API/library")


def fetch_gold_world_history(days: int = 1100) -> DataResult:
    try:
        df = _market_ohlcv("commodity", ["XAUUSD", "Gold", "GOLD"], days)
        return ok("Vàng thế giới", "Vnstock Market", df, urls=["https://vnstocks.com/docs/vnstock/du-lieu-thi-truong-market-data"], method="API/library")
    except Exception as e:
        return failed("Vàng thế giới", "Vnstock Market", e, method="API/library")


def fetch_usdvnd_history(days: int = 1100) -> DataResult:
    try:
        df = _market_ohlcv("forex", ["USDVND"], days)
        return ok("USD/VND lịch sử", "Vnstock Market", df, urls=["https://vnstocks.com/docs/vnstock/du-lieu-thi-truong-market-data"], method="API/library")
    except Exception as e:
        return failed("USD/VND lịch sử", "Vnstock Market", e, method="API/library")


def _parse_term_months(text: str) -> Optional[int]:
    t = str(text).strip().lower()
    if "không kỳ hạn" in t:
        return 0
    m = re.search(r"(\d+)\s*tháng", t)
    if m:
        return int(m.group(1))
    # day terms are retained as fractional months only if needed; current analysis focuses on monthly terms.
    return None


def fetch_vcb_rates() -> DataResult:
    try:
        r = requests.get(VCB_RATE_URL, headers=HTTP_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        tables = pd.read_html(io.StringIO(r.text))
        rows: List[Dict[str, Any]] = []
        for table in tables:
            d = flatten_columns(table)
            if d.empty:
                continue
            cols = {c: str(c).lower() for c in d.columns}
            term_col = next((c for c, n in cols.items() if "kỳ hạn" in n or "ky han" in n), None)
            vnd_col = next((c for c, n in cols.items() if "vnd" in n), None)
            if not term_col or not vnd_col:
                continue
            for _, row in d.iterrows():
                term = _parse_term_months(row.get(term_col))
                rate = parse_num(row.get(vnd_col))
                if term is not None and rate is not None and 0 <= rate <= 20:
                    rows.append({"bank": "Vietcombank", "term_months": term, "annual_rate_pct": rate, "channel": "Niêm yết cá nhân", "source_url": VCB_RATE_URL})
        if rows:
            out = pd.DataFrame(rows).drop_duplicates(["bank", "term_months", "annual_rate_pct"])
            return ok("Lãi suất Vietcombank", "Vietcombank official", out, urls=[VCB_RATE_URL], method="Official HTML")
        return missing("Lãi suất Vietcombank", "Vietcombank official", "Trang trả HTML nhưng không nhận diện được bảng VND.", [VCB_RATE_URL], "Official HTML")
    except Exception as e:
        return failed("Lãi suất Vietcombank", "Vietcombank official", e, [VCB_RATE_URL], "Official HTML")


def fetch_vietinbank_rates() -> DataResult:
    try:
        r = requests.get(VIETINBANK_URL, headers=HTTP_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        # Look for the compact homepage block: 1 Tháng x, 3 Tháng y, 6 Tháng z, 12 Tháng q.
        rows = []
        for term in [1, 3, 6, 12]:
            pat = rf"{term}\s*Tháng\s*([0-9]+(?:[\.,][0-9]+)?)"
            m = re.search(pat, text, flags=re.I)
            if m:
                rate = parse_num(m.group(1))
                if rate is not None and 0 <= rate <= 20:
                    rows.append({"bank": "VietinBank", "term_months": term, "annual_rate_pct": rate, "channel": "Niêm yết cá nhân", "source_url": VIETINBANK_URL})
        if rows:
            return ok("Lãi suất VietinBank", "VietinBank official", pd.DataFrame(rows), urls=[VIETINBANK_URL], method="Official HTML")
        return missing("Lãi suất VietinBank", "VietinBank official", "Không nhận diện được block lãi suất trên trang cá nhân.", [VIETINBANK_URL], "Official HTML")
    except Exception as e:
        return failed("Lãi suất VietinBank", "VietinBank official", e, [VIETINBANK_URL], "Official HTML")


def fetch_bank_rates_official() -> DataResult:
    parts = [fetch_vcb_rates(), fetch_vietinbank_rates()]
    frames = [p.data for p in parts if p.ok and isinstance(p.data, pd.DataFrame)]
    if frames:
        out = pd.concat(frames, ignore_index=True)
        return ok("Lãi suất tiền gửi", "Official bank websites", out, urls=sum([p.source_urls or [] for p in parts], []), method="Official HTML")
    return missing("Lãi suất tiền gửi", "Official bank websites", "Không ngân hàng nào trả bảng đọc được. " + " | ".join(p.message for p in parts), method="Official HTML")


def _extract_latest_nso_cpi_link(html: str) -> Optional[str]:
    soup = BeautifulSoup(html, "html.parser")
    candidates = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if re.search(r"chỉ số giá tiêu dùng|cpi", text, flags=re.I):
            href = urljoin(NSO_CPI_INDEX, a["href"])
            score = 0
            if re.search(r"2026|2025", text): score += 2
            if "tháng" in text.lower(): score += 2
            candidates.append((score, href, text))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _sentence_percent(text: str, keyword: str, compare_phrase: str) -> Optional[float]:
    # Search a short window around a keyword and comparison phrase.
    norm = re.sub(r"\s+", " ", text)
    pattern = rf"{keyword}.{{0,180}}?{compare_phrase}.{{0,80}}?(?:tăng|giảm)?\s*([0-9]+(?:[\.,][0-9]+)?)\s*%"
    m = re.search(pattern, norm, flags=re.I)
    if not m:
        return None
    val = parse_num(m.group(1))
    if val is None:
        return None
    # Preserve sign for "giảm" if it appears immediately before the percentage.
    snippet = m.group(0).lower()
    return -val if "giảm" in snippet[-60:] else val


def fetch_cpi_official() -> DataResult:
    try:
        r = requests.get(NSO_CPI_INDEX, headers=HTTP_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        link = _extract_latest_nso_cpi_link(r.text)
        if not link:
            return missing("CPI", "NSO", "Không tìm thấy bài CPI mới nhất trên trang danh mục.", [NSO_CPI_INDEX], "Official HTML")
        a = requests.get(link, headers=HTTP_HEADERS, timeout=TIMEOUT)
        a.raise_for_status()
        soup = BeautifulSoup(a.text, "html.parser")
        title = soup.find("h1")
        title_text = title.get_text(" ", strip=True) if title else soup.title.get_text(" ", strip=True) if soup.title else "CPI"
        text = soup.get_text(" ", strip=True)
        row = {
            "title": title_text,
            "mom_pct": _sentence_percent(text, r"(?:CPI|chỉ số giá tiêu dùng)", r"so với tháng trước"),
            "yoy_pct": _sentence_percent(text, r"(?:CPI|chỉ số giá tiêu dùng)", r"so với cùng kỳ"),
            "avg_yoy_pct": _sentence_percent(text, r"CPI bình quân", r"so với cùng kỳ"),
            "core_inflation_yoy_pct": _sentence_percent(text, r"lạm phát cơ bản", r"so với cùng kỳ"),
            "source_url": link,
        }
        if any(row[k] is not None for k in ["mom_pct", "yoy_pct", "avg_yoy_pct", "core_inflation_yoy_pct"]):
            return ok("CPI", "Cơ quan Thống kê Quốc gia (NSO)", pd.DataFrame([row]), urls=[link], method="Official HTML")
        return missing("CPI", "NSO", "Đã mở bài CPI nhưng cấu trúc câu thay đổi nên chưa tách được phần trăm.", [link], "Official HTML")
    except Exception as e:
        return failed("CPI", "NSO", e, [NSO_CPI_INDEX], "Official HTML")


def _extract_batdongsan_ranges(text: str) -> pd.DataFrame:
    norm = re.sub(r"\s+", " ", text)
    rows = []
    # Typical public page lines: Quận 7 | 19.8 - 147,5 tr/m²
    pattern = re.compile(r"([A-ZÀ-ỸĐ][A-Za-zÀ-ỹĐđ0-9\.\-\s]{2,40}?)\s*[|:]?\s*([0-9]+(?:[\.,][0-9]+)?)\s*-\s*([0-9]+(?:[\.,][0-9]+)?)\s*tr(?:iệu)?/m(?:²|2)", re.I)
    seen = set()
    for m in pattern.finditer(norm):
        area = re.sub(r"\s+", " ", m.group(1)).strip(" |-:")
        low, high = parse_num(m.group(2)), parse_num(m.group(3))
        if low is None or high is None or not (1 <= low <= 1000 and 1 <= high <= 1000):
            continue
        key = (area.lower(), round(low, 2), round(high, 2))
        if key in seen:
            continue
        seen.add(key)
        rows.append({"area": area, "min_million_vnd_m2": low, "max_million_vnd_m2": high, "source_url": BATDONGSAN_HCM_APT, "data_type": "Giá chào bán/tham khảo"})
    return pd.DataFrame(rows)


def fetch_bds_public_hcm() -> DataResult:
    errors: List[str] = []
    try:
        r = requests.get(BATDONGSAN_HCM_APT, headers=HTTP_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        df = _extract_batdongsan_ranges(text)
        if not df.empty:
            return ok("BĐS TP.HCM", "Batdongsan.com.vn public market page", df, "Dữ liệu là giá chào bán/tham khảo, không phải giá công chứng.", [BATDONGSAN_HCM_APT], "Public web")
        errors.append("Batdongsan: không tách được bảng giá")
    except Exception as e:
        errors.append(f"Batdongsan: {e}")
    # NhaTot page is kept as a secondary availability signal; it may expose listings but schema changes often.
    try:
        r = requests.get(NHATOT_HCM, headers=HTTP_HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        # Collect a small set of asking price/m2 examples if present.
        vals = [parse_num(x) for x in re.findall(r"([0-9]+(?:[\.,][0-9]+)?)\s*tr/m(?:²|2)", text, flags=re.I)]
        vals = [v for v in vals if v is not None and 1 <= v <= 1000]
        if vals:
            df = pd.DataFrame([{
                "area": "TP.HCM (mẫu tin Nhà Tốt)",
                "min_million_vnd_m2": float(np.percentile(vals, 10)),
                "max_million_vnd_m2": float(np.percentile(vals, 90)),
                "median_million_vnd_m2": float(np.median(vals)),
                "sample_count": len(vals),
                "source_url": NHATOT_HCM,
                "data_type": "Giá chào bán mẫu tin",
            }])
            return ok("BĐS TP.HCM", "Nhà Tốt public listings", df, "Dữ liệu là giá chào bán, không phải giá giao dịch thành công.", [NHATOT_HCM], "Public web")
    except Exception as e:
        errors.append(f"Nhà Tốt: {e}")
    return missing("BĐS TP.HCM", "Public listing sites", " | ".join(errors), [BATDONGSAN_HCM_APT, NHATOT_HCM], "Public web")


def grounded_json(api_key: Optional[str], prompt: str, schema: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not api_key or genai is None:
        return None
    client = genai.Client(api_key=api_key)
    interaction = client.interactions.create(
        model=os.getenv("GEMINI_MODEL", "gemini-3.6-flash"),
        input=prompt,
        tools=[{"type": "google_search"}],
        response_format={"type": "text", "mime_type": "application/json", "schema": schema},
    )
    text = getattr(interaction, "output_text", None)
    if not text:
        return None
    return json.loads(text)


def fetch_bank_rates_grounded(api_key: Optional[str]) -> DataResult:
    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "bank": {"type": "string"},
                        "term_months": {"type": "integer"},
                        "annual_rate_pct": {"type": ["number", "null"]},
                        "channel": {"type": "string"},
                        "updated_date": {"type": ["string", "null"]},
                        "source_url": {"type": ["string", "null"]},
                    },
                    "required": ["bank", "term_months", "annual_rate_pct", "channel", "updated_date", "source_url"],
                },
            }
        },
        "required": ["rows"],
    }
    prompt = """
Search the current official Vietnamese bank websites and return VND retail deposit rates for personal customers for terms 1, 3, 6, 12 and 24 months when publicly available. Prioritize Vietcombank, BIDV, VietinBank, Agribank, MB, Techcombank, ACB, VPBank, Sacombank. Use only the banks' own official domains. Do not use news aggregators. If a rate cannot be verified from an official bank page, return null for that bank/term rather than guessing. source_url must be the official page actually supporting the number. Rates are percent per year.
"""
    try:
        js = grounded_json(api_key, prompt, schema)
        rows = (js or {}).get("rows", [])
        if rows:
            df = pd.DataFrame(rows)
            df["annual_rate_pct"] = pd.to_numeric(df["annual_rate_pct"], errors="coerce")
            df = df.dropna(subset=["annual_rate_pct"])
            # Safety validation against plausible retail deposit rates.
            df = df[(df["annual_rate_pct"] >= 0) & (df["annual_rate_pct"] <= 20)]
            if not df.empty:
                urls = [u for u in df.get("source_url", pd.Series(dtype=str)).dropna().astype(str).unique().tolist() if u.startswith("http")]
                return ok("Lãi suất nhiều ngân hàng", "Gemini Google Search grounding + official bank sites", df, "Dữ liệu chỉ giữ các hàng có URL nguồn chính thức do kết quả grounded trả về.", urls, "Grounded web search")
        return missing("Lãi suất nhiều ngân hàng", "Gemini Search", "Không nhận được lãi suất đã xác minh.", method="Grounded web search")
    except Exception as e:
        return failed("Lãi suất nhiều ngân hàng", "Gemini Search", e, method="Grounded web search")


def fetch_bds_grounded(api_key: Optional[str], city: str = "TP.HCM") -> DataResult:
    schema = {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "area": {"type": "string"},
                        "property_type": {"type": "string"},
                        "min_million_vnd_m2": {"type": ["number", "null"]},
                        "max_million_vnd_m2": {"type": ["number", "null"]},
                        "median_million_vnd_m2": {"type": ["number", "null"]},
                        "updated_date": {"type": ["string", "null"]},
                        "source_name": {"type": "string"},
                        "source_url": {"type": ["string", "null"]},
                        "data_type": {"type": "string"},
                    },
                    "required": ["area", "property_type", "min_million_vnd_m2", "max_million_vnd_m2", "median_million_vnd_m2", "updated_date", "source_name", "source_url", "data_type"],
                },
            }
        },
        "required": ["rows"],
    }
    prompt = f"""
Search the latest publicly available real-estate market data for {city}, Vietnam. Prefer Batdongsan.com.vn, Nha Tot, CBRE Vietnam and Savills Vietnam. Return price-per-square-meter ranges only when a public source explicitly supports them. Distinguish asking/listing price from completed transaction price. Never label listing price as transaction price. Include major districts/areas and apartment/house/land categories when available. Values must be million VND per m2. If there is no defensible number, return null rather than estimate. source_url must point to the supporting public page.
"""
    try:
        js = grounded_json(api_key, prompt, schema)
        rows = (js or {}).get("rows", [])
        if rows:
            df = pd.DataFrame(rows)
            for c in ["min_million_vnd_m2", "max_million_vnd_m2", "median_million_vnd_m2"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            keep = df[["min_million_vnd_m2", "max_million_vnd_m2", "median_million_vnd_m2"]].notna().any(axis=1)
            df = df[keep]
            if not df.empty:
                urls = [u for u in df["source_url"].dropna().astype(str).unique().tolist() if u.startswith("http")]
                return ok(f"BĐS {city}", "Gemini Google Search grounding + public market sources", df, "Giá BĐS được phân loại theo loại dữ liệu; giá chào bán không được coi là giá giao dịch.", urls, "Grounded web search")
        return missing(f"BĐS {city}", "Gemini Search", "Không tìm được số liệu BĐS có nguồn công khai đủ rõ.", method="Grounded web search")
    except Exception as e:
        return failed(f"BĐS {city}", "Gemini Search", e, method="Grounded web search")


def merge_bank_rates(primary: DataResult, secondary: DataResult) -> DataResult:
    frames = []
    urls: List[str] = []
    for r in [primary, secondary]:
        if r.ok and isinstance(r.data, pd.DataFrame):
            frames.append(r.data)
            urls.extend(r.source_urls or [])
    if not frames:
        return missing("Lãi suất tiền gửi", "Auto sources", f"Official: {primary.message}; Grounded: {secondary.message}", method="Multiple automatic connectors")
    df = pd.concat(frames, ignore_index=True, sort=False)
    # Prefer deterministic official connector if duplicates exist.
    if "bank" in df.columns and "term_months" in df.columns:
        df["_priority"] = np.where(df.get("source_url", pd.Series(index=df.index, dtype=str)).astype(str).str.contains("vietcombank|vietinbank", case=False, na=False), 0, 1)
        df = df.sort_values("_priority").drop_duplicates(["bank", "term_months"], keep="first").drop(columns="_priority")
    return ok("Lãi suất tiền gửi", "Official banks + grounded official search", df, urls=list(dict.fromkeys(urls)), method="Multiple automatic connectors")


def merge_bds(primary: DataResult, secondary: DataResult) -> DataResult:
    if secondary.ok:
        return secondary
    return primary


def fetch_all(api_key: Optional[str] = None, city: str = "TP.HCM") -> Dict[str, DataResult]:
    gold = fetch_gold_current()
    fx = fetch_fx_current()
    vni = fetch_vnindex_history()
    gold_world = fetch_gold_world_history()
    usd_hist = fetch_usdvnd_history()
    cpi = fetch_cpi_official()
    bank_official = fetch_bank_rates_official()
    bank_grounded = fetch_bank_rates_grounded(api_key)
    bank = merge_bank_rates(bank_official, bank_grounded)
    bds_public = fetch_bds_public_hcm() if city == "TP.HCM" else missing(f"BĐS {city}", "Public web", "Chưa có parser deterministic cho thành phố này.")
    bds_ground = fetch_bds_grounded(api_key, city)
    bds = merge_bds(bds_public, bds_ground)
    return {
        "gold": gold,
        "fx": fx,
        "vnindex": vni,
        "gold_world": gold_world,
        "usdvnd_history": usd_hist,
        "cpi": cpi,
        "bank_rates": bank,
        "bds": bds,
    }


def best_price_series(df: Optional[pd.DataFrame]) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    d = flatten_columns(df).copy()
    time_col = next((c for c in d.columns if str(c).lower() in {"time", "date", "datetime"} or "time" in str(c).lower() or "date" in str(c).lower()), None)
    price_col = next((c for c in d.columns if str(c).lower() == "close"), None)
    if price_col is None:
        price_col = next((c for c in d.columns if any(k in str(c).lower() for k in ["close", "price", "value"])), None)
    if price_col is None:
        return None
    vals = pd.to_numeric(d[price_col], errors="coerce")
    if time_col:
        idx = pd.to_datetime(d[time_col], errors="coerce")
        s = pd.Series(vals.values, index=idx).dropna().sort_index()
        s = s[~s.index.duplicated(keep="last")]
    else:
        s = pd.Series(vals.dropna().values)
    return s if len(s) >= 2 else None


def period_return(s: Optional[pd.Series], days: int) -> Optional[float]:
    if s is None or len(s) < 2:
        return None
    s = s.dropna().astype(float)
    if isinstance(s.index, pd.DatetimeIndex):
        cutoff = s.index.max() - pd.Timedelta(days=days)
        older = s.loc[s.index <= cutoff]
        base = older.iloc[-1] if len(older) else s.iloc[0]
    else:
        base = s.iloc[max(0, len(s)-1-days)]
    if not np.isfinite(base) or base == 0:
        return None
    return float((s.iloc[-1] / base - 1) * 100)


def annual_vol(s: Optional[pd.Series]) -> Optional[float]:
    if s is None or len(s) < 3:
        return None
    r = s.astype(float).pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 2:
        return None
    return float(r.std(ddof=1) * math.sqrt(252) * 100)


def max_drawdown(s: Optional[pd.Series]) -> Optional[float]:
    if s is None or len(s) < 2:
        return None
    d = s.dropna().astype(float)
    return float(((d / d.cummax()) - 1).min() * 100)


def cagr(s: Optional[pd.Series]) -> Optional[float]:
    if s is None or len(s) < 2:
        return None
    d = s.dropna().astype(float)
    if d.iloc[0] <= 0 or d.iloc[-1] <= 0:
        return None
    years = (d.index[-1] - d.index[0]).days / 365.25 if isinstance(d.index, pd.DatetimeIndex) else len(d) / 252
    if years <= 0:
        return None
    return float(((d.iloc[-1] / d.iloc[0]) ** (1 / years) - 1) * 100)


def series_metrics(s: Optional[pd.Series]) -> Dict[str, Optional[float]]:
    if s is None or len(s) < 2:
        return {}
    return {
        "latest": float(s.dropna().iloc[-1]),
        "ret_7d_pct": period_return(s, 7),
        "ret_30d_pct": period_return(s, 30),
        "ret_90d_pct": period_return(s, 90),
        "ret_365d_pct": period_return(s, 365),
        "cagr_pct": cagr(s),
        "vol_ann_pct": annual_vol(s),
        "max_drawdown_pct": max_drawdown(s),
    }


def result_from_jsonable(payload: Dict[str, Any]) -> DataResult:
    data = payload.get("data")
    if isinstance(data, list):
        data = pd.DataFrame(data)
    return DataResult(
        name=payload.get("name", "Unknown"),
        status=payload.get("status", "THIẾU"),
        source=payload.get("source", "GitHub snapshot"),
        updated_at=payload.get("updated_at"),
        data=data,
        message=payload.get("message", ""),
        source_urls=payload.get("source_urls") or [],
        method=payload.get("method", "GitHub scheduled snapshot"),
    )


def load_snapshot_file(path: str) -> Dict[str, DataResult]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            js = json.load(f)
        raw = js.get("datasets", js)
        return {k: result_from_jsonable(v) for k, v in raw.items() if isinstance(v, dict)}
    except Exception:
        return {}


def apply_snapshot_fallback(live: Dict[str, DataResult], snapshot_path: str) -> Dict[str, DataResult]:
    snap = load_snapshot_file(snapshot_path)
    if not snap:
        return live
    out = dict(live)
    for key, current in live.items():
        old = snap.get(key)
        if (not current.ok) and old is not None and old.ok:
            old.source = f"{old.source} • GitHub scheduled snapshot fallback"
            old.method = "GitHub scheduled snapshot fallback"
            old.message = f"Live connector hiện {current.status}: {current.message}. Đang dùng snapshot tự động gần nhất từ GitHub."
            out[key] = old
    return out
