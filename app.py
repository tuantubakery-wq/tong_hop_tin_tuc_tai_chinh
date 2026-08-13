"""
TUAN TU FINANCIAL INTELLIGENCE - R1 REAL DATA ENGINE
=====================================================
Mục tiêu:
- Không sinh dữ liệu tài chính giả để trình bày như dữ liệu thật.
- Phân tích Vàng, USD/VND, VN-Index, lãi suất, CPI, BĐS, hoạt động kinh doanh.
- Ưu tiên nguồn dữ liệu thật; nếu thiếu thì báo THIẾU/LỖI.
- Có thể dùng vnstock_data (nếu người dùng có gói phù hợp) cho Macro Việt Nam.
- Có thể dùng vnstock miễn phí cho VN-Index.
- Có thể nhập CSV/XLSX cho BĐS và lãi suất từng ngân hàng.
- Lưu snapshot dữ liệu vào SQLite để tạo lịch sử riêng theo thời gian.
- Gemini chỉ diễn giải dữ liệu đã xác minh, không tự bịa số còn thiếu.

Chạy:
    streamlit run tuan_tu_financial_intelligence.py

Secrets khuyến nghị (.streamlit/secrets.toml):
    USER_PASSWORD = "..."
    GEMINI_API_KEY = "..."
    GEMINI_MODEL = "gemini-3.5-flash"

Tuỳ chọn:
- vnstock: pip install -U vnstock
- vnstock_data: cài theo hướng dẫn chính thức nếu tài khoản của bạn có quyền sử dụng.
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sqlite3
import statistics
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from bs4 import BeautifulSoup

# -----------------------------
# Optional dependencies
# -----------------------------
try:
    from google import genai
except Exception:
    genai = None

try:
    from vnstock.ui import Market as VnstockMarket
except Exception:
    VnstockMarket = None

try:
    from vnstock_data import Macro as VnstockMacro
except Exception:
    VnstockMacro = None

# ==========================================
# 1. APP CONFIG
# ==========================================
st.set_page_config(
    page_title="Tuấn Tú Financial Intelligence",
    page_icon="🏛️",
    layout="wide",
)

APP_VERSION = "R1.0 REAL-DATA"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "financial_intelligence.db")
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151 Safari/537.36"
    )
}

# Official/public reference pages. Selectors may change; every fetch is validated.
SJC_GOLD_URL = "https://www.sjc.com.vn/gia-vang-online"
SJC_INFO_URL = "https://www.sjc.com.vn/cong-bo-thong-tin"
GSO_DATA_URL = "https://www.gso.gov.vn/du-lieu-va-so-lieu-thong-ke/"
SBV_URL = "https://www.sbv.gov.vn/"


def secret(name: str, default: Any = None) -> Any:
    try:
        return st.secrets.get(name, default)
    except Exception:
        return default


USER_PASSWORD = str(secret("USER_PASSWORD", "123456")).strip()
GEMINI_API_KEY = secret("GEMINI_API_KEY", None)
GEMINI_MODEL = str(secret("GEMINI_MODEL", "gemini-3.5-flash"))

# ==========================================
# 2. DATABASE / SNAPSHOT STORE
# ==========================================

def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                dataset TEXT NOT NULL,
                key TEXT NOT NULL,
                value REAL,
                unit TEXT,
                source TEXT,
                payload_json TEXT,
                UNIQUE(ts, dataset, key, source)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS imported_rows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset TEXT NOT NULL,
                row_hash TEXT NOT NULL,
                row_json TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                UNIQUE(dataset, row_hash)
            )
            """
        )


init_db()


def save_snapshot(dataset: str, key: str, value: Optional[float], unit: str, source: str, payload: Any = None) -> None:
    ts = datetime.now().replace(second=0, microsecond=0).isoformat()
    with db_conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO snapshots(ts, dataset, key, value, unit, source, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (ts, dataset, key, value, unit, source, json.dumps(payload, ensure_ascii=False, default=str) if payload is not None else None),
        )


def load_snapshots(dataset: str, key: Optional[str] = None) -> pd.DataFrame:
    sql = "SELECT ts, dataset, key, value, unit, source, payload_json FROM snapshots WHERE dataset=?"
    params: List[Any] = [dataset]
    if key:
        sql += " AND key=?"
        params.append(key)
    sql += " ORDER BY ts"
    with db_conn() as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    if not df.empty:
        df["ts"] = pd.to_datetime(df["ts"], errors="coerce")
    return df

# ==========================================
# 3. DATA QUALITY
# ==========================================
@dataclass
class DataResult:
    name: str
    status: str  # OK / THIẾU / LỖI
    data: Optional[pd.DataFrame]
    source: str
    updated_at: Optional[datetime]
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == "OK" and self.data is not None and not self.data.empty


def ok_result(name: str, data: pd.DataFrame, source: str, message: str = "") -> DataResult:
    if data is None or data.empty:
        return DataResult(name, "THIẾU", data, source, None, message or "Nguồn không trả về dữ liệu.")
    return DataResult(name, "OK", data, source, datetime.now(), message)


def missing_result(name: str, source: str, message: str) -> DataResult:
    return DataResult(name, "THIẾU", None, source, None, message)


def error_result(name: str, source: str, exc: Exception) -> DataResult:
    return DataResult(name, "LỖI", None, source, None, f"{type(exc).__name__}: {exc}")

# ==========================================
# 4. NORMALIZATION HELPERS
# ==========================================

def normalize_col(c: Any) -> str:
    s = str(c).strip().lower()
    s = re.sub(r"\s+", "_", s)
    s = re.sub(r"[^0-9a-zA-ZÀ-ỹ_]+", "", s)
    return s


def parse_number(x: Any) -> Optional[float]:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    if isinstance(x, (int, float, np.number)):
        return float(x)
    s = str(x).strip()
    if not s or s.lower() in {"nan", "none", "-", "--"}:
        return None
    s = s.replace("\xa0", " ")
    # Keep only digits, separators and minus.
    s = re.sub(r"[^0-9,\.\-]", "", s)
    if not s:
        return None
    # Vietnamese display often uses dot as thousands separator.
    if s.count(".") > 1 and "," not in s:
        s = s.replace(".", "")
    elif s.count(",") > 1 and "." not in s:
        s = s.replace(",", "")
    elif "," in s and "." in s:
        # Assume final separator is decimal; remove the other separator.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        # 24,580.00 vs 24,580; safest if 3 digits after comma => thousands.
        tail = s.split(",")[-1]
        s = s.replace(",", "") if len(tail) == 3 else s.replace(",", ".")
    try:
        return float(s)
    except Exception:
        return None


def pick_numeric_col(df: pd.DataFrame, preferred_tokens: Iterable[str]) -> Optional[str]:
    if df is None or df.empty:
        return None
    cols = list(df.columns)
    normalized = {c: normalize_col(c) for c in cols}
    for token in preferred_tokens:
        token_n = normalize_col(token)
        for c, n in normalized.items():
            if token_n in n:
                vals = pd.to_numeric(df[c], errors="coerce")
                if vals.notna().sum() >= max(2, len(df) // 4):
                    return c
    for c in cols:
        vals = pd.to_numeric(df[c], errors="coerce")
        if vals.notna().sum() >= max(2, len(df) // 2):
            return c
    return None


def pick_date_col(df: pd.DataFrame) -> Optional[str]:
    if df is None or df.empty:
        return None
    for c in df.columns:
        n = normalize_col(c)
        if any(k in n for k in ["date", "time", "ngay", "timestamp"]):
            parsed = pd.to_datetime(df[c], errors="coerce", dayfirst=True)
            if parsed.notna().sum() >= max(2, len(df) // 4):
                return c
    return None


def flatten_columns(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["_".join([str(x) for x in tup if str(x) != "nan"]).strip("_") for tup in df.columns]
    else:
        df.columns = [str(c) for c in df.columns]
    return df

# ==========================================
# 5. REAL DATA CONNECTORS
# ==========================================
@st.cache_data(ttl=900, show_spinner=False)
def fetch_sjc_html_tables() -> Tuple[List[pd.DataFrame], str]:
    """Fetch official SJC pages. Returns validated HTML tables if the site exposes them server-side."""
    errors = []
    tables: List[pd.DataFrame] = []
    for url in [SJC_GOLD_URL, SJC_INFO_URL]:
        try:
            r = requests.get(url, headers=HTTP_HEADERS, timeout=12)
            r.raise_for_status()
            try:
                found = pd.read_html(io.StringIO(r.text))
                tables.extend([flatten_columns(x) for x in found if not x.empty])
            except Exception as e:
                errors.append(f"read_html {url}: {e}")
        except Exception as e:
            errors.append(f"GET {url}: {e}")
    return tables, " | ".join(errors)


def find_gold_table(tables: List[pd.DataFrame]) -> Optional[pd.DataFrame]:
    candidates = []
    for df in tables:
        text = " ".join(map(str, df.columns)).lower() + " " + " ".join(df.astype(str).head(10).fillna("").values.flatten()).lower()
        if "vàng" in text or "vang" in text or "gold" in text:
            score = sum(k in text for k in ["mua", "bán", "ban", "loại", "sjc"])
            candidates.append((score, df))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]


def find_fx_table(tables: List[pd.DataFrame]) -> Optional[pd.DataFrame]:
    candidates = []
    for df in tables:
        text = " ".join(map(str, df.columns)).lower() + " " + " ".join(df.astype(str).head(15).fillna("").values.flatten()).lower()
        if "usd" in text and ("mua" in text or "bán" in text or "ban" in text):
            score = sum(k in text for k in ["usd", "eur", "jpy", "aud", "tỷ giá", "ty gia"])
            candidates.append((score, df))
    if not candidates:
        return None
    return sorted(candidates, key=lambda x: x[0], reverse=True)[0][1]


def normalize_sjc_gold(df: pd.DataFrame) -> pd.DataFrame:
    out = flatten_columns(df).copy()
    out.columns = [normalize_col(c) for c in out.columns]
    # Locate likely columns.
    name_col = next((c for c in out.columns if any(k in c for k in ["loai", "type", "vang"])), out.columns[0])
    buy_col = next((c for c in out.columns if "mua" in c or "buy" in c), None)
    sell_col = next((c for c in out.columns if "ban" in c or "sell" in c), None)
    if not buy_col or not sell_col:
        raise ValueError("Không nhận diện được cột mua/bán trong bảng SJC.")
    result = pd.DataFrame({
        "product": out[name_col].astype(str).str.strip(),
        "buy": out[buy_col].map(parse_number),
        "sell": out[sell_col].map(parse_number),
    }).dropna(subset=["buy", "sell"], how="all")
    # SJC may display in thousand VND or VND. Detect scale conservatively.
    for c in ["buy", "sell"]:
        med = result[c].dropna().median() if result[c].notna().any() else np.nan
        if pd.notna(med) and 1_000 <= med < 1_000_000:
            result[c] = result[c] * 1_000
    result["spread"] = result["sell"] - result["buy"]
    result["source"] = "SJC official"
    result["updated_at"] = datetime.now()
    return result


def normalize_sjc_fx(df: pd.DataFrame) -> pd.DataFrame:
    out = flatten_columns(df).copy()
    out.columns = [normalize_col(c) for c in out.columns]
    ccy_col = next((c for c in out.columns if any(k in c for k in ["loai", "currency", "ngoai_te"])), out.columns[0])
    buy_col = next((c for c in out.columns if "mua" in c or "buy" in c), None)
    sell_col = next((c for c in out.columns if "ban" in c or "sell" in c), None)
    if not buy_col or not sell_col:
        raise ValueError("Không nhận diện được cột mua/bán tỷ giá.")
    result = pd.DataFrame({
        "currency": out[ccy_col].astype(str).str.upper().str.strip(),
        "buy": out[buy_col].map(parse_number),
        "sell": out[sell_col].map(parse_number),
    }).dropna(subset=["buy", "sell"], how="all")
    result["spread"] = result["sell"] - result["buy"]
    result["source"] = "SJC official"
    result["updated_at"] = datetime.now()
    return result


@st.cache_data(ttl=900, show_spinner=False)
def fetch_gold_current() -> DataResult:
    # 1) Prefer Vnstock Data if installed because it can expose time series and normalized schemas.
    if VnstockMacro is not None:
        try:
            mac = VnstockMacro()
            df = mac.commodity().gold(market="VN")
            if df is not None and not df.empty:
                return ok_result("Vàng VN", flatten_columns(df), "vnstock_data Macro / nguồn dữ liệu của thư viện")
        except Exception:
            pass
    # 2) Official SJC HTML fallback.
    try:
        tables, errors = fetch_sjc_html_tables()
        t = find_gold_table(tables)
        if t is not None:
            return ok_result("Vàng SJC", normalize_sjc_gold(t), "SJC official")
        return missing_result("Vàng SJC", "SJC official", "Trang SJC không trả bảng giá ở HTML server-side. " + errors)
    except Exception as e:
        return error_result("Vàng SJC", "SJC official", e)


@st.cache_data(ttl=900, show_spinner=False)
def fetch_fx_data() -> DataResult:
    if VnstockMacro is not None:
        try:
            mac = VnstockMacro()
            df = mac.currency().exchange_rate(period="day", length=365)
            if df is not None and not df.empty:
                return ok_result("Tỷ giá", flatten_columns(df), "vnstock_data Macro / nguồn dữ liệu của thư viện")
        except Exception:
            pass
    try:
        tables, errors = fetch_sjc_html_tables()
        t = find_fx_table(tables)
        if t is not None:
            return ok_result("Tỷ giá", normalize_sjc_fx(t), "SJC official")
        return missing_result("Tỷ giá", "SJC official", "Không đọc được bảng tỷ giá từ HTML SJC. " + errors)
    except Exception as e:
        return error_result("Tỷ giá", "SJC official", e)


@st.cache_data(ttl=1800, show_spinner=False)
def fetch_macro_data() -> Dict[str, DataResult]:
    names = ["CPI", "Lãi suất thị trường"]
    if VnstockMacro is None:
        return {
            "cpi": missing_result("CPI", "GSO / vnstock_data", "Chưa có connector tự động. Có thể cài/cấu hình vnstock_data hoặc nhập file."),
            "interest": missing_result("Lãi suất thị trường", "NHNN / vnstock_data", "Chưa có connector tự động cho lịch sử lãi suất trong môi trường này."),
        }
    try:
        mac = VnstockMacro()
        cpi = flatten_columns(mac.economy().cpi(period="month", length=60))
        ir = flatten_columns(mac.currency().interest_rate(period="day", format="long", length=365))
        return {
            "cpi": ok_result("CPI", cpi, "vnstock_data Macro"),
            "interest": ok_result("Lãi suất thị trường", ir, "vnstock_data Macro"),
        }
    except Exception as e:
        return {
            "cpi": error_result("CPI", "vnstock_data Macro", e),
            "interest": error_result("Lãi suất thị trường", "vnstock_data Macro", e),
        }


@st.cache_data(ttl=900, show_spinner=False)
def fetch_vnindex(days: int = 1100) -> DataResult:
    if VnstockMarket is None:
        return missing_result("VN-Index", "vnstock", "Chưa cài thư viện vnstock (`pip install -U vnstock`).")
    try:
        end = date.today()
        start = end - timedelta(days=days)
        mkt = VnstockMarket()
        df = mkt.index("VNINDEX").ohlcv(start=start.isoformat(), end=end.isoformat(), interval="1D")
        df = flatten_columns(df)
        if df is None or df.empty:
            return missing_result("VN-Index", "vnstock", "Nguồn VN-Index không trả dữ liệu.")
        return ok_result("VN-Index", df, "vnstock")
    except Exception as e:
        return error_result("VN-Index", "vnstock", e)

# ==========================================
# 6. USER-IMPORTED DATA
# ==========================================

def read_upload(uploaded) -> pd.DataFrame:
    name = uploaded.name.lower()
    raw = uploaded.getvalue()
    if name.endswith(".csv"):
        # Auto-try UTF-8 then common Vietnamese legacy encoding.
        for enc in ["utf-8-sig", "utf-8", "cp1258", "latin1"]:
            try:
                return pd.read_csv(io.BytesIO(raw), encoding=enc)
            except Exception:
                continue
        raise ValueError("Không đọc được CSV.")
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(raw))
    raise ValueError("Chỉ hỗ trợ CSV/XLS/XLSX.")


def standardize_bank_rates(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d.columns = [normalize_col(c) for c in d.columns]
    aliases = {
        "bank": ["bank", "ngan_hang", "nganhang", "ten_ngan_hang"],
        "term_months": ["term_months", "ky_han_thang", "kyhan", "thang"],
        "annual_rate_pct": ["annual_rate_pct", "lai_suat", "laisuat", "rate", "interest_rate"],
        "channel": ["channel", "kenh", "hinh_thuc"],
        "updated_at": ["updated_at", "ngay_cap_nhat", "ngay", "date"],
        "source": ["source", "nguon"],
    }
    out = pd.DataFrame()
    for target, candidates in aliases.items():
        col = next((c for c in candidates if c in d.columns), None)
        out[target] = d[col] if col else np.nan
    out["term_months"] = pd.to_numeric(out["term_months"], errors="coerce")
    out["annual_rate_pct"] = out["annual_rate_pct"].map(parse_number)
    out["updated_at"] = pd.to_datetime(out["updated_at"], errors="coerce", dayfirst=True)
    out["bank"] = out["bank"].astype(str).str.strip()
    out["channel"] = out["channel"].fillna("Không rõ").astype(str)
    out["source"] = out["source"].fillna("File người dùng").astype(str)
    return out.dropna(subset=["term_months", "annual_rate_pct"])


def standardize_real_estate(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    d.columns = [normalize_col(c) for c in d.columns]
    aliases = {
        "date": ["date", "ngay", "ngay_ghi_nhan", "updated_at"],
        "location": ["location", "khu_vuc", "quan", "phuong", "du_an"],
        "property_type": ["property_type", "loai_bds", "loai", "type"],
        "price_vnd": ["price_vnd", "gia_ban", "gia", "price"],
        "area_m2": ["area_m2", "dien_tich", "dientich", "m2"],
        "monthly_rent_vnd": ["monthly_rent_vnd", "gia_thue_thang", "gia_thue", "rent"],
        "source": ["source", "nguon"],
    }
    out = pd.DataFrame()
    for target, candidates in aliases.items():
        col = next((c for c in candidates if c in d.columns), None)
        out[target] = d[col] if col else np.nan
    out["date"] = pd.to_datetime(out["date"], errors="coerce", dayfirst=True)
    out["price_vnd"] = out["price_vnd"].map(parse_number)
    out["area_m2"] = out["area_m2"].map(parse_number)
    out["monthly_rent_vnd"] = out["monthly_rent_vnd"].map(parse_number)
    out["location"] = out["location"].fillna("Không rõ").astype(str).str.strip()
    out["property_type"] = out["property_type"].fillna("Không rõ").astype(str).str.strip()
    out["source"] = out["source"].fillna("File người dùng").astype(str)
    out = out.dropna(subset=["price_vnd"])
    out["price_per_m2"] = np.where(out["area_m2"] > 0, out["price_vnd"] / out["area_m2"], np.nan)
    out["gross_rental_yield_pct"] = np.where(
        (out["price_vnd"] > 0) & (out["monthly_rent_vnd"] > 0),
        out["monthly_rent_vnd"] * 12 / out["price_vnd"] * 100,
        np.nan,
    )
    return out

# ==========================================
# 7. ANALYTICS ENGINE
# ==========================================

def infer_series(df: pd.DataFrame, preferred_price_tokens: Iterable[str] = ("close", "price", "sell", "value")) -> Optional[pd.Series]:
    if df is None or df.empty:
        return None
    d = flatten_columns(df).copy()
    date_col = pick_date_col(d)
    price_col = pick_numeric_col(d, preferred_price_tokens)
    if price_col is None:
        return None
    vals = pd.to_numeric(d[price_col], errors="coerce")
    if date_col:
        idx = pd.to_datetime(d[date_col], errors="coerce", dayfirst=True)
        s = pd.Series(vals.values, index=idx).dropna().sort_index()
        s = s[~s.index.duplicated(keep="last")]
    else:
        s = pd.Series(vals.dropna().values)
    return s if len(s) >= 2 else None


def return_over_period(s: pd.Series, days: int) -> Optional[float]:
    if s is None or len(s) < 2:
        return None
    s = s.dropna()
    if isinstance(s.index, pd.DatetimeIndex):
        cutoff = s.index.max() - pd.Timedelta(days=days)
        older = s.loc[s.index <= cutoff]
        base = older.iloc[-1] if len(older) else s.iloc[0]
    else:
        n = min(days, len(s) - 1)
        base = s.iloc[-1 - n]
    if base == 0 or pd.isna(base):
        return None
    return (s.iloc[-1] / base - 1) * 100


def max_drawdown(s: pd.Series) -> Optional[float]:
    if s is None or len(s) < 2:
        return None
    s = s.dropna().astype(float)
    peak = s.cummax()
    dd = s / peak - 1
    return float(dd.min() * 100)


def annualized_volatility(s: pd.Series) -> Optional[float]:
    if s is None or len(s) < 3:
        return None
    r = s.astype(float).pct_change().replace([np.inf, -np.inf], np.nan).dropna()
    if len(r) < 2:
        return None
    # Approximation for daily-ish data.
    return float(r.std(ddof=1) * math.sqrt(252) * 100)


def cagr(s: pd.Series) -> Optional[float]:
    if s is None or len(s) < 2:
        return None
    s = s.dropna().astype(float)
    if s.iloc[0] <= 0 or s.iloc[-1] <= 0:
        return None
    if isinstance(s.index, pd.DatetimeIndex) and len(s.index) >= 2:
        years = max((s.index[-1] - s.index[0]).days / 365.25, 1 / 365.25)
    else:
        years = len(s) / 252
    return float(((s.iloc[-1] / s.iloc[0]) ** (1 / years) - 1) * 100)


def time_series_metrics(s: Optional[pd.Series]) -> Dict[str, Optional[float]]:
    if s is None or len(s) < 2:
        return {}
    return {
        "latest": float(s.dropna().iloc[-1]),
        "ret_7d_pct": return_over_period(s, 7),
        "ret_30d_pct": return_over_period(s, 30),
        "ret_90d_pct": return_over_period(s, 90),
        "ret_365d_pct": return_over_period(s, 365),
        "cagr_pct": cagr(s),
        "vol_ann_pct": annualized_volatility(s),
        "max_drawdown_pct": max_drawdown(s),
    }


def bank_future_value(principal: float, annual_rate_pct: float, months: int, compound_monthly: bool = False) -> Tuple[float, float]:
    r = annual_rate_pct / 100.0
    if compound_monthly:
        fv = principal * (1 + r / 12) ** months
    else:
        fv = principal * (1 + r * months / 12)
    return fv, fv - principal


def real_rate(nominal_pct: float, inflation_pct: float) -> float:
    return ((1 + nominal_pct / 100) / (1 + inflation_pct / 100) - 1) * 100


def bakery_metrics(
    setup_cost: float,
    avg_price: float,
    cogs_pct: float,
    monthly_rent: float,
    monthly_labor: float,
    monthly_utilities: float,
    monthly_marketing: float,
    daily_volume: float,
) -> Dict[str, float]:
    cogs_per_unit = avg_price * cogs_pct / 100.0
    contribution = avg_price - cogs_per_unit
    opex = monthly_rent + monthly_labor + monthly_utilities + monthly_marketing
    breakeven_month = opex / contribution if contribution > 0 else np.inf
    revenue = daily_volume * 30 * avg_price
    gross = revenue - daily_volume * 30 * cogs_per_unit
    net = gross - opex
    payback = setup_cost / net if net > 0 else np.inf
    roic_annual = (net * 12 / setup_cost * 100) if setup_cost > 0 else np.nan
    return {
        "monthly_revenue": revenue,
        "monthly_net_profit": net,
        "breakeven_units_day": breakeven_month / 30 if np.isfinite(breakeven_month) else np.inf,
        "payback_months": payback,
        "roic_annual_pct": roic_annual,
    }


def monte_carlo_portfolio(
    initial_capital: float,
    years: int,
    simulations: int,
    assets: pd.DataFrame,
    seed: int = 42,
) -> pd.DataFrame:
    """
    assets columns: asset, weight, expected_return_pct, volatility_pct
    Independent-normal baseline. This is a scenario engine, NOT a forecast guarantee.
    """
    a = assets.copy()
    a = a[(a["weight"] > 0) & a["expected_return_pct"].notna() & a["volatility_pct"].notna()]
    if a.empty:
        return pd.DataFrame()
    weights = a["weight"].astype(float).values
    weights = weights / weights.sum()
    mu = np.sum(weights * a["expected_return_pct"].astype(float).values / 100)
    vol = math.sqrt(np.sum((weights * a["volatility_pct"].astype(float).values / 100) ** 2))
    rng = np.random.default_rng(seed)
    monthly_mu = (1 + mu) ** (1 / 12) - 1 if mu > -1 else -0.99
    monthly_vol = vol / math.sqrt(12)
    values = np.full(simulations, initial_capital, dtype=float)
    min_values = values.copy()
    for _ in range(years * 12):
        shocks = rng.normal(monthly_mu, monthly_vol, simulations)
        shocks = np.maximum(shocks, -0.95)
        values *= (1 + shocks)
        min_values = np.minimum(min_values, values)
    return pd.DataFrame({"final_value": values, "min_value": min_values})

# ==========================================
# 8. AI REPORT ENGINE
# ==========================================

def safe_df_summary(df: Optional[pd.DataFrame], rows: int = 12) -> Any:
    if df is None or df.empty:
        return None
    d = flatten_columns(df).tail(rows).copy()
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].astype(str)
    return d.replace({np.nan: None}).to_dict(orient="records")


def generate_ai_report(context: Dict[str, Any]) -> str:
    if genai is None:
        raise RuntimeError("Chưa cài `google-genai`.")
    if not GEMINI_API_KEY:
        raise RuntimeError("Chưa cấu hình GEMINI_API_KEY trong Streamlit Secrets.")
    client = genai.Client(api_key=GEMINI_API_KEY)
    prompt = f"""
Bạn là trợ lý phân tích tài chính tại Việt Nam. Đây là HỆ THỐNG HỖ TRỢ QUYẾT ĐỊNH, không phải lời hứa lợi nhuận.

QUY TẮC BẮT BUỘC:
1. CHỈ dùng các số liệu trong JSON bên dưới.
2. Nếu một nhóm dữ liệu có status khác OK hoặc giá trị null: nói rõ là THIẾU DỮ LIỆU; tuyệt đối không tự điền số.
3. Phân biệt DỮ LIỆU QUAN SÁT, CHỈ SỐ TÍNH TOÁN và GIẢ ĐỊNH/MÔ PHỎNG.
4. Không khẳng định chắc chắn giá vàng, USD, chứng khoán hay BĐS sẽ tăng/giảm.
5. Khi so sánh tài sản phải nhắc đến thanh khoản, spread/chi phí giao dịch, rủi ro và thời gian nắm giữ.
6. Nếu đề xuất phân bổ vốn, đưa ra 3 kịch bản: thận trọng, cân bằng, tăng trưởng; tổng tỷ trọng mỗi kịch bản = 100%.
7. Không dùng kiến thức ngoài JSON để tạo ra con số mới.

DỮ LIỆU ĐÃ KIỂM SOÁT:
{json.dumps(context, ensure_ascii=False, default=str, indent=2)}

HÃY TRẢ VỀ BÁO CÁO:
A. Tình trạng dữ liệu & độ tin cậy
B. Vàng
C. USD/VND
D. Lãi suất & lợi suất thực sau lạm phát
E. VN-Index / thị trường chứng khoán
F. Bất động sản (chỉ nếu có dữ liệu)
G. Hiệu quả vốn kinh doanh (nếu có)
H. So sánh cơ hội sử dụng vốn
I. 3 kịch bản phân bổ vốn, ghi rõ đây chỉ là khung tham khảo
J. 5 rủi ro lớn nhất và dữ liệu nào cần bổ sung trước khi ra quyết định lớn
"""
    resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return resp.text

# ==========================================
# 9. AUTH
# ==========================================
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if not st.session_state.logged_in:
    st.title("🏛️ TUẤN TÚ FINANCIAL INTELLIGENCE")
    st.caption(f"{APP_VERSION} • Dữ liệu thật, không dùng số ngẫu nhiên để giả lập thị trường")
    with st.form("login"):
        pwd = st.text_input("Mật khẩu", type="password")
        submit = st.form_submit_button("Đăng nhập", use_container_width=True)
        if submit:
            if str(pwd).strip() == USER_PASSWORD:
                st.session_state.logged_in = True
                st.rerun()
            st.error("Mật khẩu không đúng.")
    st.stop()

# ==========================================
# 10. SIDEBAR / GLOBAL INPUTS
# ==========================================
st.sidebar.title("🏛️ FINANCIAL INTELLIGENCE")
st.sidebar.caption(APP_VERSION)
if st.sidebar.button("🚪 Đăng xuất", use_container_width=True):
    st.session_state.logged_in = False
    st.rerun()

page = st.sidebar.radio(
    "Khu vực",
    [
        "📡 Trung tâm dữ liệu",
        "🟡 Vàng & USD",
        "🏦 Lãi suất & lạm phát",
        "📈 Chứng khoán",
        "🏠 Bất động sản",
        "🍰 Hiệu quả kinh doanh",
        "🧮 Phân bổ vốn & Monte Carlo",
        "🤖 Báo cáo AI tổng hợp",
    ],
)

# Session imported datasets
if "bank_rates" not in st.session_state:
    st.session_state.bank_rates = pd.DataFrame()
if "real_estate" not in st.session_state:
    st.session_state.real_estate = pd.DataFrame()
if "bakery" not in st.session_state:
    st.session_state.bakery = None

# Load market datasets only once per rerun; cache controls requests.
gold_res = fetch_gold_current()
fx_res = fetch_fx_data()
macro_res = fetch_macro_data()
vni_res = fetch_vnindex()

all_results = {
    "gold": gold_res,
    "fx": fx_res,
    "cpi": macro_res["cpi"],
    "market_interest": macro_res["interest"],
    "vnindex": vni_res,
}

# ==========================================
# 11. UI HELPERS
# ==========================================

def status_badge(res: DataResult) -> str:
    return {"OK": "🟢 OK", "THIẾU": "🟡 THIẾU", "LỖI": "🔴 LỖI"}.get(res.status, res.status)


def show_data_result(res: DataResult, max_rows: int = 50) -> None:
    st.write(f"**{res.name}:** {status_badge(res)}")
    st.caption(f"Nguồn: {res.source}" + (f" • Cập nhật kiểm tra: {res.updated_at:%d/%m/%Y %H:%M}" if res.updated_at else ""))
    if res.message:
        st.caption(res.message)
    if res.ok:
        st.dataframe(res.data.tail(max_rows), use_container_width=True)


def best_series(res: DataResult, tokens: Iterable[str]) -> Optional[pd.Series]:
    return infer_series(res.data, tokens) if res.ok else None

# ==========================================
# 12. PAGES
# ==========================================
if page == "📡 Trung tâm dữ liệu":
    st.title("📡 Trung tâm dữ liệu & kiểm soát chất lượng")
    st.info("Nguyên tắc: mất nguồn = báo thiếu/lỗi. Hệ thống không tự tạo giá vàng, USD hay lãi suất giả để lấp chỗ trống.")
    cols = st.columns(len(all_results))
    for col, (k, res) in zip(cols, all_results.items()):
        col.metric(res.name, status_badge(res))
    st.divider()
    for res in all_results.values():
        with st.expander(f"{status_badge(res)} — {res.name}"):
            show_data_result(res)

    st.subheader("Nhập dữ liệu lãi suất từng ngân hàng")
    st.caption("Cột gợi ý: bank, term_months, annual_rate_pct, channel, updated_at, source")
    bank_file = st.file_uploader("CSV/XLSX lãi suất ngân hàng", type=["csv", "xlsx", "xls"], key="bank_upload")
    if bank_file:
        try:
            st.session_state.bank_rates = standardize_bank_rates(read_upload(bank_file))
            st.success(f"Đã đọc {len(st.session_state.bank_rates):,} dòng lãi suất.")
            st.dataframe(st.session_state.bank_rates, use_container_width=True)
        except Exception as e:
            st.error(str(e))

    st.subheader("Nhập dữ liệu BĐS")
    st.caption("Cột gợi ý: date, location, property_type, price_vnd, area_m2, monthly_rent_vnd, source")
    re_file = st.file_uploader("CSV/XLSX bất động sản", type=["csv", "xlsx", "xls"], key="re_upload")
    if re_file:
        try:
            st.session_state.real_estate = standardize_real_estate(read_upload(re_file))
            st.success(f"Đã đọc {len(st.session_state.real_estate):,} dòng BĐS.")
            st.dataframe(st.session_state.real_estate.head(100), use_container_width=True)
        except Exception as e:
            st.error(str(e))

elif page == "🟡 Vàng & USD":
    st.title("🟡 Vàng & USD/VND")
    c1, c2 = st.columns(2)
    with c1:
        show_data_result(gold_res, 100)
        if gold_res.ok:
            gold_s = best_series(gold_res, ["sell", "close", "price", "gia_ban", "value"])
            m = time_series_metrics(gold_s)
            if m:
                st.write("**Chỉ số chuỗi thời gian**")
                st.json(m)
                if isinstance(gold_s.index, pd.DatetimeIndex):
                    st.plotly_chart(px.line(x=gold_s.index, y=gold_s.values, labels={"x": "Ngày", "y": "Giá"}), use_container_width=True)
            # Save latest if normalized current table.
            if {"product", "buy", "sell"}.issubset(gold_res.data.columns):
                for _, row in gold_res.data.iterrows():
                    save_snapshot("gold", str(row["product"]), parse_number(row["sell"]), "VND/lượng", gold_res.source, row.to_dict())
    with c2:
        show_data_result(fx_res, 100)
        if fx_res.ok:
            fx_s = best_series(fx_res, ["usd", "sell", "close", "exchange", "rate", "value"])
            m = time_series_metrics(fx_s)
            if m:
                st.write("**Chỉ số chuỗi thời gian**")
                st.json(m)
                if isinstance(fx_s.index, pd.DatetimeIndex):
                    st.plotly_chart(px.line(x=fx_s.index, y=fx_s.values, labels={"x": "Ngày", "y": "USD/VND"}), use_container_width=True)
            if {"currency", "buy", "sell"}.issubset(fx_res.data.columns):
                usd = fx_res.data[fx_res.data["currency"].astype(str).str.contains("USD", case=False, na=False)]
                for _, row in usd.iterrows():
                    save_snapshot("fx", "USDVND", parse_number(row["sell"]), "VND/USD", fx_res.source, row.to_dict())

    st.subheader("Lịch sử snapshot do hệ thống tự tích lũy")
    sg = load_snapshots("gold")
    sf = load_snapshots("fx")
    c1, c2 = st.columns(2)
    if not sg.empty:
        c1.plotly_chart(px.line(sg, x="ts", y="value", color="key", title="Snapshot vàng"), use_container_width=True)
    else:
        c1.info("Chưa đủ snapshot vàng. Mỗi lần app lấy được giá hợp lệ, lịch sử sẽ được tích lũy.")
    if not sf.empty:
        c2.plotly_chart(px.line(sf, x="ts", y="value", color="key", title="Snapshot USD/VND"), use_container_width=True)
    else:
        c2.info("Chưa đủ snapshot USD/VND.")

elif page == "🏦 Lãi suất & lạm phát":
    st.title("🏦 Lãi suất, tiết kiệm & lợi suất thực")
    show_data_result(macro_res["cpi"], 40)
    show_data_result(macro_res["interest"], 50)

    bank_df = st.session_state.bank_rates
    if bank_df.empty:
        st.warning("Chưa có bảng lãi suất từng ngân hàng. Hãy nhập CSV/XLSX tại Trung tâm dữ liệu.")
    else:
        st.subheader("So sánh lãi suất từng ngân hàng")
        term = st.selectbox("Kỳ hạn (tháng)", sorted(bank_df["term_months"].dropna().unique().tolist()))
        view = bank_df[bank_df["term_months"] == term].sort_values("annual_rate_pct", ascending=False)
        st.dataframe(view, use_container_width=True)
        st.plotly_chart(px.bar(view, x="bank", y="annual_rate_pct", color="channel", text="annual_rate_pct"), use_container_width=True)

        st.subheader("Máy tính tiền gửi")
        c1, c2, c3 = st.columns(3)
        principal = c1.number_input("Số tiền gửi (VNĐ)", min_value=0.0, value=2_000_000_000.0, step=100_000_000.0)
        selected_bank = c2.selectbox("Ngân hàng", view["bank"].astype(str).unique())
        rate_row = view[view["bank"].astype(str) == selected_bank].iloc[0]
        rate = c3.number_input("Lãi suất %/năm", value=float(rate_row["annual_rate_pct"]), step=0.1)
        fv, interest = bank_future_value(principal, rate, int(term), False)
        st.metric("Tiền lãi danh nghĩa", f"{interest:,.0f} ₫")
        st.metric("Giá trị cuối kỳ", f"{fv:,.0f} ₫")

        inflation = st.number_input("Lạm phát giả định / CPI % để tính lợi suất thực", value=4.0, step=0.1)
        st.metric("Lợi suất thực xấp xỉ", f"{real_rate(rate, inflation):.2f}%/năm")

elif page == "📈 Chứng khoán":
    st.title("📈 Thị trường chứng khoán Việt Nam")
    show_data_result(vni_res, 100)
    if vni_res.ok:
        s = best_series(vni_res, ["close", "price", "index", "value"])
        metrics = time_series_metrics(s)
        if metrics:
            c = st.columns(5)
            c[0].metric("Mức gần nhất", f"{metrics.get('latest', np.nan):,.2f}")
            c[1].metric("7 ngày", f"{metrics.get('ret_7d_pct', np.nan):.2f}%")
            c[2].metric("30 ngày", f"{metrics.get('ret_30d_pct', np.nan):.2f}%")
            c[3].metric("Vol năm hóa", f"{metrics.get('vol_ann_pct', np.nan):.2f}%")
            c[4].metric("Max drawdown", f"{metrics.get('max_drawdown_pct', np.nan):.2f}%")
            if isinstance(s.index, pd.DatetimeIndex):
                st.plotly_chart(px.line(x=s.index, y=s.values, labels={"x": "Ngày", "y": "VN-Index"}), use_container_width=True)

elif page == "🏠 Bất động sản":
    st.title("🏠 Phân tích bất động sản")
    re_df = st.session_state.real_estate
    if re_df.empty:
        st.warning("BĐS không có một feed công khai duy nhất đủ sạch để tôi giả định là 'giá thị trường thật'. Hãy nhập dữ liệu giao dịch/rao bán của nguồn bạn tin cậy tại Trung tâm dữ liệu.")
        st.markdown("**Tối thiểu:** ngày, khu vực, loại BĐS, giá bán. Nên có thêm diện tích và giá thuê tháng để tính giá/m² và rental yield.")
    else:
        locs = ["Tất cả"] + sorted(re_df["location"].dropna().astype(str).unique().tolist())
        loc = st.selectbox("Khu vực", locs)
        view = re_df if loc == "Tất cả" else re_df[re_df["location"] == loc]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Số mẫu", f"{len(view):,}")
        c2.metric("Giá trung vị", f"{view['price_vnd'].median()/1e9:,.2f} tỷ")
        c3.metric("Giá/m² trung vị", f"{view['price_per_m2'].median()/1e6:,.1f} tr/m²" if view["price_per_m2"].notna().any() else "Thiếu")
        c4.metric("Gross rental yield", f"{view['gross_rental_yield_pct'].median():.2f}%" if view["gross_rental_yield_pct"].notna().any() else "Thiếu")
        st.dataframe(view.sort_values("date", ascending=False), use_container_width=True)
        if view["date"].notna().any() and view["price_per_m2"].notna().any():
            monthly = view.dropna(subset=["date", "price_per_m2"]).set_index("date").resample("MS")["price_per_m2"].median().reset_index()
            st.plotly_chart(px.line(monthly, x="date", y="price_per_m2", markers=True, title="Giá/m² trung vị theo tháng"), use_container_width=True)
        if view["gross_rental_yield_pct"].notna().any():
            st.plotly_chart(px.box(view, x="property_type", y="gross_rental_yield_pct", points="all", title="Phân bố tỷ suất cho thuê gộp"), use_container_width=True)

elif page == "🍰 Hiệu quả kinh doanh":
    st.title("🍰 Hiệu quả vốn kinh doanh")
    c1, c2, c3 = st.columns(3)
    setup_cost = c1.number_input("Vốn setup/CAPEX", value=400_000_000.0, step=10_000_000.0)
    daily_volume = c1.number_input("Sản lượng/ngày", value=35.0, step=1.0)
    avg_price = c1.number_input("Giá bán TB/bánh", value=150_000.0, step=5_000.0)
    monthly_rent = c2.number_input("Thuê/tháng", value=25_000_000.0, step=1_000_000.0)
    monthly_labor = c2.number_input("Nhân sự/tháng", value=30_000_000.0, step=1_000_000.0)
    cogs_pct = c2.number_input("COGS %", value=35.0, step=1.0)
    monthly_utilities = c3.number_input("Điện nước/vận hành", value=8_000_000.0, step=500_000.0)
    monthly_marketing = c3.number_input("Marketing/tháng", value=7_000_000.0, step=500_000.0)
    res = bakery_metrics(setup_cost, avg_price, cogs_pct, monthly_rent, monthly_labor, monthly_utilities, monthly_marketing, daily_volume)
    st.session_state.bakery = res
    m = st.columns(5)
    m[0].metric("Doanh thu/tháng", f"{res['monthly_revenue']/1e6:,.1f} tr")
    m[1].metric("LN ròng/tháng", f"{res['monthly_net_profit']/1e6:,.1f} tr")
    m[2].metric("Hòa vốn", f"{res['breakeven_units_day']:.1f} bánh/ngày")
    m[3].metric("Hoàn vốn", "Không đạt" if not np.isfinite(res["payback_months"]) else f"{res['payback_months']:.1f} tháng")
    m[4].metric("ROIC năm", f"{res['roic_annual_pct']:.1f}%")

    st.subheader("Stress test nhanh")
    sales_drop = st.slider("Doanh số giảm", 0, 50, 20) / 100
    cogs_up = st.slider("COGS tăng", 0, 30, 10) / 100
    stress = bakery_metrics(
        setup_cost, avg_price, min(95, cogs_pct * (1 + cogs_up)), monthly_rent, monthly_labor,
        monthly_utilities, monthly_marketing, daily_volume * (1 - sales_drop)
    )
    st.metric("LN ròng/tháng trong stress", f"{stress['monthly_net_profit']/1e6:,.1f} tr")

elif page == "🧮 Phân bổ vốn & Monte Carlo":
    st.title("🧮 Phân bổ vốn & Monte Carlo")
    st.warning("Monte Carlo là mô phỏng theo giả định, không phải dự báo chắc chắn. Kết quả nhạy với expected return/volatility bạn nhập.")
    total_capital = st.number_input("Tổng vốn", value=2_000_000_000.0, step=100_000_000.0)

    # Auto-propose historical inputs where available, but keep editable.
    vni_s = best_series(vni_res, ["close", "price", "value"])
    vni_m = time_series_metrics(vni_s)
    gold_s = best_series(gold_res, ["close", "sell", "price", "value"])
    gold_m = time_series_metrics(gold_s)
    bakery_roic = (st.session_state.bakery or {}).get("roic_annual_pct", np.nan)

    default_assets = pd.DataFrame([
        {"asset": "Tiền gửi", "weight_pct": 30.0, "expected_return_pct": 5.0, "volatility_pct": 1.0},
        {"asset": "Vàng", "weight_pct": 20.0, "expected_return_pct": gold_m.get("cagr_pct", 5.0) if gold_m else 5.0, "volatility_pct": gold_m.get("vol_ann_pct", 15.0) if gold_m else 15.0},
        {"asset": "VN-Index", "weight_pct": 20.0, "expected_return_pct": vni_m.get("cagr_pct", 8.0) if vni_m else 8.0, "volatility_pct": vni_m.get("vol_ann_pct", 22.0) if vni_m else 22.0},
        {"asset": "BĐS", "weight_pct": 20.0, "expected_return_pct": 7.0, "volatility_pct": 10.0},
        {"asset": "Kinh doanh", "weight_pct": 10.0, "expected_return_pct": bakery_roic if pd.notna(bakery_roic) else 12.0, "volatility_pct": 30.0},
    ])
    edited = st.data_editor(default_assets, use_container_width=True, num_rows="fixed")
    if abs(edited["weight_pct"].sum() - 100) > 0.01:
        st.error(f"Tổng tỷ trọng hiện là {edited['weight_pct'].sum():.1f}%, phải bằng 100%.")
    years = st.slider("Số năm", 1, 20, 5)
    sims = st.select_slider("Số kịch bản", options=[1000, 5000, 10000, 20000, 50000], value=10000)
    if st.button("🎲 Chạy Monte Carlo", type="primary"):
        if abs(edited["weight_pct"].sum() - 100) <= 0.01:
            inp = edited.rename(columns={"weight_pct": "weight"}).copy()
            inp["weight"] = inp["weight"] / 100
            sim = monte_carlo_portfolio(total_capital, years, sims, inp)
            if sim.empty:
                st.error("Không đủ giả định để mô phỏng.")
            else:
                p10, p50, p90 = np.percentile(sim["final_value"], [10, 50, 90])
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("P10", f"{p10/1e9:,.2f} tỷ")
                c2.metric("P50", f"{p50/1e9:,.2f} tỷ")
                c3.metric("P90", f"{p90/1e9:,.2f} tỷ")
                c4.metric("Xác suất cuối kỳ < vốn gốc", f"{(sim['final_value'] < total_capital).mean()*100:.1f}%")
                st.plotly_chart(px.histogram(sim, x="final_value", nbins=80, title="Phân bố giá trị tài sản cuối kỳ"), use_container_width=True)

elif page == "🤖 Báo cáo AI tổng hợp":
    st.title("🤖 Báo cáo AI tổng hợp")
    st.caption("AI chỉ nhận dữ liệu đã lấy/tính trong ứng dụng. Không được tự tạo số liệu thiếu.")

    context: Dict[str, Any] = {
        "generated_at": datetime.now().isoformat(),
        "data_quality": {
            k: {"status": v.status, "source": v.source, "message": v.message} for k, v in all_results.items()
        },
        "gold": safe_df_summary(gold_res.data) if gold_res.ok else None,
        "fx": safe_df_summary(fx_res.data) if fx_res.ok else None,
        "cpi": safe_df_summary(macro_res["cpi"].data) if macro_res["cpi"].ok else None,
        "market_interest": safe_df_summary(macro_res["interest"].data) if macro_res["interest"].ok else None,
        "vnindex": safe_df_summary(vni_res.data) if vni_res.ok else None,
        "bank_rates": safe_df_summary(st.session_state.bank_rates, 30) if not st.session_state.bank_rates.empty else None,
        "real_estate": safe_df_summary(st.session_state.real_estate, 30) if not st.session_state.real_estate.empty else None,
        "bakery_metrics": st.session_state.bakery,
        "computed": {
            "gold_metrics": time_series_metrics(best_series(gold_res, ["close", "sell", "price", "value"])),
            "vnindex_metrics": time_series_metrics(best_series(vni_res, ["close", "price", "value"])),
        },
    }
    with st.expander("Xem dữ liệu sẽ gửi cho AI"):
        st.json(context)
    if st.button("🚀 Tạo báo cáo AI", type="primary"):
        try:
            with st.spinner("Đang lập báo cáo từ dữ liệu đã kiểm soát..."):
                report = generate_ai_report(context)
            st.markdown(report)
        except Exception as e:
            st.error(str(e))

# ==========================================
# 13. FOOTER / SOURCE DISCLOSURE
# ==========================================
st.divider()
st.caption(
    "Nguồn tham chiếu trong mã: SJC official, NHNN, GSO; tùy môi trường có thể dùng vnstock/vnstock_data. "
    "Bất kỳ connector nào lỗi đều phải hiển thị lỗi/thếu dữ liệu thay vì thay thế bằng dữ liệu ngẫu nhiên."
)
