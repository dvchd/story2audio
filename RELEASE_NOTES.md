# 🌟 Lịch sử Phát triển (Release Notes) - Story2Audio

Dưới đây là tài liệu tổng hợp lại toàn bộ các tính năng, cải tiến và bản vá lỗi từ lúc khởi tạo dự án cho tới nay.

---

## [v3.2.0] - Chunk cues cho Google TTS (tự cuộn theo đoạn)

### ✨ Tính năng mới

- **Chunk cues cho Google TTS (gTTS):**
  - gTTS không có `WordBoundary` nên mỗi cue = một chunk audio, với timing thật được đo từ duration MP3 của từng chunk (`mp3_duration_seconds`).
  - Giới hạn chunk gTTS xuống `GTTS_HARD_MAX_CHUNK = 1200` ký tự để granularity tốt (client có thể nội suy vị trí theo số ký tự bên trong chunk).
  - Tích hợp khongdich: độc giả nghe giọng Google (Nữ Miền Bắc) giờ được **tự cuộn tới đoạn đang đọc** như giọng Edge.

- **Cờ khả năng `scroll_supported`:**
  - Meta, `/tts/status`, `/tts/start`, `/tts/cues` và SSE stream trả thêm `scroll_supported` (true cho edge + gtts) tách biệt với `subtitle_supported` (chỉ edge) — karaoke phụ đề vẫn chỉ dành cho Edge, còn cues phục vụ auto-scroll có ở cả hai engine.

- **Cache validity cho gTTS:**
  - `is_cache_valid` thêm `require_cues` — cache gTTS cũ (sinh trước v3.2.0, không có cues) tự động bị coi là hết hạn và tái tạo lại ở lần nghe tiếp theo.

- **Giới hạn dung lượng cache (`MAX_CACHE_MB`):**
  - Env mới `MAX_CACHE_MB` (mặc định `20480` = 20 GB). Khi thư mục `audio_cache` vượt giới hạn, service tự động xóa các cache cũ nhất (theo mtime, xóa theo từng nhóm cache_id) tới khi về dưới 90% giới hạn.
  - Chạy một lần lúc khởi động + sau mỗi lần tạo audio; bỏ qua các cache đang được tạo để không xóa file đang ghi.

---

## [v3.0.0] - Bản cập nhật Phụ đề Trực tiếp & Redesign Giao diện

Bản cập nhật lớn tiếp theo mang đến tính năng **Phụ đề trực tiếp (Live Subtitles)** cho Edge TTS, redesign toàn bộ giao diện người dùng và cải tiến sâu kiến trúc backend.

### ✨ Tính năng mới (Features)

- **Phụ đề trực tiếp (Live Subtitles) cho Edge TTS:**
  - Thu thập dữ liệu `WordBoundary` và `SentenceBoundary` từ Edge TTS API, bao gồm thời gian bắt đầu, kết thúc và nội dung từng từ.
  - Gom word boundary thành các **cue** có ý nghĩa (mỗi cue là một cụm từ hoàn chỉnh), tối ưu riêng cho tiếng Latin (tối đa 8 từ/cue) và tiếng CJK (tối đa 12 token/cue).
  - Hiển thị phụ đề song song với audio player trên giao diện web, tự động cuộn và bôi sáng theo tiến độ phát.

- **Phát trực tiếp phụ đề qua SSE (Server-Sent Events):**
  - Endpoint `/tts/cues/stream/{cache_id}` stream cues theo thời gian thực ngay khi đang tạo audio. Frontend nhận từng cue qua event type `cue`, `complete` và `error`.
  - Ghi incremental cues vào file `.cues.jsonl` (JSON Lines) để hỗ trợ streaming không gián đoạn.

- **Xuất phụ đề đa định dạng:**
  - Tự động tạo file **SRT** (`.srt`) — tương thích với trình phát video và phần mềm chỉnh sửa phụ đề.
  - Tự động tạo file **WebVTT** (`.vtt`) — định dạng chuẩn cho web, tương thích HTML5 `<video>`.
  - Tự động tạo file **Cues JSON** (`.cues.json`) — dữ liệu cấu trúc để tích hợp API bên thứ ba.
  - API endpoint tải về riêng biệt: `/tts/subtitle/srt/{cache_id}`, `/tts/subtitle/vtt/{cache_id}`, `/tts/cues/{cache_id}`.

- **Tính toán thời lượng MP3 tự động:**
  - Hàm `mp3_duration_seconds()` phân tích frame header MP3 để tính chính xác thời lượng mỗi chunk audio.
  - Thời lượng được sử dụng để căn chỉnh offset thời gian phụ đề giữa các chunk audio, đảm bảo phụ đề đồng bộ chính xác.

- **Redesign giao diện người dùng:**
  - Bố cục mới dạng card-based, bảng màu hiện đại, responsive trên mọi kích thước màn hình.
  - Bộ chuyển đổi ngôn ngữ (Language Switcher) tích hợp trên giao diện — thay thế cơ chế inject locale từ server bằng client-side i18n hoàn toàn.
  - Tiêu đề cập nhật: "Story to Audio + Live Subtitle".

### 🚀 Cải tiến hiệu năng & Tối ưu hóa (Enhancements)

- **Cải thiện quản lý Cache:**
  - Thêm tham số `require_subtitles` cho `is_cache_valid()` để đảm bảo cache được xem xét đầy đủ cả audio và phụ đề (đối với Edge TTS).
  - Thêm hàm `remove_runtime_files_only()` để xóa các file runtime (audio, subtitle) nhưng giữ lại metadata khi cần tái tạo.
  - Thay thế các hàm xóa file lặp (`remove_audio_file`, `remove_meta_file`) bằng hàm chung `remove_if_exists()`.
  - Ghi file JSON/Text atomically (`write_json_atomic`, `write_text_atomic`) bằng cơ chế ghi file tạm + rename để tránh file bị hỏng khi crash.

- **Mở rộng Metadata Cache:**
  - Thêm các trường mới: `subtitle_supported` (boolean), `subtitle_ready` (boolean), `subtitle_cues` (số lượng cues đã tạo), `duration_seconds` (tổng thời lượng audio).
  - API status và start response trả thêm thông tin phụ đề để Frontend quyết định có hiển thị UI phụ đề hay không.

- **Tối ưu hóa Endpoint `/`:**
  - Loại bỏ cơ chế inject voice registry và locale từ server-side (không cần replace placeholder trong HTML).
  - Sử dụng placeholder đơn giản `__APP_VERSION__` thay vì khối JavaScript inject phức tạp.

- **Endpoint `/health` nâng cấp:** Trả thêm trường `version` trong response.

- **Refactoring mã nguồn:**
  - Loại bỏ các docstring/comment thừa, đơn giản hóa logic các hàm helper.
  - Chuẩn hóa thứ tự import, format code nhất quán.
  - Nâng cấp hàm `edge_tts_to_bytes()` thành `edge_tts_to_audio_and_words()` trả về tuple `(audio_bytes, words)`.

### 🗑️ File bị xóa

- `static/locales/*.json` (7 file: en, vi, ja, zh, ko, fr, de) — Chuyển sang cơ chế client-side i18n, không cần locale file trên server.
- `tests/conftest.py` — File test cấu hình cũ, cần viết lại cho v3.0.0.
- `tests/test_chunking.py` — File test cũ, cần viết lại cho v3.0.0.

### ⚠️ Breaking Changes

- **Phiên bản nâng cấp v2.0.2 → v3.0.0:** Đây là bản thay đổi breaking do xóa locale files và thay đổi cấu trúc response API.
- **Tính năng phụ đề chỉ hỗ trợ Edge TTS:** Google TTS (gTTS) không cung cấp word boundary nên không thể tạo phụ đề. API trả `subtitle_supported: false` khi dùng gTTS.
- **File test bị xóa:** Cần viết lại test suite cho v3.0.0 trong một PR riêng.
- **Metadata cache cũ không tương thích:** Cache từ v2.x không chứa thông tin phụ đề. Hệ thống sẽ tự động tái tạo cache khi yêu cầu mới (hoặc khi `require_subtitles=True`).

---

## [v2.0.0] - Bản cập nhật Đa ngôn ngữ & Tối ưu hoá Hệ thống

Bản cập nhật lớn đánh dấu sự hỗ trợ vươn ra quốc tế, tối ưu hóa giao diện người dùng và cải tiến hiệu năng server.

### ✨ Tính năng mới (Features)
- **Hỗ trợ 7 Ngôn ngữ (i18n):** Mở rộng hỗ trợ từ tiếng Việt sang tiếng Anh (EN), Nhật (JA), Trung (ZH), Hàn (KO), Pháp (FR) và Đức (DE).
- **Hỗ trợ giọng đọc Bản địa (Native Voices):** Tích hợp danh sách các giọng đọc Neural chất lượng cao của Edge-TTS tương ứng cho từng quốc gia (ví dụ: Xiaoxiao cho tiếng Trung, Nanami cho tiếng Nhật...).
- **In-memory Locale Cache & Zero-fetch:**
  - Nhúng trực tiếp (inject) file ngôn ngữ tiếng Việt (`vi.json`) và Danh sách giọng đọc (`EDGE_VOICES`) thẳng vào HTML từ server. Trình duyệt không cần gọi API lúc tải trang đầu tiên.
  - Sử dụng In-memory cache cho các file ngôn ngữ khác, loại bỏ tình trạng tải lại file JSON (redundant fetches) khi người dùng chuyển đổi qua lại giữa các tab ngôn ngữ.
- **Biến môi trường linh hoạt:** Bổ sung cấu hình `HOST` và `PORT` cho Uvicorn, giúp dễ dàng deploy ứng dụng trên đa nền tảng.

### 🚀 Cải tiến hiệu năng & Tối ưu hóa (Enhancements)
- **Cải tiến Text Chunking:** Ngăn chặn việc chạy thuật toán phân tách đoạn (split chunks) hai lần. Chuyển kết quả tính toán (chunk_preview) trực tiếp vào Background Task.
- **Tối ưu UTF-8 HTML Injection:** Sửa lỗi `json.dumps()` mặc định mã hóa các ký tự Unicode sang ASCII (`\uXXXX`), tối ưu hóa băng thông bằng cách trả về raw UTF-8 thuần túy (`ensure_ascii=False`).
- **Nâng cấp Debug API:** Ẩn endpoint `/tts/debug/chunks` phía sau cờ môi trường `ENABLE_DEBUG_TTS`, tự động block (HTTP 404) khi chạy trên Production.
- **Nâng cấp HTTP Stream Exception:** Thay vì trả về 404 hay 425 khi audio đang chạy, API nay trả về `HTTP 503 Service Unavailable` kèm theo header `Retry-After: 5`, thông báo cho client thời điểm nên thử tải lại.

### 🐛 Sửa lỗi (Bug Fixes)
- Sửa lỗi **Mất Tiếng Việt khi chuyển Tab:** Khắc phục lỗi logic bị ghi đè biến global khiến ngôn ngữ tiếng Việt hiển thị sai sau khi người dùng đổi qua ngôn ngữ khác rồi đổi lại.
- Fix **Race Condition trong Xóa Cache:** Đảm bảo hệ thống ưu tiên đọc trạng thái trên ổ đĩa (`get_effective_status`) thay vì chỉ dùng RAM để chặn tình trạng xóa nhầm cache file đang trong quá trình tạo.
- Ngăn chặn lỗi **Stale State:** Buộc gỡ bỏ trạng thái của job khỏi bộ nhớ RAM ngay khi tiến trình xử lý Terminal (Completed/Failed) được lưu xuống ổ cứng.
- Fix **Redundant JS aria-label:** Loại bỏ vòng lặp gán `aria-label` thừa thãi trên Javascript vì chúng đã được cài đặt cứng trên DOM HTML.

---

## [v1.1.0] - Bản cập nhật Trải nghiệm Người dùng & Quản lý Download

Tập trung nâng cao trải nghiệm tải tệp và tinh chỉnh hệ thống phân tách văn bản.

### ✨ Tính năng mới (Features)
- Bổ sung nút **"Tải xuống MP3" (Download MP3)** trên giao diện người dùng.

### 🚀 Cải tiến & Sửa lỗi (Enhancements & Fixes)
- **Quản lý Nút tải xuống thông minh:** Chỉ cho phép người dùng nhìn thấy nút Tải xuống MP3 **sau khi** quá trình nối file Audio hoàn thành hoàn toàn (hoặc ngay ở chunk đầu tiên của MediaSource Stream).
- **Cải tiến thuật toán Text Normalization & Chunking:** Tinh chỉnh mạnh mẽ logic cắt câu chữ để hỗ trợ các ký tự xuống dòng đặc biệt và từ ngữ dài, giúp giọng đọc tự nhiên, liền mạch hơn.
- Cấu hình hỗ trợ chạy qua **HTTP/HTTPS Proxy** thông qua biến môi trường `PROXY` (hữu ích cho các server bị chặn port/Edge-TTS).

---

## [v1.0.0] - Phiên bản Khởi tạo (Initial Release)

Phiên bản đầu tiên của Story2Audio với kiến trúc Core FastAPI và Live Streaming.

### ✨ Tính năng cốt lõi (Core Features)
- Xây dựng thành công hệ thống **Text-to-Speech (TTS) Web App** dùng **FastAPI**.
- Tích hợp 2 Engine TTS phổ biến: **Edge-TTS** (Chính) và **Google TTS** (Phụ).
- Hỗ trợ **Phát trực tiếp theo thời gian thực (Live Streaming)** thông qua giao thức **MediaSource Extension (MSE)** của Javascript. Hệ thống sẽ cắt nhỏ văn bản, tạo audio từng khúc và stream ngay lập tức dưới dạng bytes thay vì phải chờ nguyên bài.
- Xây dựng cơ chế **Cache & Metadata Management:** Tính toán mã Hash `md5` của nội dung và lưu trữ `.mp3` kèm metadata `.json` (Tiến độ xử lý, kích thước file).
- Cấu hình **Docker** và `docker-compose.yml` để dễ dàng triển khai.

### 🐛 Sửa lỗi (Bug Fixes)
- Sửa lỗi **Premature Termination** (Dừng đột ngột) khi stream audio trên trình duyệt.
- Thay đổi method `/tts/start` sang `POST` để ngăn giới hạn độ dài của URL khi người dùng gửi tiểu thuyết/truyện quá dài.
- Nâng cấp Endpoint `/tts/stream` bằng tính năng Stability Checking giúp quá trình gửi data xuống Frontend không bị ngắt quãng nửa chừng.
