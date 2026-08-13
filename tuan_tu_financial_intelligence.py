from __future__ import annotations

import json
import math
import os
from datetime import datetime
from typing import Any, Dict, Optional

import streamlit as st

st.set_page_config(page_title="Tuấn Tú Financial Intelligence", page_icon="🏛️", layout="wide")

APP_VERSION = "R2.1.3 CLOUD PERFORMANCE"

# -------------------------------------------------------------------
# STREAMLIT CLOUD SECRETS — NO DEFAULT PASSWORD
# -------------------------------------------------------------------
def get_secret(name: str, default: Any = None) -> Any:
    try:
        return st.secrets[name]
    except Exception:
        return default

USER_PASSWORD = get_secret("USER_PASSWORD")
GEMINI_API_KEY = get_secret("GEMINI_API_KEY")
GEMINI_MODEL = str(get_secret("GEMINI_MODEL", "gemini-3.5-flash"))
VNSTOCK_API_KEY = get_secret("VNSTOCK_API_KEY")

# Make optional provider keys visible to libraries through environment variables.
if GEMINI_API_KEY:
    os.environ["GEMINI_API_KEY"] = str(GEMINI_API_KEY)
os.environ["GEMINI_MODEL"] = GEMINI_MODEL
if VNSTOCK_API_KEY:
    os.environ["VNSTOCK_API_KEY"] = str(VNSTOCK_API_KEY)

# -------------------------------------------------------------------
# AUTH — STRICT STREAMLIT SECRETS ONLY
# -------------------------------------------------------------------
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False

if not USER_PASSWORD:
    st.error("🔒 Ứng dụng đang bị khóa vì Streamlit Cloud chưa có secret `USER_PASSWORD`.")
    st.code('USER_PASSWORD = "mat-khau-rieng-cua-ban"\nGEMINI_API_KEY = "..."\nGEMINI_MODEL = "gemini-3.5-flash"')
    st.caption("Vào Streamlit Community Cloud → Manage app → Settings → Secrets. Không có mật khẩu mặc định 123456.")
    st.stop()

if str(USER_PASSWORD).strip() in {"123456", "12345678", "admin", "password", "000000", "111111"}:
    st.error("🔒 USER_PASSWORD đang là mật khẩu mặc định/yếu và bị R2.1 từ chối. Hãy đổi Secret trên Streamlit Cloud.")
    st.stop()

if not st.session_state.logged_in:
    st.title("🏛️ TUẤN TÚ FINANCIAL INTELLIGENCE")
    st.caption(f"{APP_VERSION} • GitHub + Streamlit Cloud • dữ liệu tự động, không dùng Excel")
    with st.form("strict_login"):
        pwd = st.text_input("Mật khẩu", type="password")
        submitted = st.form_submit_button("Đăng nhập", use_container_width=True)
        if submitted:
            if str(pwd) == str(USER_PASSWORD):
                st.session_state.logged_in = True
                st.rerun()
            else:
                st.error("Mật khẩu không đúng.")
    st.stop()

# -------------------------------------------------------------------
# HEAVY IMPORTS — ONLY AFTER SUCCESSFUL LOGIN
# This keeps the login page fast on Streamlit Cloud.
# -------------------------------------------------------------------
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import plotly.express as px
import data_sources as ds

# Shorter network timeout for interactive pages. Scheduled GitHub collector can still
# perform deeper/longer refreshes outside the user's request cycle.
ds.TIMEOUT = 6

DataResult = ds.DataResult
best_price_series = ds.best_price_series
series_metrics = ds.series_metrics

try:
    from google import genai
except Exception:
    genai = None

# -------------------------------------------------------------------
# CLOUD DATA CACHE — PAGE-SCOPED + PARALLEL
# -------------------------------------------------------------------
@st.cache_data(ttl=900, show_spinner=False)
def load_bundle(city: str, page: str) -> Dict[str, DataResult]:
    """Load only the live datasets needed by the current page.

    Other datasets come from the last GitHub snapshot when available. This avoids
    running every connector + Gemini grounding on every Streamlit rerun.
    """
    snapshot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "auto_snapshot.json")
    snap = ds.load_snapshot_file(snapshot_path)

    labels = {
        "gold": ("Vàng SJC", "Auto source"),
        "fx": ("Tỷ giá ngân hàng", "Auto source"),
        "vnindex": ("VN-Index", "Vnstock Market"),
        "gold_world": ("Vàng thế giới", "Vnstock Market"),
        "usdvnd_history": ("USD/VND lịch sử", "Vnstock Market"),
        "cpi": ("CPI", "NSO"),
        "bank_rates": ("Lãi suất tiền gửi", "Official banks / snapshot"),
        "bds": (f"BĐS {city}", "Public sources / snapshot"),
    }

    out: Dict[str, DataResult] = {}
    for key, (name, source) in labels.items():
        if key in snap:
            out[key] = snap[key]
        else:
            out[key] = ds.missing(name, source, "Chưa có snapshot. Dữ liệu sẽ được lấy khi trang tương ứng được mở hoặc GitHub Actions cập nhật.", method="Lazy cloud loading")

    page_keys = {
        # Default page must be very fast: only three independent market calls.
        "📊 Tổng quan tự động": ["gold", "fx", "vnindex"],
        "🟡 Vàng & USD/VND": ["gold", "fx", "gold_world", "usdvnd_history"],
        "🏦 Lãi suất & CPI": ["cpi", "bank_rates"],
        "📈 Chứng khoán": ["vnindex"],
        "🏠 Bất động sản": ["bds"],
        "🧮 So sánh lợi suất": ["vnindex", "gold_world", "usdvnd_history", "cpi"],
        # AI and diagnostics are snapshot-first. Expensive grounded search runs in GitHub Actions,
        # not automatically inside a user's page rerun.
        "🤖 AI phân tích tổng hợp": [],
        "🩺 Chẩn đoán nguồn dữ liệu": [],
    }
    keys = page_keys.get(page, [])
    if not keys:
        return out

    def fetch_one(key: str) -> DataResult:
        if key == "gold":
            return ds.fetch_gold_current()
        if key == "fx":
            return ds.fetch_fx_current()
        if key == "vnindex":
            return ds.fetch_vnindex_history()
        if key == "gold_world":
            return ds.fetch_gold_world_history()
        if key == "usdvnd_history":
            return ds.fetch_usdvnd_history()
        if key == "cpi":
            return ds.fetch_cpi_official()
        if key == "bank_rates":
            # Interactive mode uses deterministic official sites only. Gemini Search is delegated
            # to scheduled collector.py so opening this page does not wait on an AI web search.
            return ds.fetch_bank_rates_official()
        if key == "bds":
            if city == "TP.HCM":
                return ds.fetch_bds_public_hcm()
            return ds.missing(f"BĐS {city}", "Public web", "Trang tương tác chưa có parser trực tiếp cho thành phố này; dùng snapshot GitHub nếu có.")
        return ds.missing(key, "Unknown", "Không có connector.")

    # Independent network/API calls are fetched concurrently instead of sequentially.
    live: Dict[str, DataResult] = {}
    max_workers = min(4, max(1, len(keys)))
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tt-fin") as pool:
        future_map = {pool.submit(fetch_one, key): key for key in keys}
        for fut in as_completed(future_map):
            key = future_map[fut]
            try:
                live[key] = fut.result()
            except Exception as exc:
                name, source = labels[key]
                live[key] = ds.failed(name, source, exc, method="Parallel lazy loading")

    # Prefer fresh live data. If live fails, keep a valid scheduled snapshot.
    for key in keys:
        current = live.get(key)
        old = snap.get(key)
        if current is not None and current.ok:
            out[key] = current
        elif old is not None and old.ok:
            old.source = f"{old.source} • GitHub snapshot fallback"
            old.method = "GitHub scheduled snapshot fallback"
            if current is not None:
                old.message = f"Live hiện {current.status}: {current.message}. Đang dùng snapshot gần nhất."
            out[key] = old
        elif current is not None:
            out[key] = current

    return out


def clear_and_reload() -> None:
    st.cache_data.clear()
    st.rerun()


def status_icon(status: str) -> str:
    return {"OK": "🟢", "THIẾU": "🟡", "LỖI": "🔴"}.get(status, "⚪")


def fmt_num(v: Any, digits: int = 2, suffix: str = "") -> str:
    try:
        if v is None or pd.isna(v):
            return "—"
        return f"{float(v):,.{digits}f}{suffix}"
    except Exception:
        return "—"


def show_sources(res: DataResult) -> None:
    st.caption(f"Nguồn: {res.source} • Phương thức: {res.method or '—'} • Cập nhật: {res.updated_at or '—'}")
    if res.message:
        st.caption(res.message)
    if res.source_urls:
        for url in res.source_urls[:8]:
            st.markdown(f"↗ {url}")


def show_result(res: DataResult, max_rows: int = 30) -> None:
    st.subheader(f"{status_icon(res.status)} {res.name}: {res.status}")
    show_sources(res)
    if res.ok and isinstance(res.data, pd.DataFrame):
        st.dataframe(res.data.tail(max_rows), use_container_width=True, hide_index=True)


def latest_usd_row(res: DataResult) -> Optional[pd.Series]:
    if not res.ok or not isinstance(res.data, pd.DataFrame):
        return None
    d = res.data
    if "currency" not in d.columns:
        return None
    x = d[d["currency"].astype(str).str.upper().str.contains("USD", na=False)]
    return x.iloc[0] if not x.empty else None


def latest_gold_row(res: DataResult) -> Optional[pd.Series]:
    if not res.ok or not isinstance(res.data, pd.DataFrame) or res.data.empty:
        return None
    d = res.data.copy()
    if "type" in d.columns:
        priority = d[d["type"].astype(str).str.contains("SJC|1L|miếng|mieng", case=False, regex=True, na=False)]
        if not priority.empty:
            return priority.iloc[0]
    return d.iloc[0]


def cpi_yoy(res: DataResult) -> Optional[float]:
    if not res.ok or not isinstance(res.data, pd.DataFrame) or res.data.empty:
        return None
    for col in ["yoy_pct", "avg_yoy_pct"]:
        if col in res.data.columns:
            v = pd.to_numeric(res.data[col], errors="coerce").dropna()
            if not v.empty:
                return float(v.iloc[-1])
    return None


def bank_12m_best(res: DataResult) -> Optional[pd.Series]:
    if not res.ok or not isinstance(res.data, pd.DataFrame):
        return None
    d = res.data.copy()
    if "term_months" not in d.columns or "annual_rate_pct" not in d.columns:
        return None
    d["term_months"] = pd.to_numeric(d["term_months"], errors="coerce")
    d["annual_rate_pct"] = pd.to_numeric(d["annual_rate_pct"], errors="coerce")
    x = d[d["term_months"] == 12].dropna(subset=["annual_rate_pct"])
    if x.empty:
        return None
    return x.sort_values("annual_rate_pct", ascending=False).iloc[0]


def metrics_for(res: DataResult) -> Dict[str, Any]:
    if not res.ok or not isinstance(res.data, pd.DataFrame):
        return {}
    return series_metrics(best_price_series(res.data))


def jsonable_context(bundle: Dict[str, DataResult], city: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"city": city, "generated_at": datetime.now().astimezone().isoformat(), "datasets": {}}
    for key, r in bundle.items():
        item = {"status": r.status, "source": r.source, "message": r.message, "updated_at": r.updated_at, "source_urls": r.source_urls, "method": r.method}
        if r.ok and isinstance(r.data, pd.DataFrame):
            d = r.data.tail(40).copy()
            for c in d.columns:
                if pd.api.types.is_datetime64_any_dtype(d[c]):
                    d[c] = d[c].astype(str)
            item["rows"] = d.replace({np.nan: None}).to_dict(orient="records")
        out["datasets"][key] = item
    out["metrics"] = {
        "vnindex": metrics_for(bundle["vnindex"]),
        "gold_world": metrics_for(bundle["gold_world"]),
        "usdvnd_history": metrics_for(bundle["usdvnd_history"]),
        "cpi_yoy_pct": cpi_yoy(bundle["cpi"]),
    }
    return out


# -------------------------------------------------------------------
# SIDEBAR
# -------------------------------------------------------------------
st.sidebar.title("🏛️ FINANCIAL INTELLIGENCE")
st.sidebar.caption(APP_VERSION)
st.sidebar.success("☁️ Cloud mode: không đọc/ghi Excel")

city = st.sidebar.selectbox("Thị trường BĐS", ["TP.HCM", "Hà Nội", "Đà Nẵng", "Bình Dương", "Đồng Nai"], index=0)
if st.sidebar.button("🔄 Làm mới dữ liệu ngay", use_container_width=True):
    clear_and_reload()
if st.sidebar.button("🚪 Đăng xuất", use_container_width=True):
    st.session_state.logged_in = False
    st.rerun()

page = st.sidebar.radio(
    "Khu vực",
    [
        "📊 Tổng quan tự động",
        "🟡 Vàng & USD/VND",
        "🏦 Lãi suất & CPI",
        "📈 Chứng khoán",
        "🏠 Bất động sản",
        "🧮 So sánh lợi suất",
        "🤖 AI phân tích tổng hợp",
        "🩺 Chẩn đoán nguồn dữ liệu",
    ],
)

load_started = time.perf_counter()
bundle = load_bundle(city, page)
load_elapsed = time.perf_counter() - load_started

# -------------------------------------------------------------------
# PAGES
# -------------------------------------------------------------------
if page == "📊 Tổng quan tự động":
    st.title("📊 Trung tâm dữ liệu tài chính tự động")
    st.info("Không nhập Excel. Giao diện lấy dữ liệu theo từng trang và chạy song song; nguồn AI/grounding nặng được giao cho GitHub Actions. Cache 15 phút để thao tác không phải gọi lại toàn bộ Internet.")

    keys = ["gold", "fx", "cpi", "bank_rates", "vnindex", "bds"]
    cols = st.columns(6)
    for col, key in zip(cols, keys):
        r = bundle[key]
        col.metric(r.name, f"{status_icon(r.status)} {r.status}")
    st.caption(f"Tải trang: {load_elapsed:.2f}s • Cache 15 phút • dữ liệu nặng/AI grounding chạy qua GitHub Actions, không chặn giao diện.")

    gold_row = latest_gold_row(bundle["gold"])
    usd_row = latest_usd_row(bundle["fx"])
    best_bank = bank_12m_best(bundle["bank_rates"])
    vni_m = metrics_for(bundle["vnindex"])
    cpi = cpi_yoy(bundle["cpi"])

    st.subheader("Chỉ số nhanh")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Vàng SJC bán", f"{float(gold_row['sell'])/1e6:,.2f} tr/lượng" if gold_row is not None and pd.notna(gold_row.get("sell")) else "Thiếu")
    c2.metric("USD/VND bán", f"{float(usd_row['sell']):,.0f}" if usd_row is not None and pd.notna(usd_row.get("sell")) else "Thiếu")
    c3.metric("CPI YoY", fmt_num(cpi, 2, "%"))
    if best_bank is not None:
        c4.metric("LS 12T cao nhất đọc được", f"{best_bank['annual_rate_pct']:.2f}%", str(best_bank.get("bank", "")))
    else:
        c4.metric("LS 12T", "Thiếu")
    c5.metric("VN-Index 30D", fmt_num(vni_m.get("ret_30d_pct"), 2, "%"))

    st.subheader("Độ tin cậy theo nguồn")
    quality = pd.DataFrame([
        {"dataset": r.name, "status": r.status, "source": r.source, "method": r.method, "updated_at": r.updated_at, "message": r.message}
        for r in bundle.values()
    ])
    st.dataframe(quality, use_container_width=True, hide_index=True)

elif page == "🟡 Vàng & USD/VND":
    st.title("🟡 Vàng & USD/VND")
    c1, c2 = st.columns(2)
    with c1:
        show_result(bundle["gold"], 30)
        gr = latest_gold_row(bundle["gold"])
        if gr is not None:
            a, b, c = st.columns(3)
            a.metric("Mua", f"{gr['buy']/1e6:,.2f} tr")
            b.metric("Bán", f"{gr['sell']/1e6:,.2f} tr")
            c.metric("Spread", f"{gr['spread']/1e6:,.2f} tr")
    with c2:
        show_result(bundle["fx"], 30)
        ur = latest_usd_row(bundle["fx"])
        if ur is not None:
            a, b, c = st.columns(3)
            a.metric("Mua CK USD", f"{ur['buy_transfer']:,.0f}")
            b.metric("Bán USD", f"{ur['sell']:,.0f}")
            c.metric("Spread", f"{ur['spread']:,.0f}")

    st.subheader("Chuỗi lịch sử thị trường")
    for key, title in [("gold_world", "Vàng thế giới"), ("usdvnd_history", "USD/VND")]:
        r = bundle[key]
        show_result(r, 5)
        if r.ok and isinstance(r.data, pd.DataFrame):
            s = best_price_series(r.data)
            if s is not None and isinstance(s.index, pd.DatetimeIndex):
                st.plotly_chart(px.line(x=s.index, y=s.values, labels={"x": "Ngày", "y": title}, title=title), use_container_width=True)
                m = series_metrics(s)
                cc = st.columns(4)
                cc[0].metric("30 ngày", fmt_num(m.get("ret_30d_pct"), 2, "%"))
                cc[1].metric("1 năm", fmt_num(m.get("ret_365d_pct"), 2, "%"))
                cc[2].metric("Vol năm hóa", fmt_num(m.get("vol_ann_pct"), 2, "%"))
                cc[3].metric("Max drawdown", fmt_num(m.get("max_drawdown_pct"), 2, "%"))

elif page == "🏦 Lãi suất & CPI":
    st.title("🏦 Lãi suất, CPI & lợi suất thực")
    show_result(bundle["cpi"], 10)
    show_result(bundle["bank_rates"], 100)

    rates = bundle["bank_rates"].data if bundle["bank_rates"].ok and isinstance(bundle["bank_rates"].data, pd.DataFrame) else pd.DataFrame()
    if not rates.empty:
        rates = rates.copy()
        rates["term_months"] = pd.to_numeric(rates["term_months"], errors="coerce")
        rates["annual_rate_pct"] = pd.to_numeric(rates["annual_rate_pct"], errors="coerce")
        terms = sorted(int(x) for x in rates["term_months"].dropna().unique() if x >= 1)
        if terms:
            term = st.selectbox("Kỳ hạn", terms, index=terms.index(12) if 12 in terms else 0)
            view = rates[rates["term_months"] == term].dropna(subset=["annual_rate_pct"]).sort_values("annual_rate_pct", ascending=False)
            st.plotly_chart(px.bar(view, x="bank", y="annual_rate_pct", text="annual_rate_pct", hover_data=[c for c in ["channel", "source_url"] if c in view.columns], title=f"Lãi suất VND kỳ hạn {term} tháng"), use_container_width=True)
            inflation = cpi_yoy(bundle["cpi"])
            if inflation is not None and not view.empty:
                view = view.copy()
                view["real_rate_pct"] = ((1 + view["annual_rate_pct"] / 100) / (1 + inflation / 100) - 1) * 100
                st.subheader("Lợi suất thực sau CPI")
                st.dataframe(view[[c for c in ["bank", "annual_rate_pct", "real_rate_pct", "channel", "source_url"] if c in view.columns]], use_container_width=True, hide_index=True)

elif page == "📈 Chứng khoán":
    st.title("📈 VN-Index")
    show_result(bundle["vnindex"], 10)
    if bundle["vnindex"].ok:
        s = best_price_series(bundle["vnindex"].data)
        if s is not None:
            m = series_metrics(s)
            c = st.columns(6)
            c[0].metric("Mức gần nhất", fmt_num(m.get("latest"), 2))
            c[1].metric("7D", fmt_num(m.get("ret_7d_pct"), 2, "%"))
            c[2].metric("30D", fmt_num(m.get("ret_30d_pct"), 2, "%"))
            c[3].metric("1Y", fmt_num(m.get("ret_365d_pct"), 2, "%"))
            c[4].metric("Vol", fmt_num(m.get("vol_ann_pct"), 2, "%"))
            c[5].metric("Max DD", fmt_num(m.get("max_drawdown_pct"), 2, "%"))
            if isinstance(s.index, pd.DatetimeIndex):
                st.plotly_chart(px.line(x=s.index, y=s.values, title="VN-Index", labels={"x": "Ngày", "y": "Điểm"}), use_container_width=True)

elif page == "🏠 Bất động sản":
    st.title(f"🏠 Bất động sản — {city}")
    st.warning("BĐS tự động lấy từ nguồn công khai. Giá chào bán/tin rao được giữ đúng nhãn và KHÔNG coi là giá giao dịch công chứng.")
    show_result(bundle["bds"], 100)
    if bundle["bds"].ok and isinstance(bundle["bds"].data, pd.DataFrame):
        d = bundle["bds"].data.copy()
        numeric = [c for c in ["min_million_vnd_m2", "max_million_vnd_m2", "median_million_vnd_m2"] if c in d.columns]
        if numeric and "area" in d.columns:
            plot_col = "median_million_vnd_m2" if "median_million_vnd_m2" in d.columns and d["median_million_vnd_m2"].notna().any() else "max_million_vnd_m2"
            if plot_col in d.columns:
                v = d.dropna(subset=[plot_col]).sort_values(plot_col)
                if not v.empty:
                    st.plotly_chart(px.bar(v, x="area", y=plot_col, color="property_type" if "property_type" in v.columns else None, title="Giá tham khảo theo khu vực (triệu VND/m²)"), use_container_width=True)

elif page == "🧮 So sánh lợi suất":
    st.title("🧮 So sánh lợi suất quan sát")
    st.caption("Đây là so sánh dữ liệu quan sát, không phải dự báo chắc chắn.")
    vni = metrics_for(bundle["vnindex"])
    gold = metrics_for(bundle["gold_world"])
    usd = metrics_for(bundle["usdvnd_history"])
    best_bank = bank_12m_best(bundle["bank_rates"])
    inflation = cpi_yoy(bundle["cpi"])

    rows = [
        {"asset": "VN-Index", "1Y_return_pct": vni.get("ret_365d_pct"), "cagr_pct": vni.get("cagr_pct"), "vol_pct": vni.get("vol_ann_pct"), "max_drawdown_pct": vni.get("max_drawdown_pct")},
        {"asset": "Vàng thế giới", "1Y_return_pct": gold.get("ret_365d_pct"), "cagr_pct": gold.get("cagr_pct"), "vol_pct": gold.get("vol_ann_pct"), "max_drawdown_pct": gold.get("max_drawdown_pct")},
        {"asset": "USD/VND", "1Y_return_pct": usd.get("ret_365d_pct"), "cagr_pct": usd.get("cagr_pct"), "vol_pct": usd.get("vol_ann_pct"), "max_drawdown_pct": usd.get("max_drawdown_pct")},
    ]
    if best_bank is not None:
        nominal = float(best_bank["annual_rate_pct"])
        real = ((1 + nominal/100)/(1 + inflation/100)-1)*100 if inflation is not None else None
        rows.append({"asset": f"Tiền gửi 12T — {best_bank.get('bank','')}", "1Y_return_pct": nominal, "cagr_pct": nominal, "vol_pct": None, "max_drawdown_pct": None, "real_after_cpi_pct": real})
    compare = pd.DataFrame(rows)
    st.dataframe(compare, use_container_width=True, hide_index=True)

    capital = st.number_input("Số vốn muốn so sánh (VNĐ)", min_value=0.0, value=2_000_000_000.0, step=100_000_000.0)
    if best_bank is not None:
        rate = float(best_bank["annual_rate_pct"])
        st.metric("Tiền lãi danh nghĩa sau 12 tháng theo mức cao nhất đọc được", f"{capital*rate/100:,.0f} ₫", f"{best_bank.get('bank','')} {rate:.2f}%")

elif page == "🤖 AI phân tích tổng hợp":
    st.title("🤖 AI phân tích tổng hợp")
    if not GEMINI_API_KEY:
        st.error("Chưa có `GEMINI_API_KEY` trong Streamlit Secrets. Các connector không cần Gemini vẫn hoạt động.")
    elif genai is None:
        st.error("Thiếu package google-genai trong requirements.txt.")
    else:
        context = jsonable_context(bundle, city)
        st.caption("AI chỉ được phép dùng dữ liệu đã thu tự động trong context; dataset thiếu phải được nói là thiếu.")
        if st.button("🚀 Phân tích toàn bộ dữ liệu hiện tại", type="primary"):
            prompt = f"""
Bạn là hệ thống phân tích tài chính Việt Nam. Chỉ sử dụng JSON bên dưới. Không tự tạo số liệu ngoài JSON.
Phải phân biệt: dữ liệu quan sát, giá chào bán BĐS, lãi suất niêm yết, chỉ số tính toán và nhận định.
Nếu status khác OK thì nêu rõ thiếu dữ liệu.
Hãy phân tích: vàng, USD/VND, CPI, tiền gửi ngân hàng, VN-Index, BĐS; so sánh lợi suất/rủi ro/thanh khoản; nêu 3 kịch bản thận trọng-cân bằng-tăng trưởng nhưng không hứa lợi nhuận; chỉ ra dữ liệu nào chưa đủ tin cậy.

JSON:
{json.dumps(context, ensure_ascii=False, default=str)}
"""
            with st.spinner("Đang phân tích..."):
                try:
                    client = genai.Client(api_key=GEMINI_API_KEY)
                    resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
                    st.markdown(resp.text)
                except Exception as e:
                    st.error(f"Gemini lỗi: {e}")

elif page == "🩺 Chẩn đoán nguồn dữ liệu":
    st.title("🩺 Chẩn đoán nguồn dữ liệu")
    st.success(f"Đang chạy: {APP_VERSION}")
    st.write({
        "entrypoint": __file__,
        "working_directory": os.getcwd(),
        "streamlit_cloud_mode": True,
        "USER_PASSWORD_loaded": bool(USER_PASSWORD),
        "GEMINI_API_KEY_loaded": bool(GEMINI_API_KEY),
        "VNSTOCK_API_KEY_loaded": bool(VNSTOCK_API_KEY),
        "GEMINI_MODEL": GEMINI_MODEL,
        "cache_ttl_seconds": 900,
        "current_page_load_seconds": round(load_elapsed, 3),
        "interactive_network_timeout_seconds": ds.TIMEOUT,
        "loading_mode": "page-scoped parallel + snapshot fallback",
    })
    diag = pd.DataFrame([
        {"key": key, "name": r.name, "status": r.status, "source": r.source, "method": r.method, "updated_at": r.updated_at, "message": r.message}
        for key, r in bundle.items()
    ])
    st.dataframe(diag, use_container_width=True, hide_index=True)
    st.markdown("### Ý nghĩa")
    st.markdown("- 🟢 **OK**: lấy được dữ liệu tự động.\n- 🟡 **THIẾU**: nguồn hiện không trả đủ dữ liệu; hệ thống không tự bịa số.\n- 🔴 **LỖI**: connector gặp lỗi kỹ thuật.\n\nKhông có bất kỳ nút Upload Excel nào trong R2.1.")

st.divider()
st.caption("R2.1.3 CLOUD PERFORMANCE • Page-scoped parallel loading + GitHub snapshot fallback • không Excel/SQLite làm nguồn chính.")
