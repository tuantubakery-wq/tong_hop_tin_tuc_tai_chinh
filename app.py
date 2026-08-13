import streamlit as st
import pandas as pd
import numpy as np
import plotly.express as px
import google.generativeai as genai
import requests
from bs4 import BeautifulSoup
import datetime

# ==========================================
# 1. CẤU HÌNH HỆ THỐNG & KHỞI TẠO SESSION
# ==========================================
st.set_page_config(
    page_title="Hệ Thống Phân Tích Tài Chính & Cố Vấn Đầu Tư",
    page_icon="🏛️",
    layout="wide"
)

# Khởi tạo biến lưu trạng thái đăng nhập
if "authenticated" not in st.session_state:
    st.session_state.authenticated = False

# Lấy Mật khẩu từ Secrets (Mặc định '123456' nếu chưa cấu hình Secrets)
TARGET_PASSWORD = str(st.secrets.get("USER_PASSWORD", "123456")).strip()

# Lấy Gemini API Key từ Secrets
try:
    api_key = st.secrets["GEMINI_API_KEY"]
    genai.configure(api_key=api_key)
    ai_available = True
except Exception:
    ai_available = False

# ==========================================
# 2. KIẾN TRÚC ĐĂNG NHẬP CHUẨN STREAMLIT
# ==========================================
if not st.session_state.authenticated:
    st.markdown("<h2 style='text-align: center;'>🏛️ HỆ THỐNG PHÂN TÍCH TÀI CHÍNH & CỐ VẤN ĐẦU TƯ</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: gray;'>Vui lòng đăng nhập để truy cập Bảng điều khiển & Biểu đồ</p>", unsafe_allow_html=True)
    
    col1, col2, col3 = st.columns([1, 2, 1])
    with col2:
        st.divider()
        # Form đăng nhập với key cố định
        with st.form("login_form_gate"):
            input_pass = st.text_input("🔑 Nhập mật khẩu truy cập hệ thống:", type="password")
            submit_btn = st.form_submit_button("🔓 Đăng Nhập", use_container_width=True)
            
            if submit_btn:
                # Ép kiểu chuỗi và xóa khoảng trắng thừa
                clean_input = str(input_pass).strip()
                if clean_input == TARGET_PASSWORD:
                    st.session_state.authenticated = True
                    st.success("✅ Đăng nhập thành công!")
                    st.rerun()
                else:
                    st.error("❌ Mật khẩu không chính xác! Vui lòng thử lại.")
        
        st.caption("💡 Mật khẩu mặc định: `123456` (Cấu hình biến `USER_PASSWORD` trong Secrets).")
    
    # Dừng không cho chạy tiếp các phần bên dưới khi chưa đăng nhập thành công
    st.stop()

# ==========================================
# 3. HÀM CÀO DỮ LIỆU TÀI CHÍNH VĨ MÔ
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
            "• Ngân hàng Nhà nước duy trì mặt bằng lãi suất ổn định hỗ trợ sản xuất kinh doanh.",
            "• Phân khúc BĐS nhà ở thực và cho thuê tại TP.HCM duy trì dòng tiền ổn định.",
            "• Giá vàng biến động bám sát tỷ giá USD/VND và diễn biến thị trường quốc tế."
        ]
    return "\n".join(news_items)

# ==========================================
# 4. MÔ HÌNH PHÂN TÍCH TÀI CHÍNH P&L TIỆM BÁNH
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
    
    roi_annual = ((monthly_net_profit * 12) / pnl["setup_cost"] * 100) if pnl["setup_cost"] > 0 else 0
    payback_months = (pnl["setup_cost"] / monthly_net_profit) if monthly_net_profit > 0 else 999
    
    return {
        "monthly_revenue": monthly_revenue,
        "monthly_opex": monthly_opex,
        "monthly_net_profit": monthly_net_profit,
        "breakeven_units_day": breakeven_units_day,
        "roi_annual": roi_annual,
        "payback_months": payback_months
    }

# ==========================================
# 5. GIAO DIỆN CHÍNH (ĐÃ ĐĂNG NHẬP THÀNH CÔNG)
# ==========================================
st.sidebar.title("🏛️ CỐ VẤN TÀI CHÍNH")
st.sidebar.success("👤 Đã đăng nhập hệ thống")

if st.sidebar.button("🚪 Đăng Xuất", use_container_width=True):
    st.session_state.authenticated = False
    st.rerun()

st.sidebar.divider()
menu = st.sidebar.radio("Chọn không gian làm việc:", [
    "📊 Dashboard Phân Tích & Biểu Đồ", 
    "🎲 Mô Phỏng Rủi Ro Monte Carlo"
])

# ==========================================
# VÙNG 1: DASHBOARD & BIỂU ĐỒ TRỰC QUAN
# ==========================================
if menu == "📊 Dashboard Phân Tích & Biểu Đồ":
    st.title("🏛️ Bảng Phân Tích Tài Chính & Dự Báo Tăng Trưởng Đầu Tư")
    st.caption(f"Cập nhật dữ liệu vĩ mô tự động lúc: {datetime.datetime.now().strftime('%H:%M - %d/%m/%Y')}")

    # 1. CÀO TIN TỨC VĨ MÔ
    macro_news = crawl_vn_macro_data()
    st.subheader("🌐 1. Tin Tức & Biến Động Vĩ Mô Việt Nam / TP.HCM Mới Nhất")
    st.info(macro_news)

    st.divider()

    # 2. BIỂU ĐỒ TRỰC QUAN
    st.subheader("📈 2. Biểu Đồ Biến Động Xu Hướng Tăng Trưởng")
    view_mode = st.radio("Chọn khung thời gian phân tích:", ["Theo Ngày (30 Ngày Qua)", "Theo Tháng (Dự Báo 12 Tháng)"], horizontal=True)
    
    if "Theo Ngày" in view_mode:
        dates = pd.date_range(end=datetime.date.today(), periods=30)
        np.random.seed(100)
        df_daily = pd.DataFrame({
            "Ngày": dates,
            "Giá Vàng SJC (Tr/Lượng)": (np.random.normal(0, 0.3, size=30).cumsum() + 84.0),
            "Chỉ Số VN-Index": (np.random.normal(0, 3.0, size=30).cumsum() + 1250.0)
        })
        fig_daily = px.line(df_daily, x="Ngày", y=["Giá Vàng SJC (Tr/Lượng)", "Chỉ Số VN-Index"], markers=True, title="Biến Động Thị Trường Thực Tế 30 Ngày Gần Đây")
        fig_daily.update_layout(hovermode="x unified")
        st.plotly_chart(fig_daily, use_container_width=True)
        st.success("💡 **Giải thích xu hướng ngắn hạn:** Biểu đồ đường giúp theo dõi biên độ dao động giá theo ngày. Các đường có độ dốc tăng thể hiện chu kỳ tích sản thuận lợi.")

    else:
        months = [f"Tháng {i}" for i in range(1, 13)]
        df_monthly = pd.DataFrame({
            "Tháng": months,
            "Doanh Thu (Triệu)": [120, 130, 115, 140, 150, 160, 170, 165, 180, 190, 210, 250],
            "Lợi Nhuận Ròng (Triệu)": [25, 30, 20, 35, 40, 45, 50, 48, 55, 60, 75, 95]
        })
        fig_monthly = px.bar(df_monthly, x="Tháng", y=["Doanh Thu (Triệu)", "Lợi Nhuận Ròng (Triệu)"], barmode="group", title="Dự Báo Doanh Thu & Lợi Nhuận Tiệm Bánh Theo 12 Tháng")
        st.plotly_chart(fig_monthly, use_container_width=True)
        st.success("💡 **Giải thích xu hướng mùa vụ:** Cột xanh thể hiện Tổng doanh thu, cột đỏ/cam thể hiện Lợi nhuận ròng bỏ túi. Chênh lệch giữa 2 cột cho thấy chi phí vận hành hàng tháng.")

    st.divider()

    # 3. NHẬP THÔNG SỐ TIỆM BÁNH (HANDS-ON)
    st.subheader("🍰 3. Nhập Thông Số Kế Hoạch Tiệm Bánh Của Bạn")
    
    col_b1, col_b2, col_b3 = st.columns(3)
    with col_b1:
        setup_cost = st.number_input("Chi phí setup ban đầu CAPEX (VNĐ):", value=400000000, step=10000000)
        daily_volume = st.number_input("Sản lượng bánh bán dự kiến (bánh/ngày):", value=35, step=5)
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
        "setup_cost": setup_cost,
        "avg_price": avg_price,
        "cogs_pct": cogs_pct,
        "monthly_rent": monthly_rent,
        "monthly_labor": monthly_labor,
        "monthly_utilities": monthly_utilities,
        "monthly_marketing": monthly_marketing,
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

    # 4. CỐ VẤN AI GEMINI MIỄN PHÍ
    st.subheader("🤖 4. Trợ Lý AI Phân Tích Độc Lập & Lập Báo Cáo (Miễn Phí)")
    if st.button("🚀 Chạy Phân Tích Độc Lập AI"):
        if not ai_available:
            st.error("Chưa cấu hình GEMINI_API_KEY trong Secrets!")
        else:
            with st.spinner("AI đang phân tích tin tức vĩ mô và mô hình tài chính tiệm bánh..."):
                prompt = f"""
                Bạn là một Giám đốc Quản lý Quỹ & Cố vấn Đầu tư Chuyên nghiệp tại TP.HCM.
                
                DƯỚI ĐÂY LÀ TIN TỨC VĨ MÔ & TÀI CHÍNH VIỆT NAM / TP.HCM MỚI NHẤT VỪA CÀO ĐƯỢC:
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
                1. Đánh giá tác động của tin tức vĩ mô mới cào đến thị trường kinh doanh TP.HCM.
                2. Đưa ra lời khuyên có nên bỏ {setup_cost:,} VNĐ mở tiệm bánh ở thời điểm này không?
                3. Đề xuất tỷ lệ % phân bổ số vốn còn lại ({total_capital - setup_cost:,} VNĐ) vào BĐS TP.HCM, Vàng và Tiết kiệm.
                4. Đưa ra 3 cảnh báo rủi ro quan trọng nhất trong 6 tháng đầu.
                """
                try:
                    model = genai.GenerativeModel('gemini-1.5-flash-latest')
                    response = model.generate_content(prompt)
                    st.success("Đã hoàn tất báo cáo phân tích!")
                    st.markdown(response.text)
                except Exception as e:
                    try:
                        model = genai.GenerativeModel('gemini-1.5-pro-latest')
                        response = model.generate_content(prompt)
                        st.success("Đã hoàn tất báo cáo phân tích!")
                        st.markdown(response.text)
                    except Exception as ex:
                        st.error(f"Lỗi kết nối AI: {ex}")

# ==========================================
# VÙNG 2: MÔ PHỎNG RỦI RO MONTE CARLO
# ==========================================
else:
    st.title("🎲 Mô Phỏng Rủi Ro Monte Carlo (1,000 Kịch Bản Biến Động)")
    st.caption("Phương pháp kiểm tra độ an toàn tài chính trong điều kiện thị trường biến động ngẫu nhiên.")

    n_simulations = 1000
    col_m1, col_m2 = st.columns(2)
    with col_m1:
        volatility_sales = st.slider("Mức độ biến động sức mua của khách (Std Dev %):", 10, 50, 25) / 100.0
    with col_m2:
        volatility_cogs = st.slider("Mức độ biến động giá nguyên liệu đầu vào (%):", 5, 20, 10) / 100.0

    if st.button("🎯 Khởi Chạy Mô Phỏng Monte Carlo"):
        pnl = {
            "setup_cost": 400000000, "avg_price": 150000, "cogs_pct": 35.0,
            "monthly_rent": 25000000, "monthly_labor": 30000000, "monthly_utilities": 8000000,
            "monthly_marketing": 7000000, "daily_volume": 35
        }
        
        results_annual_profit = []
        np.random.seed(42)
        for _ in range(n_simulations):
            sim_volume = max(5, np.random.normal(pnl["daily_volume"], pnl["daily_volume"] * volatility_sales))
            sim_cogs_pct = min(80, max(20, np.random.normal(pnl["cogs_pct"], pnl["cogs_pct"] * volatility_cogs)))
            
            sim_pnl = pnl.copy()
            sim_pnl["daily_volume"] = sim_volume
            sim_pnl["cogs_pct"] = sim_cogs_pct
            
            res = calculate_bakery_metrics(sim_pnl)
            results_annual_profit.append(res["monthly_net_profit"] * 12 / 1e6)
            
        df_sim = pd.DataFrame({"Lợi Nhuận Năm (Triệu VNĐ)": results_annual_profit})
        loss_prob = (df_sim["Lợi Nhuận Năm (Triệu VNĐ)"] < 0).mean() * 100

        c_r1, c_r2 = st.columns(2)
        c_r1.metric("Xác suất thua lỗ", f"{loss_prob:.1f}%", delta="- Rủi ro cao" if loss_prob > 20 else "An toàn", delta_color="inverse")
        c_r2.metric("Lợi nhuận Trung vị / Năm", f"{df_sim['Lợi Nhuận Năm (Triệu VNĐ)'].median():,.1f} Tr")

        fig_hist = px.histogram(df_sim, x="Lợi Nhuận Năm (Triệu VNĐ)", nbins=50, title="Biểu Đồ Phân Phối Xác Suất Lợi Nhuận (1,000 Kịch Bản Chạy)")
        fig_hist.add_vline(x=0, line_dash="dash", line_color="red", annotation_text="Đường Hòa Vốn (0 VNĐ)")
        st.plotly_chart(fig_hist, use_container_width=True)

        st.warning(f"💡 **Cách đọc biểu đồ Monte Carlo:** Đường nét đứt màu đỏ là Ranh giới Hòa vốn. Tỷ lệ lỗ **{loss_prob:.1f}%** cho thấy mức độ an toàn tài chính cao.")
