# TUẤN TÚ FINANCIAL INTELLIGENCE — R2.1 CLOUD AUTO-DATA

## Kiến trúc đúng cho dự án này

- **GitHub**: lưu source code + snapshot tự động gần nhất.
- **Streamlit Community Cloud**: chạy ứng dụng web.
- **Không dùng Excel/CSV upload để cấp dữ liệu thị trường.**
- **Không dùng SQLite local làm kho dữ liệu chính.**
- Dữ liệu sống được gọi tự động khi app mở; cache 15 phút.
- Khi phiên Streamlit đang mở, `st.fragment(run_every="15m")` tự kiểm tra lại dashboard.
- GitHub Actions chạy theo lịch sáng/chiều và cập nhật `data/auto_snapshot.json`; app dùng snapshot này làm fallback nếu connector live tạm lỗi.

## Các nguồn tự động

1. **Vàng SJC hiện tại**: `vnstock.Retail().gold(source="sjc")`, fallback BTMC.
2. **USD/VND ngân hàng hiện tại**: `vnstock.Retail().exchange_rate()`, fallback Vietcombank official HTTP.
3. **VN-Index lịch sử**: Vnstock Market.
4. **Vàng thế giới lịch sử**: Vnstock Market commodity.
5. **USD/VND lịch sử**: Vnstock Market forex.
6. **CPI**: parser bài công bố mới nhất của Cơ quan Thống kê Quốc gia (NSO).
7. **Lãi suất tiền gửi**: Vietcombank + VietinBank official; nếu có Gemini API key thì Google Search Grounding tự tìm thêm BIDV/Agribank/MB/Techcombank/ACB/VPBank/Sacombank từ website chính thức của từng ngân hàng.
8. **Bất động sản**: dữ liệu công khai Batdongsan.com.vn/Nhà Tốt; nếu có Gemini API key thì Google Search Grounding bổ sung dữ liệu mới nhất từ các nguồn công khai. Luôn phân biệt **giá chào bán** với **giá giao dịch thực**.

## 1. Đưa lên GitHub

Đặt toàn bộ cấu trúc này ở root repository:

```text
repo/
├── tuan_tu_financial_intelligence.py
├── data_sources.py
├── collector.py
├── requirements.txt
├── data/
│   └── auto_snapshot.json
├── .streamlit/
│   └── config.toml
└── .github/
    └── workflows/
        └── refresh_financial_data.yml
```

Không commit `.streamlit/secrets.toml`.

## 2. Cấu hình Streamlit Community Cloud Secrets

Trong Streamlit: **Manage app → Settings → Secrets**.

```toml
USER_PASSWORD = "MAT_KHAU_RIENG_MANH_CUA_BAN"
GEMINI_API_KEY = "GEMINI_KEY_CUA_BAN"
GEMINI_MODEL = "gemini-3.6-flash"
VNSTOCK_API_KEY = "VNSTOCK_KEY_CUA_BAN"
```

- `USER_PASSWORD`: bắt buộc. R2.1 không có fallback `123456`.
- `GEMINI_API_KEY`: khuyến nghị để tự động lấy thêm BĐS và lãi suất nhiều ngân hàng bằng Search Grounding, đồng thời chạy báo cáo AI.
- `VNSTOCK_API_KEY`: khuyến nghị để tăng hạn mức Vnstock Community; nếu không có, một số hàm vẫn có thể chạy ở guest/community mode tùy nguồn.

## 3. Deploy trên Streamlit Community Cloud

- Repo: repository GitHub của bạn.
- Branch: `main`.
- Main file path: `tuan_tu_financial_intelligence.py`.
- Python: nên chọn **3.12**.

Streamlit sẽ tự cài `requirements.txt` từ GitHub.

## 4. Cấu hình GitHub Actions Secrets

Trong GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**.

Tạo nếu có:

- `GEMINI_API_KEY`
- `VNSTOCK_API_KEY`

Workflow không cần `USER_PASSWORD` vì collector không đăng nhập vào app.

GitHub Actions sẽ tự chạy:

- 08:15 và 16:15 giờ Việt Nam từ thứ Hai đến thứ Sáu.
- 09:15 cuối tuần.

Nó tự ghi `data/auto_snapshot.json` và commit chỉ khi dữ liệu thay đổi.

## 5. Nguyên tắc an toàn dữ liệu

- Không lấy được nguồn → `THIẾU` hoặc `LỖI`.
- Không tạo giá vàng/USD/lãi suất giả để lấp chỗ trống.
- Snapshot GitHub chỉ là fallback của dữ liệu **đã được bot tự lấy trước đó**, không phải dữ liệu thủ công.
- BĐS công khai thường là giá chào bán/tin rao. App không được phép gọi nó là giá giao dịch công chứng.
- AI phân tích chỉ dùng dữ liệu đã thu trong bundle; dataset thiếu phải được ghi rõ thiếu.

## 6. Cách làm mới

- Tự động: cache live 15 phút + fragment Streamlit 15 phút khi session mở.
- Nền: GitHub Actions theo lịch.
- Thủ công khi cần: nút **🔄 Làm mới dữ liệu ngay** trong app hoặc GitHub → Actions → Refresh financial data → Run workflow.
