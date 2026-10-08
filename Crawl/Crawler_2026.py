import os
import csv
import sys
import time
import random
import logging
import urllib.request
import ssl
import signal
import subprocess
import re
import json
import certifi
import socket
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
import undetected_chromedriver as uc

# Fix SSL certificate trên macOS & đặt timeout socket mạng tránh treo khi rớt mạng
os.environ['SSL_CERT_FILE'] = certifi.where()
os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
ssl._create_default_https_context = ssl._create_unverified_context
socket.setdefaulttimeout(25)  # Tránh treo vô tận khi khởi tạo kết nối mạng chập chờn

# Ép Python ưu tiên dùng IPv4 (tránh lỗi IPv6 blackhole gây timeout/chậm trên macOS)
old_getaddrinfo = socket.getaddrinfo
def getaddrinfo_ipv4_only(host, port, family=0, type=0, proto=0, flags=0):
    return old_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = getaddrinfo_ipv4_only

# ==============================================================================
# CẤU HÌNH HỆ THỐNG
# ==============================================================================

WORK_DIR = os.path.dirname(os.path.abspath(__file__))

# Các file dữ liệu & trạng thái
OUTPUT_LINKS_FILE    = os.path.join(WORK_DIR, "links_queue.csv")
OUTPUT_DATA_FILE     = os.path.join(WORK_DIR, "Hanoi_RealEstate_2026_Raw.csv")
LOG_FILE             = os.path.join(WORK_DIR, "crawler_2026.log")
PROGRESS_PHASE1_FILE = os.path.join(WORK_DIR, "harvest_progress.json")
FAILED_LINKS_FILE    = os.path.join(WORK_DIR, "failed_links.csv")
CF_TRACKER_FILE      = os.path.join(WORK_DIR, "cf_retry_tracker.json")

# 3 Phân khúc BĐS mục tiêu theo Đề cương Khóa luận
CATEGORIES = [
    {
        "type": "condo",
        "label": "Căn hộ chung cư",
        "url_pattern": "https://batdongsan.com.vn/ban-can-ho-chung-cu-ha-noi/p{page}",
        "max_pages": 1760,
    },
    {
        "type": "private_house",
        "label": "Nhà riêng & Nhà mặt phố",
        "url_pattern": "https://batdongsan.com.vn/ban-nha-rieng-ha-noi/p{page}",
        "max_pages": 1550,
    },
    {
        "type": "villa",
        "label": "Biệt thự & Liền kề",
        "url_pattern": "https://batdongsan.com.vn/ban-nha-biet-thu-lien-ke-ha-noi/p{page}",
        "max_pages": 700,
    },
]

# Các cột dữ liệu đầu ra (khớp hoàn toàn với schema của Đề tài & Pipeline Clean Data)
FIELDNAMES = [
    "id", "property_type", "title2", "links", "title", "description",
    "price", "price_per_m2", "area",
    "district", "city", "address", "project_name",
    "floors", "bedrooms", "toilets",
    "house_direction", "balcony",
    "street_frontage", "road_width", "legal", "interior", "date",
]

# Cột cho file failed_links.csv
FAILED_FIELDNAMES = ["id", "link", "property_type", "reason", "timestamp"]

# Tham số điều khiển tốc độ & an toàn
BATCH_FLUSH_SIZE     = 5        # Ghi đĩa định kỳ sau mỗi 5 bài (an toàn tuyệt đối)
DRIVER_RESTART_EVERY = 200      # Khởi động lại Chrome sau 200 bài để giải phóng RAM triệt để (tránh renderer timeout)
SLEEP_BETWEEN_PAGES  = (1.0, 2.0) # Nghỉ giữa các trang danh mục (giây)
SLEEP_BETWEEN_ITEMS  = (1.2, 2.2) # Nghỉ giữa các bài chi tiết (vừa an toàn vừa nhanh: ~20h cho 20k bài)
PAGE_LOAD_TIMEOUT    = 45       # Timeout tải trang (giây)
CF_WAIT_TIMEOUT      = 20       # Thời gian chờ Cloudflare tự giải (giây)
MAX_CF_RETRIES       = 3        # Số lần retry tối đa cho bài bị CF chặn trước khi bỏ cuộc
MAX_CONSECUTIVE_ERRORS = 10     # Ngưỡng lỗi liên tiếp tối đa kích hoạt backoff sâu
EMPTY_PAGES_THRESHOLD  = 3      # Số trang trống liên tiếp để dừng phân khúc (chạm đáy web)
REDIRECT_THRESHOLD     = 3      # Số lần bị redirect khỏi trang yêu cầu liên tiếp để dừng phân khúc (Deep Paging)
ZERO_NEW_THRESHOLD     = 20     # Số trang liên tiếp thu được 0 link mới (khoảng ~500 bài trùng ID) để dừng phân khúc (tránh nhầm khi resume)
REFRESH_RECENT_PAGES   = 10     # Số trang đầu quét lại để bắt tin mới khi resume Pha 1 (tăng lên 10 trang)

# ==============================================================================
# QUẢN LÝ TÍN HIỆU NGẮT (SIGNAL HANDLING - DỪNG AN TOÀN KHI NHẤN CTRL+C)
# ==============================================================================

shutdown_requested = False

def setup_signal_handlers():
    """Bắt tín hiệu SIGINT (Ctrl+C) và SIGTERM để dừng an toàn mà không làm hỏng dữ liệu."""
    global shutdown_requested

    def handle_signal(signum, frame):
        global shutdown_requested
        if not shutdown_requested:
            sig_name = "Ctrl+C (SIGINT)" if signum == signal.SIGINT else f"Signal {signum}"
            print(f"\n\n🛑 ĐÃ NHẬN TÍN HIỆU {sig_name}!")
            print("⏳ Đang hoàn tất bài hiện tại, lưu toàn bộ dữ liệu xuống đĩa và đóng Chrome...")
            print("👉 Vui lòng chờ vài giây, KHÔNG tắt cửa sổ Terminal...")
            shutdown_requested = True
        else:
            print("\n⚠️ Nhấn lần thứ 2: Đang ép buộc dừng khẩn cấp...")
            sys.exit(1)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)


def safe_sleep(seconds):
    """Ngủ ngắt được ngay lập tức nếu người dùng gửi tín hiệu dừng (Ctrl+C)."""
    step = 0.2
    waited = 0.0
    while waited < seconds:
        if shutdown_requested:
            break
        time.sleep(min(step, seconds - waited))
        waited += step


# ==============================================================================
# THIẾT LẬP HỆ THỐNG LOG
# ==============================================================================

def setup_logging():
    """Thiết lập log ghi ra cả màn hình Terminal và file."""
    logger = logging.getLogger("Crawler")
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    return logger

log = setup_logging()
setup_signal_handlers()

# ==============================================================================
# TIỆN ÍCH MẠNG & CHROME VERSION
# ==============================================================================

def wait_for_internet(max_wait_minutes=30):
    """Kiểm tra kết nối mạng siêu tốc bằng TCP socket tới 8.8.8.8 hoặc 1.1.1.1 (< 0.05s)."""
    deadline = datetime.now() + timedelta(minutes=max_wait_minutes)

    while datetime.now() < deadline:
        if shutdown_requested:
            return False
        for host in ["8.8.8.8", "1.1.1.1"]:
            try:
                s = socket.create_connection((host, 53), timeout=2)
                s.close()
                return True
            except Exception:
                pass
        log.warning(
            "⚠️  Mất kết nối mạng! Đang chờ Wi-Fi/4G kết nối lại... "
            "(tự động thử lại sau 5 giây)"
        )
        safe_sleep(5)

    log.error(f"❌ Không thể kết nối mạng sau {max_wait_minutes} phút. Dừng lại.")
    return False


def detect_chrome_version():
    """
    Tự động phát hiện phiên bản Chrome chính (major version) trên macOS.
    Giải quyết triệt để lỗi crash do Chrome tự động cập nhật trong nền.
    """
    candidate_paths = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chrome.app/Contents/MacOS/Chrome",
        os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    for path in candidate_paths:
        if os.path.exists(path):
            try:
                output = subprocess.check_output([path, "--version"], stderr=subprocess.STDOUT).decode("utf-8")
                match = re.search(r"Google Chrome\s+(\d+)\.", output)
                if match:
                    major = int(match.group(1))
                    log.info(f"🔍 Phát hiện Google Chrome Version: {major} ({output.strip()})")
                    return major
            except Exception as e:
                log.debug(f"Không lấy được version qua CLI {path}: {e}")

    try:
        output = subprocess.check_output(
            ["defaults", "read", "/Applications/Google Chrome.app/Contents/Info.plist", "CFBundleShortVersionString"],
            stderr=subprocess.STDOUT
        ).decode("utf-8")
        match = re.search(r"^(\d+)\.", output.strip())
        if match:
            major = int(match.group(1))
            log.info(f"🔍 Phát hiện Chrome Version qua Info.plist: {major}")
            return major
    except Exception:
        pass

    log.warning("⚠️ Không thể tự động phát hiện phiên bản Chrome. Dùng fallback (152).")
    return 152


def init_driver():
    """Khởi tạo undetected-chromedriver với version tự động detect & chặn triệt để ảnh/video."""
    options = uc.ChromeOptions()
    options.page_load_strategy = "eager"  # Chỉ đợi DOM sẵn sàng, không treo vì script/ads bên thứ 3
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--mute-audio")
    options.add_argument("--autoplay-policy=user-gesture-required")

    # Tắt load ảnh và media stream
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.default_content_setting_values.media_stream": 2,
    }
    options.add_experimental_option("prefs", prefs)

    chrome_ver = detect_chrome_version()
    try:
        driver = uc.Chrome(options=options, version_main=chrome_ver)
    except Exception as e:
        log.warning(f"Khởi tạo với version_main={chrome_ver} lỗi ({e}). Để UC tự xử lý...")
        driver = uc.Chrome(options=options)

    # Đảm bảo driver luôn gắn chặt vào cửa sổ/tab đang mở
    try:
        if driver.window_handles:
            driver.switch_to.window(driver.window_handles[-1])
    except Exception:
        pass

    # Chặn triệt để luồng tải video (.mp4, .m3u8, .webm, tiktok, youtube...) ở tầng Network
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {
            "urls": ["*.mp4", "*.webm", "*.m3u8", "*.ts", "*googlevideo*", "*tiktok*", "*youtube*", "*vimeo*"]
        })
    except Exception as e:
        log.debug(f"Không thể áp dụng CDP URL filter: {e}")

    driver.set_page_load_timeout(PAGE_LOAD_TIMEOUT)
    log.info("✅ Khởi tạo UC Chrome Driver thành công (Cloudflare Bypass + Chặn ảnh/video).")
    return driver


def safe_restart_driver(driver):
    """Đóng sạch driver cũ và tạo driver mới."""
    try:
        driver.quit()
    except Exception:
        pass
    log.info("🔄 Đang khởi động lại Chrome để giải phóng RAM & làm mới phiên...")
    safe_sleep(3)
    return init_driver()


def wait_for_cloudflare(driver, timeout=None):
    """Chờ Cloudflare Turnstile tự giải xong. Trả về True nếu vượt qua."""
    if timeout is None:
        timeout = CF_WAIT_TIMEOUT

    start = time.time()
    while time.time() - start < timeout:
        if shutdown_requested:
            return False
        title = driver.title
        if "Chờ một chút" not in title and "Just a moment" not in title:
            src = driver.page_source
            if len(src) > 50000 or "re__pr-title" in src or "re__section-body" in src or "re__pr-specs" in src:
                return True
        safe_sleep(1)

    return False

# ==============================================================================
# QUẢN LÝ CHECKPOINT VÀ RETRY TRACKER
# ==============================================================================

def load_harvest_progress():
    """Đọc tiến độ quét link Pha 1 từ harvest_progress.json."""
    if os.path.exists(PROGRESS_PHASE1_FILE):
        try:
            with open(PROGRESS_PHASE1_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Không thể đọc {PROGRESS_PHASE1_FILE}: {e}")
    return {}


def save_harvest_progress(progress):
    """Lưu tiến độ quét link Pha 1 vào harvest_progress.json."""
    try:
        with open(PROGRESS_PHASE1_FILE, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"Không thể ghi {PROGRESS_PHASE1_FILE}: {e}")


def load_cf_tracker():
    """Đọc bộ đếm số lần retry các bài bị Cloudflare chặn."""
    if os.path.exists(CF_TRACKER_FILE):
        try:
            with open(CF_TRACKER_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_cf_tracker(tracker):
    """Lưu bộ đếm retry Cloudflare."""
    try:
        with open(CF_TRACKER_FILE, "w", encoding="utf-8") as f:
            json.dump(tracker, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def record_failed_link(p_id, link, p_type, reason):
    """Ghi nhận bài đăng lỗi vĩnh viễn vào failed_links.csv để theo dõi."""
    file_exists = os.path.exists(FAILED_LINKS_FILE)
    with open(FAILED_LINKS_FILE, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FAILED_FIELDNAMES)
        if not file_exists:
            writer.writeheader()
        writer.writerow({
            "id": p_id,
            "link": link,
            "property_type": p_type,
            "reason": reason,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        })

# ==============================================================================
# PHA 1: THU THẬP LINK (LINK HARVESTER VỚI SMART RESUME)
# ==============================================================================

def load_collected_ids():
    """Đọc danh sách ID đã gom từ links_queue.csv."""
    collected = set()
    if os.path.exists(OUTPUT_LINKS_FILE):
        with open(OUTPUT_LINKS_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                collected.add(str(row["id"]))
    return collected


def harvest_links(driver):
    """
    PHA 1: Thu thập link với cơ chế Smart Resume & Hybrid Refresh:
    - Quét 3 trang đầu để bắt trọn tin mới nhất được đẩy lên top.
    - Nhảy tiếp tục từ vị trí checkpoint của phiên trước đến max_pages.
    """
    log.info("=" * 70)
    log.info("PHA 1: BẮT ĐẦU THU THẬP DANH SÁCH LINK (SMART RESUME)")
    log.info("=" * 70)

    collected_ids = load_collected_ids()
    progress = load_harvest_progress()
    log.info(f"📋 Đã có sẵn {len(collected_ids)} link từ các phiên trước.")

    if not os.path.exists(OUTPUT_LINKS_FILE):
        with open(OUTPUT_LINKS_FILE, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["id", "property_type", "link"])
            writer.writeheader()

    total_new = 0

    for cat in CATEGORIES:
        if shutdown_requested:
            log.info("Đã dừng Pha 1 theo yêu cầu người dùng.")
            break

        p_type = cat["type"]
        label  = cat["label"]
        max_p  = cat["max_pages"]

        cat_prog = progress.get(p_type, {"completed_page": 0, "status": "in_progress"})
        last_page = cat_prog.get("completed_page", 0)
        status = str(cat_prog.get("status", "in_progress")).lower().strip()

        # Lập danh sách trang cần quét:
        # 1. Nếu status là "completed" / "complete": Chỉ quét nhanh 1..10 trang đầu đón tin mới rồi qua phân khúc khác / qua Pha 2
        # 2. Nếu status là "in_progress" / "in_process": Quét 10 trang đầu + cào tiếp từ last_page + 1 cho đến khi tự nhận diện hết tin
        if status in ("completed", "complete"):
            pages_to_crawl = list(range(1, REFRESH_RECENT_PAGES + 1))
            log.info(
                f"\n--- Phân khúc: {label} ({p_type}) | "
                f"Trạng thái: {status} -> Chỉ quét nhanh 1..{REFRESH_RECENT_PAGES} trang đầu đón tin mới rồi chuyển tiếp ---"
            )
        elif last_page > REFRESH_RECENT_PAGES:
            pages_to_crawl = list(range(1, REFRESH_RECENT_PAGES + 1))
            if last_page < max_p:
                pages_to_crawl += list(range(last_page + 1, max_p + 1))
            log.info(
                f"\n--- Phân khúc: {label} ({p_type}) | "
                f"Trạng thái: {status} -> Resume từ trang {last_page + 1}/{max_p} (kèm quét trang 1..{REFRESH_RECENT_PAGES} đón tin mới) ---"
            )
        else:
            pages_to_crawl = list(range(max(1, last_page + 1), max_p + 1))
            log.info(f"\n--- Phân khúc: {label} ({p_type}) | Quét từ trang {pages_to_crawl[0] if pages_to_crawl else 'xong'}/{max_p} ---")

        if not pages_to_crawl:
            log.info(f"Phân khúc {p_type} đã hoàn thành toàn bộ {max_p} trang. Chuyển tiếp.")
            continue

        consecutive_empty = 0
        consecutive_redirect = 0
        consecutive_zero_new = 0

        for page in pages_to_crawl:
            if shutdown_requested:
                break

            if not wait_for_internet():
                log.error("Dừng Pha 1 do mất mạng quá lâu.")
                return

            url = cat["url_pattern"].format(page=page)
            try:
                driver.get(url)
                safe_sleep(random.uniform(*SLEEP_BETWEEN_PAGES))

                if "Chờ một chút" in driver.title or "Just a moment" in driver.title:
                    log.info("  Cloudflare challenge trên trang danh mục, đang chờ giải...")
                    wait_for_cloudflare(driver, timeout=15)

                # 1. Phát hiện Deep Paging Redirect (web tự động redirect về trang 1 hoặc URL khác khi trang quá sâu)
                actual_url = driver.current_url.split("?")[0]
                if page > 1 and f"/p{page}" not in actual_url:
                    consecutive_redirect += 1
                    log.warning(
                        f"[{p_type}] Trang {page}: Bị redirect về {actual_url} "
                        f"({consecutive_redirect}/{REDIRECT_THRESHOLD} lần liên tiếp) -> Có thể đã chạm giới hạn phân trang!"
                    )
                    if consecutive_redirect >= REDIRECT_THRESHOLD:
                        log.info(
                            f"[{p_type}] Đã chạm giới hạn phân trang của website "
                            f"(bị redirect {REDIRECT_THRESHOLD} lần liên tiếp). Chuyển phân khúc."
                        )
                        break
                    continue
                else:
                    consecutive_redirect = 0

                soup = BeautifulSoup(driver.page_source, "lxml")
                cards = soup.select(".js__product-link-for-product-id")

                # 2. Phát hiện trang trống (web không trả về thẻ bài đăng nào)
                if not cards:
                    consecutive_empty += 1
                    log.warning(
                        f"[{p_type}] Trang {page}: không có bài đăng nào "
                        f"({consecutive_empty}/{EMPTY_PAGES_THRESHOLD} trang trống liên tiếp)"
                    )
                    if consecutive_empty >= EMPTY_PAGES_THRESHOLD:
                        log.info(f"[{p_type}] Đã gặp {EMPTY_PAGES_THRESHOLD} trang trống liên tiếp. Chuyển phân khúc.")
                        break
                    continue
                else:
                    consecutive_empty = 0

                new_links = []
                for card in cards:
                    post_id = card.get("data-product-id") or card.get("prid")
                    href = card.get("href")

                    if not href:
                        continue

                    # Nếu post_id bị thiếu hoặc bằng 0, trích xuất trực tiếp từ URL (-pr...)
                    if not post_id or str(post_id).strip() in ("0", ""):
                        m = re.search(r"-pr(\d+)", href)
                        if m:
                            post_id = m.group(1)
                        else:
                            continue

                    if str(post_id) in collected_ids:
                        continue

                    if not href.startswith("http"):
                        href = "https://batdongsan.com.vn" + href

                    # Bỏ các tham số theo dõi quảng cáo (như ?source=ad&adid=...)
                    href = href.split("?")[0]

                    collected_ids.add(str(post_id))
                    new_links.append({
                        "id": post_id,
                        "property_type": p_type,
                        "link": href,
                    })

                if new_links:
                    with open(OUTPUT_LINKS_FILE, "a", encoding="utf-8-sig", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=["id", "property_type", "link"])
                        writer.writerows(new_links)
                    total_new += len(new_links)

                log.info(
                    f"[{p_type}] Trang {page}/{max_p}: "
                    f"+{len(new_links)} mới | Tổng gom: {len(collected_ids)}"
                )

                # 3. Phát hiện chạm trần do lặp lại tin cũ (0 tin mới liên tiếp ở các trang sâu)
                if page > REFRESH_RECENT_PAGES:
                    if len(new_links) == 0:
                        consecutive_zero_new += 1
                        log.warning(
                            f"[{p_type}] Trang {page}: 0 link mới (toàn bộ tin đã gom hoặc web trả về nội dung cũ) "
                            f"({consecutive_zero_new}/{ZERO_NEW_THRESHOLD} trang liên tiếp)"
                        )
                        if consecutive_zero_new >= ZERO_NEW_THRESHOLD:
                            log.info(
                                f"[{p_type}] Đã gặp {ZERO_NEW_THRESHOLD} trang liên tiếp không có tin mới. "
                                f"Đã chạm trần dữ liệu thực tế. Chuyển phân khúc."
                            )
                            break
                    else:
                        consecutive_zero_new = 0

                # Cập nhật checkpoint nếu trang hiện tại tiến xa hơn checkpoint cũ
                if page > last_page:
                    last_page = page
                    progress[p_type] = {
                        "completed_page": last_page,
                        "status": "completed" if last_page >= max_p else "in_progress",
                        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    }
                    save_harvest_progress(progress)

            except Exception as e:
                log.error(f"[{p_type}] Lỗi ở trang {page}: {e}")
                safe_sleep(3)

    log.info(f"\n✅ PHA 1 HOÀN TẤT: Gom được {total_new} link mới. Tổng link trong hàng đợi: {len(collected_ids)}.")

# ==============================================================================
# PHA 2: BÓC TÁCH CHI TIẾT BÀI ĐĂNG (DETAIL SCRAPER)
# ==============================================================================

def is_expired_or_redirect(soup):
    """Phát hiện bài đăng hết hạn, đã xoá, hoặc bị chuyển hướng."""
    expired_keywords = [
        "đã hết hạn", "dừng giao dịch", "không tồn tại",
        "đã bị xóa", "tin đã ẩn", "trang không tồn tại",
    ]
    page_text_lower = soup.get_text(separator=" ", strip=True).lower()
    for kw in expired_keywords:
        if kw in page_text_lower:
            return True

    if "chờ một chút" in page_text_lower or "just a moment" in page_text_lower:
        return True

    has_title = soup.select_one("h1.re__pr-title") or soup.select_one(".js__pr-title")
    has_specs = soup.select(".re__pr-specs-content-item")
    if not has_title and not has_specs:
        return True

    return False


def parse_detail_page(html, link, p_type, p_id):
    """Bóc tách 20 trường dữ liệu bằng BeautifulSoup trong RAM (< 0.05s/bài)."""
    soup = BeautifulSoup(html, "lxml")

    if is_expired_or_redirect(soup):
        return None

    # 1. Tiêu đề
    title_el = soup.select_one("h1.re__pr-title") or soup.select_one(".js__pr-title")
    title = " ".join(title_el.get_text(strip=True).split()) if title_el else "N/A"

    # 2. Mô tả chi tiết (Chuẩn hoá khoảng trắng cho PhoBERT & giữ CSV 1 hàng/bài)
    desc_el = (
        soup.select_one(".re__section-body.re__detail-content")
        or soup.select_one(".js__pr-description")
        or soup.select_one(".re__pr-description")
    )
    description = " ".join(desc_el.get_text(separator=" ", strip=True).split()) if desc_el else "N/A"

    # 3. Giá, Diện tích, Giá/m2 (Thu thập nguyên bản - Raw Data, giữ đúng theo web)
    price, area, price_per_m2 = "N/A", "N/A", "N/A"
    for item in soup.select(".re__pr-short-info-item"):
        t_el = item.select_one(".title")
        v_el = item.select_one(".value")
        if not t_el or not v_el:
            continue
        t_text = t_el.get_text(strip=True)
        if "giá" in t_text.lower():
            price = " ".join(v_el.get_text(strip=True).split())
            ext_el = item.select_one(".ext")
            if ext_el:
                price_per_m2 = " ".join(ext_el.get_text(strip=True).split())
        elif "diện tích" in t_text.lower():
            area = " ".join(v_el.get_text(strip=True).split())

    # 4. Địa chỉ chi tiết (tương đương title3 ở bộ dữ liệu cũ)
    addr_el = (
        soup.select_one("span.re__address")
        or soup.select_one(".re__ldp-address")
    )
    address = " ".join(addr_el.get_text(strip=True).split()) if addr_el else "N/A"

    # 5. Breadcrumb cấp 4 nguyên bản (giữ nguyên tên cột title2 như code cũ để tiền xử lý trích xuất house_type)
    l4_el = soup.select_one('.re__link-se[level="4"]')
    title2 = " ".join(l4_el.get_text(strip=True).split()) if l4_el else "N/A"

    # 6. Quận/Huyện (Breadcrumb level 3) & Tỉnh/Thành phố (Breadcrumb level 2)
    district_el = soup.select_one('.re__link-se[level="3"]')
    district = district_el.get_text(strip=True) if district_el else "N/A"

    # Fallback trích xuất quận/huyện từ địa chỉ nếu breadcrumb thiếu
    if district == "N/A" and address != "N/A":
        parts = [p.strip() for p in address.split(",")]
        if len(parts) >= 2:
            for p in reversed(parts):
                if any(k in p.lower() for k in ["quận", "huyện", "thị xã"]):
                    district = re.sub(r"^(quận|huyện|thị xã)\s+", "", p, flags=re.I).strip()
                    break

    city_el = soup.select_one('.re__link-se[level="2"]')
    city = city_el.get_text(strip=True) if city_el else "Hà Nội"

    # 7. Tên dự án
    prj_el = soup.select_one(".re__project-title")
    project_name = prj_el.get_text(strip=True) if prj_el else "N/A"

    # 8. Thông số kỹ thuật (chuẩn hoá khoảng trắng tránh \n làm vỡ dòng CSV)
    specs = {}
    for item in soup.select(".re__pr-specs-content-item"):
        t_el = item.select_one(".re__pr-specs-content-item-title")
        v_el = item.select_one(".re__pr-specs-content-item-value")
        if t_el and v_el:
            specs[t_el.get_text(strip=True)] = " ".join(v_el.get_text(separator=" ", strip=True).split())

    # 9. Ngày đăng tin
    date_val = "N/A"
    for item in soup.select(".re__pr-short-info-item.js__pr-config-item"):
        t_el = item.select_one(".title")
        v_el = item.select_one(".value")
        if t_el and v_el:
            t_text = t_el.get_text(strip=True)
            if "đăng" in t_text.lower():
                date_val = " ".join(v_el.get_text(strip=True).split())
                break

    return {
        "id":              p_id,
        "property_type":   p_type,
        "title2":          title2,
        "links":           link,
        "title":           title,
        "description":     description,
        "price":           price,
        "price_per_m2":    price_per_m2,
        "area":            area,
        "district":        district,
        "city":            city,
        "address":         address,
        "project_name":    project_name,
        "floors":           specs.get("Số tầng", "N/A"),
        "bedrooms":         specs.get("Số phòng ngủ", "N/A"),
        "toilets":          specs.get("Số phòng tắm, vệ sinh", "N/A"),
        "house_direction":  specs.get("Hướng nhà", "N/A"),
        "balcony":          specs.get("Hướng ban công", "N/A"),
        "street_frontage":  specs.get("Mặt tiền", "N/A"),
        "road_width":       specs.get("Đường vào", "N/A"),
        "legal":            specs.get("Pháp lý", "N/A"),
        "interior":         specs.get("Nội thất", "N/A"),
        "date":             date_val,
    }


def flush_buffer(buffer):
    """Ghi dữ liệu trong RAM xuống file CSV."""
    if not buffer:
        return
    with open(OUTPUT_DATA_FILE, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writerows(buffer)


def scrape_details(driver, limit=None):
    """
    PHA 2: Cào chi tiết từng bài với Exponential Backoff & Cloudflare Tracker.
    """
    log.info("=" * 70)
    log.info("PHA 2: BẮT ĐẦU CÀO CHI TIẾT BÀI ĐĂNG (FULL DESCRIPTION & SPECS)")
    log.info("=" * 70)

    if not os.path.exists(OUTPUT_LINKS_FILE):
        log.error("❌ Chưa có file links_queue.csv! Hãy chạy Pha 1 trước.")
        return

    items_to_crawl = []
    with open(OUTPUT_LINKS_FILE, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            items_to_crawl.append(row)

    total_queue = len(items_to_crawl)
    log.info(f"📋 Tổng số link trong hàng đợi: {total_queue}")

    # Đọc checkpoint từ file output chính
    crawled_ids = set()
    if os.path.exists(OUTPUT_DATA_FILE):
        with open(OUTPUT_DATA_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                crawled_ids.add(str(row["id"]))
        log.info(f"✅ Checkpoint: Đã có {len(crawled_ids)} bài hoàn tất trong {os.path.basename(OUTPUT_DATA_FILE)}.")
    else:
        with open(OUTPUT_DATA_FILE, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
            writer.writeheader()

    # Đọc thêm danh sách các bài lỗi vĩnh viễn (đã quá số lần retry)
    if os.path.exists(FAILED_LINKS_FILE):
        with open(FAILED_LINKS_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                crawled_ids.add(str(row["id"]))

    remaining = total_queue - len(crawled_ids)
    log.info(f"📊 Còn lại cần cào: {remaining} bài")

    cf_tracker = load_cf_tracker()

    stats = {
        "success": 0,
        "expired": 0,
        "errors": 0,
        "cf_blocked": 0,
        "no_description": 0,
        "start_time": datetime.now(),
    }

    buffer = []
    items_since_restart = 0
    consecutive_errors = 0

    for idx, item in enumerate(items_to_crawl):
        if shutdown_requested:
            log.info("🛑 Đang dừng cào Pha 2 theo yêu cầu...")
            break

        p_id   = str(item["id"])
        link   = item["link"]
        p_type = item["property_type"]

        if p_id in crawled_ids:
            continue

        if not wait_for_internet():
            log.error("Dừng Pha 2 do mất mạng quá lâu.")
            flush_buffer(buffer)
            break

        try:
            driver.get(link)
            safe_sleep(random.uniform(*SLEEP_BETWEEN_ITEMS))

            # Xử lý Cloudflare Turnstile
            if "Chờ một chút" in driver.title or "Just a moment" in driver.title:
                cf_ok = wait_for_cloudflare(driver)
                if not cf_ok:
                    stats["cf_blocked"] += 1
                    retries = cf_tracker.get(p_id, 0) + 1
                    cf_tracker[p_id] = retries
                    save_cf_tracker(cf_tracker)

                    if retries < MAX_CF_RETRIES:
                        log.warning(
                            f"⛔ Bài {p_id}: Cloudflare không tự giải (thử lần {retries}/{MAX_CF_RETRIES}). "
                            f"Tạm bỏ qua phiên này để thử lại sau."
                        )
                        # KHÔNG thêm p_id vào crawled_ids để có cơ hội cào lại
                    else:
                        log.error(
                            f"❌ Bài {p_id}: Đã thử {MAX_CF_RETRIES} lần vẫn bị Cloudflare chặn. "
                            f"Lưu vào failed_links.csv và bỏ qua vĩnh viễn."
                        )
                        record_failed_link(p_id, link, p_type, "CF_BLOCKED_EXCEEDED_MAX_RETRIES")
                        crawled_ids.add(p_id)

                    consecutive_errors += 1
                    # Kích hoạt Exponential Backoff nếu bị chặn liên tiếp
                    if consecutive_errors == 3:
                        log.warning("⚠️ 3 lỗi liên tiếp. Nghỉ 15 giây...")
                        safe_sleep(15)
                    elif consecutive_errors == 5:
                        log.warning("⚠️ 5 lỗi liên tiếp. Nghỉ 60 giây và khởi động lại Chrome...")
                        flush_buffer(buffer)
                        buffer = []
                        safe_sleep(60)
                        driver = safe_restart_driver(driver)
                        items_since_restart = 0
                    elif consecutive_errors >= 8:
                        log.warning("⛔ Gặp nhiều lỗi liên tiếp (dấu hiệu rate-limit). Nghỉ 5 phút (300s)...")
                        flush_buffer(buffer)
                        buffer = []
                        safe_sleep(300)
                        driver = safe_restart_driver(driver)
                        items_since_restart = 0
                        if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                            consecutive_errors = 0
                    continue

            # Bóc tách trang chi tiết
            record = parse_detail_page(driver.page_source, link, p_type, p_id)

            if record is None:
                stats["expired"] += 1
                log.debug(f"⏭️  Bài {p_id}: Hết hạn/Redirect -> Bỏ qua.")
                crawled_ids.add(p_id)
                consecutive_errors = 0
                continue

            if record["description"] == "N/A" or len(record["description"].strip()) < 10:
                stats["no_description"] += 1
                log.debug(f"⚠️  Bài {p_id}: Không lấy được description.")

            buffer.append(record)
            crawled_ids.add(p_id)
            stats["success"] += 1
            items_since_restart += 1
            consecutive_errors = 0

            # Hiển thị tiến độ rõ ràng
            done = len(crawled_ids)
            pct = (done / total_queue) * 100
            log.info(
                f"[{done}/{total_queue} ({pct:.1f}%)] "
                f"ID={p_id} | {record['property_type']} | {record['district']} | "
                f"Giá: {record['price']} | DT: {record['area']}"
            )

            if limit and stats["success"] >= limit:
                log.info(f"🧪 ĐÃ HOÀN THÀNH GIỚI HẠN TEST ({limit} bài thành công). Dừng phiên test để kiểm tra.")
                break

        except Exception as e:
            if shutdown_requested:
                break
            stats["errors"] += 1
            consecutive_errors += 1
            log.error(f"❌ Lỗi bài {p_id} ({link[:60]}...): {e}")

            if consecutive_errors >= 3 and consecutive_errors < 5:
                log.warning("⚠️ 3 lỗi liên tiếp. Nghỉ 15 giây...")
                safe_sleep(15)
            elif consecutive_errors >= 5:
                log.warning("⚠️ Lỗi liên tiếp. Nghỉ 60 giây và khởi động lại Chrome...")
                flush_buffer(buffer)
                buffer = []
                safe_sleep(60)
                driver = safe_restart_driver(driver)
                items_since_restart = 0
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    consecutive_errors = 0

            # Nếu gặp lỗi renderer timeout hoặc mất kết nối: Khởi động lại Chrome ngay lập tức để giải phóng RAM
            err_msg = str(e).lower()
            if any(k in err_msg for k in ["renderer", "disconnected", "no such window", "web view not found"]):
                log.warning(f"⚠️ Phát hiện renderer đơ hoặc mất kết nối Chrome. Khởi động lại ngay...")
                flush_buffer(buffer)
                buffer = []
                driver = safe_restart_driver(driver)
                items_since_restart = 0
            elif "timeout" in err_msg:
                try:
                    if driver.window_handles:
                        driver.switch_to.window(driver.window_handles[-1])
                    else:
                        driver = safe_restart_driver(driver)
                        items_since_restart = 0
                except Exception:
                    driver = safe_restart_driver(driver)
                    items_since_restart = 0

        # Ghi checkpoint định kỳ xuống đĩa
        if len(buffer) >= BATCH_FLUSH_SIZE:
            flush_buffer(buffer)
            buffer = []
            log.info(f"💾 Checkpoint: Đã lưu {stats['success']} bài mới vào {os.path.basename(OUTPUT_DATA_FILE)}.")

        # Khởi động lại Chrome định kỳ để giải phóng RAM
        if items_since_restart >= DRIVER_RESTART_EVERY:
            flush_buffer(buffer)
            buffer = []
            driver = safe_restart_driver(driver)
            items_since_restart = 0

    # Lưu nốt dữ liệu còn lại trong RAM
    flush_buffer(buffer)

    # Tự động cập nhật file Excel (.xlsx) để người dùng mở trực tiếp không bao giờ lo lỗi format
    try:
        from export_to_excel import convert_csv_to_xlsx
        log.info("📊 Đang tự động đồng bộ sang file Excel (.xlsx)...")
        convert_csv_to_xlsx()
    except Exception as e:
        log.debug(f"Không thể tự động xuất sang XLSX: {e}")

    if shutdown_requested:
        log.info("\n💾 Đã lưu dữ liệu checkpoint an toàn trước khi dừng.")
    else:
        log.info("\n" + "=" * 70)
        log.info("✅ PHA 2 HOÀN TẤT!")

    print_session_stats(stats)

# ==============================================================================
# THỐNG KÊ TỔNG KẾT
# ==============================================================================

def print_session_stats(stats):
    """In bảng thống kê tổng kết chi tiết."""
    elapsed = datetime.now() - stats["start_time"]
    hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    total_processed = stats["success"] + stats["expired"] + stats["errors"] + stats["cf_blocked"]
    speed = stats["success"] / max(elapsed.total_seconds(), 1) * 3600

    log.info("=" * 70)
    log.info("📊 THỐNG KÊ PHIÊN CÀO")
    log.info("=" * 70)
    log.info(f"  ⏱️  Thời gian chạy     : {hours}h {minutes}m {seconds}s")
    log.info(f"  📄 Tổng đã xử lý       : {total_processed} bài")
    log.info(f"  ✅ Cào thành công       : {stats['success']} bài")
    log.info(f"  ⏭️  Hết hạn/Redirect    : {stats['expired']} bài")
    log.info(f"  ⛔ Cloudflare chặn      : {stats['cf_blocked']} bài")
    log.info(f"  ❌ Gặp lỗi             : {stats['errors']} bài")
    log.info(f"  ⚠️  Thiếu description   : {stats['no_description']} bài")
    log.info(f"  🚀 Tốc độ trung bình   : ~{speed:.0f} bài/giờ")
    log.info("=" * 70)

    if os.path.exists(OUTPUT_DATA_FILE):
        with open(OUTPUT_DATA_FILE, "r", encoding="utf-8-sig") as f:
            total_rows = sum(1 for _ in csv.reader(f)) - 1
        file_size_mb = os.path.getsize(OUTPUT_DATA_FILE) / (1024 * 1024)
        log.info(f"  📁 File output chính   : {OUTPUT_DATA_FILE}")
        log.info(f"     Tổng số bài hiện có : {total_rows}")
        log.info(f"     Dung lượng file     : {file_size_mb:.2f} MB")

    if os.path.exists(FAILED_LINKS_FILE):
        with open(FAILED_LINKS_FILE, "r", encoding="utf-8-sig") as f:
            failed_count = sum(1 for _ in csv.reader(f)) - 1
        log.info(f"  ⚠️  File bài lỗi (failed): {FAILED_LINKS_FILE} ({failed_count} bài)")

    log.info("=" * 70)

# ==============================================================================
# HÀM ĐIỀU PHỐI CHÍNH
# ==============================================================================

def main():
    """Hàm main điều phối 2 pha của Crawler."""
    log.info("🏠 CRAWLER BẤT ĐỘNG SẢN HÀ NỘI 2026 (PRODUCTION)")
    log.info(f"📅 Khởi động lúc   : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"📁 Thư mục làm việc: {WORK_DIR}")
    log.info("💡 Mẹo: Bấm Ctrl+C bất kỳ lúc nào để dừng an toàn mà không mất dữ liệu.")
    log.info("")

    if not wait_for_internet():
        log.error("Không có kết nối mạng. Vui lòng kiểm tra Wi-Fi/4G rồi chạy lại.")
        sys.exit(1)

    driver = init_driver()

    limit = None
    phase = None
    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            try:
                limit = int(sys.argv[i + 1])
            except ValueError:
                pass
        elif arg.startswith("--limit="):
            try:
                limit = int(arg.split("=")[1])
            except ValueError:
                pass
        elif arg in ("--phase", "-p") and i + 1 < len(sys.argv):
            try:
                phase = int(sys.argv[i + 1])
            except ValueError:
                pass
        elif arg.startswith("--phase="):
            try:
                phase = int(arg.split("=")[1])
            except ValueError:
                pass

    if limit:
        log.info(f"🎯 Kích hoạt chế độ TEST với --limit={limit}")
    if phase:
        log.info(f"🎯 Chỉ định chạy riêng PHA {phase}")

    try:
        # Pha 1: Thu thập link (nếu không chỉ định hoặc chọn phase 1)
        if phase in (None, 1):
            harvest_links(driver)

        # Pha 2: Cào chi tiết (nếu không chỉ định hoặc chọn phase 2)
        if not shutdown_requested and phase in (None, 2):
            scrape_details(driver, limit=limit)

    except KeyboardInterrupt:
        log.info("\n🛑 Đã bắt được Ctrl+C ở cấp độ ngoài cùng.")
    except Exception as e:
        log.error(f"Lỗi không mong đợi trong main: {e}", exc_info=True)
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        log.info(f"🏁 Phiên làm việc kết thúc lúc: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
