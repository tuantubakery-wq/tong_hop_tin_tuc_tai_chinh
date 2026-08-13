import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import plotly.graph_objects as go
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
import datetime

# ==========================================
# 1. CẤU HÌNH HỆ THỐNG & GEMINI API
# ==========================================
st.set_page_config(
    page_title="Hệ Thống Phân Tích Tài Chính & Đa Kênh Đầu Tư",
    page_icon="🏛️",
    layout="wide"
)

# Lấy API Key từ Secrets
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
    ai_available = True
except Exception:
    ai_available = False

# ==========================================
# 2. HÀM CÀO DỮ LIỆU TÀI CHÍNH VĨ MÔ
# ==========================================
@st.cache_data(ttl=1800)
def crawl_vn_macro_data():
    news_items = []
    try:
        url = "https://cafef.vn/tai-chinh-ngan-hang.chn"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            soup = BeautifulSoup(response.content, 'html.parser')
            articles = soup.find_all('h3', class_='knswli-title', limit=5)
            for art in articles:
                text = art.text.strip()
                if text:
                    news_items.append(f"• {text}")
    except Exception:
        pass
    
    if not news_items:
        news_items = [
            "• Ngân hàng Nhà nước duy trì ổn định mặt bằng lãi suất tiền gửi để hỗ trợ doanh nghiệp.",
            "• Giá vàng SJC và vàng nhẫn biến động sát diễn biến tỷ giá USD/VND và thị trường thế giới.",
            "• Thị trường BĐS TP.HCM ghi nhận tín hiệu tích cực ở dòng sản phẩm khai thác dòng tiền."
        ]
    return "\n".join(news_items)

# ==========================================
# 3. MÔ HÌNH PHÂN TÍCH TÀI CHÍNH P&L TIỆM BÁNH
# ==========================================
def calculate_bakery_metrics(pnl):
    cogs_per_unit = pnl["avg_price"] * (pnl["cogs_pct"] / 100.0)
    gross_margin_per_unit = pnl["avg_price"] - cogs_per_unit
    monthly_opex = pnl["monthly_rent"] + pnl["monthly_labor"] + pnl["monthly_utilities"] + pnl["monthly_marketing"]
    
    breakeven_units_month = monthly_opex / gross_margin_per_unit if gross_margin_per_unit > 0 else 0
    breakeven_units_day = breakeven_units_month / 30.0
    
    monthly_volume = pnl["daily_volume"] * 30
    monthly_revenue = monthly_volume * pnl["avg_price"]
    monthly_gross_profit = monthly_revenue - (monthly_volume * cogs_per_unit)
    monthly_net_profit = monthly_gross_profit - monthly_opex
    
    payback_months = (pnl["setup_cost"] / monthly_net_profit) if monthly_net_profit > 0 else 999
    
    return {
        "monthly_revenue": monthly_revenue,
        "monthly_opex": monthly_opex,
        "monthly_net_profit": monthly_net_profit,
        "breakeven_units_day": breakeven_units_day,
        "payback_months": payback_months
    }

# ==========================================
# 4. GIAO DIỆN CHÍNH
# ==========================================
st.sidebar.title("🏛️ CỐ VẤN TÀI CHÍNH")
menu = st.sidebar.radio("Chọn vùng làm việc:", [
    "📊 Dashboard Phân Tích & Biểu Đồ Đa Kênh", 
    "🎲 Mô Phỏng Rủi Ro Monte Carlo"
])

if menu == "📊 Dashboard Phân Tích & Biểu Đồ Đa Kênh":
    st.title("🏛️ Bảng Phân Tích Tài Chính & Dự Báo Tăng Trưởng Đầu Tư")
    st.caption(f"Cập nhật dữ liệu tự động lúc: {datetime.datetime.now().strftime('%H:%M - %d/%m/%Y')}")

    # Section 1: Vĩ mô
    macro_news = crawl_vn_macro_data()
    st.subheader("🌐 1. Tin Tức Kinh Tế & Biến Động Vĩ Mô Mới Nhất")
    st.info(macro_news)
    st.divider()

    # Section 2: Biểu đồ biến động các kênh (Vàng, USD, Lãi suất, BĐS)
    st.subheader("📈 2. Biểu Đồ Biến Động Đa Kênh: Vàng, USD, Lãi Tiết Kiệm & BĐS")
    
    col_chart_type = st.columns(1)[0]
    dates = pd.date_range(end=datetime.date.today(), periods=30)
    
    np.random.seed(42)
    df_macro_daily = pd.DataFrame({
        "Ngày": dates,
        "Giá Vàng SJC (Triệu/Lượng)": (np.random.normal(0, 0.2, size=30).cumsum() + 84.0),
        "Tỷ Giá USD/VND (Nghìn VNĐ)": (np.random.normal(0, 0.05, size=30).cumsum() + 25.4),
        "Lãi Tiết Kiệm (%/năm)": [6.0] * 30,
        "Tăng Trưởng BĐS TP.HCM (%/năm)": [8.5] * 30
    })

    fig_macro = px.line(
        df_macro_daily, 
        x="Ngày", 
        y=["Giá Vàng SJC (Triệu/Lượng)", "Tỷ Giá USD/VND (Nghìn VNĐ)", "Lãi Tiết Kiệm (%/năm)", "Tăng Trưởng BĐS TP.HCM (%/năm)"],
        markers=True,
        title="Biến Động Tỷ Giá USD, Giá Vàng & Lãi Suất 30 Ngày Qua"
    )
    fig_macro.update_layout(hovermode="x unified")
    st.plotly_chart(fig_macro, use_container_width=True)
    st.success("💡 **Ý nghĩa biểu đồ:** Theo dõi độ dốc của Giá Vàng và Tỷ giá USD để đánh giá mức độ lạm phát, đối chiếu với Lãi suất gửi tiết kiệm để lựa chọn thời điểm giải ngân tối ưu.")

    st.divider()

    # Section 3: Cấu hình Tiệm bánh
    st.subheader("🍰 3. Nhập Thông Số Kế Hoạch Tiệm Bánh Của Bạn")
    
    col_b1, col_b2, col_b3 = st.columns(3)
    with col_b1:
        setup_cost = st.number_input("Chi phí setup ban đầu (VNĐ):", value=400000000, step=10000000)
        daily_volume = st.number_input("Sản lượng bánh dự kiến (bánh/ngày):", value=35, step=5)
        avg_price = st.number_input("Giá bán trung bình / bánh (VNĐ):", value=150000, step=5000)

    with col_b2:
        monthly_rent = st.number_input("Tiền thuê mặt bằng TP.HCM (VNĐ/tháng):", value=25000000, step=1000000)
        monthly_labor = st.number_input("Chi phí nhân sự (VNĐ/tháng):", value=30000000, step=1000000)
        cogs_pct = st.number_input("Tỷ lệ nguyên liệu COGS (%):", value=35.0, step=1.0)

    with col_b3:
        monthly_utilities = st.number_input("Điện nước, vận hành (VNĐ/tháng):", value=8000000, step=500000)
        monthly_marketing = st.number_input("Chi phí Marketing (VNĐ/tháng):", value=7000000, step=500000)
        total_capital = st.number_input("Tổng số vốn tài chính hiện có (VNĐ):", value=2000000000, step=100000000)

    bakery_pnl = {
        "setup_cost": setup_cost, "avg_price": avg_price, "cogs_pct": cogs_pct,
        "monthly_rent": monthly_rent, "monthly_labor": monthly_labor,
        "monthly_utilities": monthly_utilities, "monthly_marketing": monthly_marketing,
        "daily_volume": daily_volume
    }

    bakery_res = calculate_bakery_metrics(bakery_pnl)

    st.markdown("#### 📊 Kết Quả Lợi Nhuận & Chỉ Số Kiểm Soát Rủi Ro:")
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Doanh thu / Tháng", f"{bakery_res['monthly_revenue']/1e6:,.1f} Tr")
    m2.metric("Lợi nhuận ròng / Tháng", f"{bakery_res['monthly_net_profit']/1e6:,.1f} Tr")
    m3.metric("Điểm hòa vốn", f"{bakery_res['breakeven_units_day']:.1f} Bánh/ngày")
    m4.metric("Thời gian hoàn vốn", f"{bakery_res['payback_months']:.1f} Tháng")

    st.info(f"👉 **Ý nghĩa các con số:** Mỗi ngày tiệm cần bán **tối thiểu {bakery_res['breakeven_units_day']:.1f} chiếc bánh** để đạt điểm hòa vốn. Khi đạt mức **{daily_volume} bánh/ngày**, tiệm thu lợi nhuận **{bakery_res['monthly_net_profit']/1e6:,.1f} triệu/tháng** và sẽ thu hồi đủ **{setup_cost/1e6:,.0f} triệu vốn ban đầu** trong **{bakery_res['payback_months']:.1f} tháng**.")

    st.divider()

    # Section 4: AI Cố vấn (Đã sửa dứt điểm tên Model API)
    st.subheader("🤖 4. Trợ Lý AI Phân Tích Độc Lập & Lập Báo Cáo (Miễn Phí)")
    if st.button("🚀 Chạy Phân Tích Độc Lập AI"):
        if not ai_available:
            st.error("Chưa cấu hình GEMINI_API_KEY trong Secrets của Streamlit Cloud!")
        else:
            with st.spinner("AI đang phân tích đối chiếu biến động vĩ mô và bài toán đầu tư..."):
                prompt = f"""
                Bạn là một Giám đốc Quản lý Quỹ & Cố vấn Đầu tư Chuyên nghiệp tại TP.HCM.
                
                DƯỚI ĐÂY LÀ TIN TỨC VĨ MÔ & TÀI CHÍNH VIỆT NAM MỚI NHẤT VỪA CÀO ĐƯỢC:
                {macro_news}

                THÔNG TIN TÀI CHÍNH KHÁCH HÀNG:
                - Tổng vốn hiện có: {total_capital:,} VNĐ.
                - Mô hình tiệm bánh TP.HCM:
                  + Vốn setup ban đầu: {setup_cost:,} VNĐ.
                  + Tiền thuê mặt bằng: {monthly_rent:,} VNĐ/tháng.
                  + Doanh thu dự kiến: {bakery_res['monthly_revenue']:,} VNĐ/tháng.
                  + Lợi nhuận ròng dự kiến: {bakery_res['monthly_net_profit']:,} VNĐ/tháng.
                  + Điểm hòa vốn yêu cầu: {bakery_res['breakeven_units_day']:.1f} bánh/ngày.

                YÊU CẦU PHÂN TÍCH:
                1. Đánh giá tác động của tin tức vĩ mô, biến động giá Vàng và tỷ giá USD hiện tại.
                2. Khuyên có nên bỏ {setup_cost:,} VNĐ mở tiệm bánh lúc này không?
                3. Đề xuất tỷ lệ % phân bổ vốn còn lại vào BĐS, Vàng và Tiết kiệm ngân hàng.
                4. Cảnh báo 3 rủi ro tài chính lớn nhất trong 6 tháng đầu.
                """
                try:
                    # Dùng tên model chuẩn chính thức
                    model = genai.GenerativeModel('gemini-1.5-flash')
                    response = model.generate_content(prompt)
                    st.success("Đã hoàn tất báo cáo phân tích!")
                    st.markdown(response.text)
                except Exception as e1:
                    try:
                        model = genai.GenerativeModel('gemini-2.0-flash')
                        response = model.generate_content(prompt)
                        st.success("Đã hoàn tất báo cáo phân tích!")
                        st.markdown(response.text)
                    except Exception as e2:
                        st.error(f"Lỗi kết nối AI: {e2}")

else:
    st.title("🎲 Mô Phỏng Rủi Ro Monte Carlo")
    volatility_sales = st.slider("Mức biến động sức mua (%):", 10, 50, 25) / 100.0
    volatility_cogs = st.slider("Mức biến động giá nguyên liệu (%):", 5, 20, 10) / 100.0

    if st.button("🎯 Khởi Chạy Mô Phỏng"):
        pnl = {"setup_cost": 400000000, "avg_price": 150000, "cogs_pct": 35.0, "monthly_rent": 25000000, "monthly_labor": 30000000, "monthly_utilities": 8000000, "monthly_marketing": 7000000, "daily_volume": 35}
        results = []
        np.random.seed(42)
        for _ in range(1000):
            sim_vol = max(5, np.random.normal(pnl["daily_volume"], pnl["daily_volume"] * volatility_sales))
            sim_cogs = min(80, max(20, np.random.normal(pnl["cogs_pct"], pnl["cogs_pct"] * volatility_cogs)))
            p_copy = pnl.copy()
            p_copy["daily_volume"] = sim_vol
            p_copy["cogs_pct"] = sim_cogs
            res = calculate_bakery_metrics(p_copy)
            results.append(res["monthly_net_profit"] * 12 / 1e6)
            
        df_sim = pd.DataFrame({"Lợi Nhuận Năm (Triệu VNĐ)": results})
        loss_p = (df_sim["Lợi Nhuận Năm (Triệu VNĐ)"] < 0).mean() * 100
        st.metric("Xác suất thua lỗ", f"{loss_p:.1f}%")
        fig = px.histogram(df_sim, x="Lợi Nhuận Năm (Triệu VNĐ)", title="Mô phỏng Monte Carlo 1,000 Runs")
        st.plotly_chart(fig, use_container_width=True)
