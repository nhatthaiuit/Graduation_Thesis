import os
import re
import sys
import shutil
import argparse
import logging
import unicodedata
from datetime import datetime
import numpy as np
import pandas as pd

# Cố gắng import rapidfuzz nếu có, nếu không thì fallback về difflib chuẩn của Python
try:
    from rapidfuzz import fuzz
    HAS_RAPIDFUZZ = True
except ImportError:
    import difflib
    HAS_RAPIDFUZZ = False

# =============================================================================
# CẤU HÌNH & TỪ ĐIỂN CHUẨN HÓA (BUILT-IN CONFIGURATIONS)
# =============================================================================

# Nhóm 4 phân vùng địa lý theo Proposal (Geographic Split)
DISTRICT_GROUPS = {
    "central": ["Hoàn Kiếm", "Ba Đình", "Đống Đa", "Hai Bà Trưng"],
    "near_central": ["Cầu Giấy", "Thanh Xuân", "Tây Hồ", "Long Biên", "Hoàng Mai"],
    "new_urban": ["Nam Từ Liêm", "Bắc Từ Liêm", "Hà Đông", "Gia Lâm", "Thanh Trì"],
    "suburban": [
        "Đông Anh", "Hoài Đức", "Sóc Sơn", "Mê Linh", "Thạch Thất",
        "Quốc Oai", "Chương Mỹ", "Thanh Oai", "Đan Phượng", "Phúc Thọ",
        "Ba Vì", "Ứng Hòa", "Mỹ Đức", "Thường Tín", "Phú Xuyên", "Sơn Tây"
    ]
}

# Từ điển viết tắt thuật ngữ Bất động sản theo proposal
DOMAIN_DICT = {
    # --- Pháp lý ---
    "sđcc": "sổ đỏ chính chủ",
    "sdcc": "sổ đỏ chính chủ",
    "shcc": "sổ hồng chính chủ",
    "hdmb": "hợp đồng mua bán",
    "hđmb": "hợp đồng mua bán",
    "gtcl": "giấy tờ có liên quan",
    # --- Giao thông ---
    "hxh": "hẻm xe hơi",
    "hxt": "hẻm xe tải",
    "hxb": "hẻm xe ba gác",
    "oto": "ô tô",
    "otô": "ô tô",
    # --- Phòng ốc ---
    "pn": "phòng ngủ",
    "wc": "vệ sinh",
    "vs": "vệ sinh",
    # --- Bất động sản ---
    "dt": "diện tích",
    "dtsd": "diện tích sử dụng",
    "mt": "mặt tiền",
    "kdt": "khu đô thị",
    "cccc": "chung cư cao cấp",
    "cc": "chung cư",
    "bt": "biệt thự",
    "lk": "liền kề",
    "sh": "sổ hồng",
    "sđ": "sổ đỏ",
    "sd": "sổ đỏ",
    "mpt": "mặt phố thương mại",
    "mpkd": "mặt phố kinh doanh",
    # --- Nội thất ---
    "nt": "nội thất",
    "ntcc": "nội thất cao cấp",
    "ntdd": "nội thất đầy đủ",
    "full nt": "full nội thất",
    "htcb": "hoàn thiện cơ bản",
    # --- Kinh doanh ---
    "kd": "kinh doanh",
    "vp": "văn phòng",
    "cđt": "chủ đầu tư",
    "cdt": "chủ đầu tư",
    "ck": "chiết khấu",
    "tl": "thương lượng",
    # --- Tiện ích ---
    "pccc": "phòng cháy chữa cháy",
    "tttm": "trung tâm thương mại",
    "bv": "bệnh viện",
    "đh": "đại học",
    "th": "trường học",
    "nk": "nội khu",
    # --- Hướng ---
    "đn": "đông nam",
    "tb": "tây bắc",
    "đb": "đông bắc",
    "tn": "tây nam",
}

# Regex tách viết tắt dạng dính liền: "3pn" → "3 phòng ngủ", "2wc" → "2 vệ sinh"
_RE_NUMERIC_ABBREV = re.compile(
    r"(\d+)\s*(pn|wc|vs|tầng|t)\b", re.IGNORECASE
)
_NUMERIC_ABBREV_MAP = {
    "pn": "phòng ngủ", "wc": "vệ sinh", "vs": "vệ sinh", "tầng": "tầng", "t": "tầng",
}

# =============================================================================
# LOGGING SETUP
# =============================================================================

def setup_logger():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S"
    )
    return logging.getLogger("DataCleaner")

logger = setup_logger()

# =============================================================================
# CÁC HÀM XỬ LÝ SỐ HỌC (NUMERIC PARSERS)
# =============================================================================

def parse_price_vnd(price_str) -> float:
    """
    Parse chuỗi giá về đơn vị VND:
    - '8,6 tỷ'   -> 8_600_000_000.0
    - '840 triệu' -> 840_000_000.0
    - 'Thỏa thuận' -> np.nan
    """
    if pd.isna(price_str):
        return np.nan
    s = str(price_str).strip()
    if "thuận" in s.lower() or "thoa thuan" in s.lower():
        return np.nan
    
    s = s.replace("~", "").strip()

    # Tỷ
    m = re.match(r"^([\d,.]+)\s*tỷ", s, re.IGNORECASE)
    if m:
        num_str = m.group(1).replace(".", "").replace(",", ".")
        try:
            return float(num_str) * 1_000_000_000.0
        except ValueError:
            return np.nan

    # Triệu (chú ý không nhầm với triệu/m²)
    m = re.match(r"^([\d,.]+)\s*triệu(?!/)", s, re.IGNORECASE)
    if m:
        num_str = m.group(1).replace(".", "").replace(",", ".")
        try:
            return float(num_str) * 1_000_000.0
        except ValueError:
            return np.nan

    return np.nan


def parse_price_per_m2(val) -> float:
    """
    Parse đơn vị triệu/m² (chỉ dùng để cross-validation, không làm feature).
    '~91,49 triệu/m²' -> 91.49
    """
    if pd.isna(val):
        return np.nan
    s = str(val).strip().replace("~", "").strip()
    if "thuận" in s.lower():
        return np.nan

    # triệu/m²
    m = re.match(r"^([\d,.]+)\s*triệu/m", s, re.IGNORECASE)
    if m:
        num_str = m.group(1).replace(".", "").replace(",", ".")
        try:
            return float(num_str)
        except ValueError:
            return np.nan

    # tỷ/m² hoặc tỷ (edge case)
    m = re.match(r"^([\d,.]+)\s*tỷ", s, re.IGNORECASE)
    if m:
        num_str = m.group(1).replace(".", "").replace(",", ".")
        try:
            return float(num_str) * 1000.0
        except ValueError:
            return np.nan

    return np.nan


def parse_area(val) -> float:
    """
    Parse diện tích sang float (m²).
    Xử lý đặc thù tiếng Việt:
      - '94 m²' -> 94.0
      - '75,6 m²' -> 75.6
      - '1.000 m²' -> 1000.0 (dấu chấm hàng nghìn)
      - '1.188 m²' -> 1188.0
      - '1.001,8 m²' -> 1001.8
    """
    if pd.isna(val) or str(val).strip() == "N/A":
        return np.nan
    s = str(val).strip().replace("m²", "").replace("m2", "").strip()
    if not s:
        return np.nan

    # Nhận diện dấu chấm phân cách hàng nghìn (ví dụ 1.000 hoặc 1.188 hoặc 1.001,8)
    if re.search(r"^\d{1,3}\.\d{3}(?:,\d+)?$", s):
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")

    try:
        return float(s)
    except ValueError:
        return np.nan


def parse_rooms(val) -> float:
    """'3 phòng' -> 3.0"""
    if pd.isna(val):
        return np.nan
    s = str(val).strip().replace("phòng", "").strip()
    try:
        return float(s)
    except ValueError:
        return np.nan


def parse_floors(val) -> float:
    """'5 tầng' -> 5.0"""
    if pd.isna(val):
        return np.nan
    s = str(val).strip().replace("tầng", "").strip()
    try:
        return float(s)
    except ValueError:
        return np.nan


def parse_measurement(val) -> float:
    """'4,5 m' -> 4.5"""
    if pd.isna(val):
        return np.nan
    s = str(val).strip().replace("m", "").strip()
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return np.nan


# =============================================================================
# CÁC HÀM CHUẨN HÓA CATEGORICAL (CATEGORICAL NORMALIZERS)
# =============================================================================

def normalize_legal(val) -> str:
    """
    Chuẩn hóa unique values của legal về 5 nhóm chuẩn.
    Nhóm: Sổ đỏ/Sổ hồng | HĐMB | Đang chờ sổ | Sở hữu lâu dài | Không rõ.
    - NaN / bỏ trống → "Không rõ".
    - Các biến thể cá biệt (vi bằng, SPA, lỗi nhập liệu) → gộp vào "Không rõ".
    """
    if pd.isna(val):
        return "Không rõ"
    # Strip Unicode no-break space + normalize NFC + dấu chấm cuối + whitespace
    s = unicodedata.normalize("NFC", str(val)).replace("\u00a0", " ").strip().rstrip(".").strip().lower()

    # 1. Sổ đỏ / Sổ hồng (bao gồm các biến thể và typo)
    if any(kw in s for kw in [
        "sổ đỏ", "sổ hồng", "có sổ", "sẵn sổ", "đã có sổ",
        "sổ vuông", "sổ đẹp", "sổ chuẩn", "sổ cầm tay", "sổ cất két",
        "sổ pháp lý", "sổ sang tên",
        "số đỏ",  # typo phổ biến: "Số" thay vì "Sổ"
        "sđcc", "shcc", "sdcc",
        "pháp lý đầy đủ", "pháp lý rõ ràng", "pháp lý hoàn chỉnh",
        "pháp lý minh bạch", "đầy đủ", "đẩy đủ",
    ]):
        return "Sổ đỏ/Sổ hồng"

    # 2. HĐMB (gồm ký quỹ, đặt cọc, hợp đồng thuê, dịch vụ)
    if any(kw in s for kw in [
        "hợp đồng mua bán", "hđmb", "hdmb",
        "hợp đồng đặt cọc", "hợp đồng ký quỹ", "hợp đồng thuê",
        "hợp đồng dịch vụ", "hợp đồng bảo đảm", "hợp đồng vay",
        "thỏa thuận ký quỹ", "thỏa thuận ký qũy",
        "văn bản ký quỹ", "ttkq",
    ]):
        return "HĐMB"

    # 3. Đang chờ sổ
    if any(kw in s for kw in [
        "chờ sổ", "đang chờ", "chưa sổ", "chờ ký hợp đồng",
        "chuần bị ký",  # typo thực tế
    ]):
        return "Đang chờ sổ"

    # 4. Sở hữu lâu dài
    if "lâu dài" in s or "sở hữu vĩnh viễn" in s:
        return "Sở hữu lâu dài"

    # 5. Còn lại (vi bằng, SPA, lỗi nhập liệu, ...) → gộp vào "Không rõ"
    return "Không rõ"


def normalize_interior(val) -> str:
    """
    Chuẩn hóa unique values của interior về 5 nhóm chuẩn.
    Nhóm: Cao cấp | Đầy đủ | Cơ bản | Không nội thất | Không rõ.
    - NaN / bỏ trống → "Không rõ".
    - Các mô tả cá biệt không phân loại được → gộp vào "Không rõ".
    Thứ tự match: Cao cấp → Không nội thất → Cơ bản → Đầy đủ → fallback heuristic → Không rõ.
    (Cao cấp check trước vì "full nội thất cao cấp" phải là Cao cấp, không phải Đầy đủ)
    """
    if pd.isna(val):
        return "Không rõ"
    s = unicodedata.normalize("NFC", str(val)).replace("\u00a0", " ").strip().rstrip(".").strip().lower()

    # 1. Cao cấp (check trước để "full nội thất cao cấp" → Cao cấp)
    if any(kw in s for kw in [
        "cao cấp", "luxury", "premium", "5*", "5 sao", "tiêu chuẩn 5",
        "sang trọng", "nhập khẩu", "vip",
    ]):
        return "Cao cấp"

    # 2. Không nội thất / Thô (check trước Đầy đủ để "nhà thô" không match "nhà"→Đầy đủ)
    if any(kw in s for kw in [
        "không nội thất", "nội thất trống", "không có nội thất", "trống",
        "bàn giao thô", "nguyên bản",
    ]):
        return "Không nội thất"
    # Pattern "thô" riêng — chỉ match khi CHÍNH XÁC là "thô" hoặc bắt đầu bằng "thô"/"xây thô"/"nhà thô"
    if s == "thô" or s.startswith("xây thô") or s.startswith("nhà thô") or "mặt ngoài" in s:
        return "Không nội thất"

    # 3. Cơ bản (bao gồm "liền tường" khi KHÔNG có "cao cấp" — đã loại ở bước 1)
    if any(kw in s for kw in ["cơ bản", "liền tường"]):
        return "Cơ bản"

    # 4. Đầy đủ (mở rộng: nội thất đẹp, xịn, hiện đại, full NT, ...)
    if any(kw in s for kw in [
        "đầy đủ", "full nội thất", "full đồ", "full nt",
        "nội thất đầy đủ", "đầy đủ nội thất", "đủ nội thất",
        "nội thất đẹp", "nội thất xịn", "nội thất hiện đại", "nội thất mới",
        "nội thất phong cách", "nội thất siêu", "nội thất full",
        "sẵn nội thất", "có nội thất", "tặng nội thất", "để lại",
        "hoàn thiện nội thất", "bàn giao full", "bàn giao nội thất",
        "đủ đồ", "trang bị",
        "nôi thất",     # typo phổ biến: "nôi" thay vì "nội"
        "nội thấ",      # cắt cụt: "nội thất" → "nội thấ"
        "full hết",     # "full hết NT", "full hết nội thất"
        "full mới",     # "full mới"
        "tặng nt",      # viết tắt: "tặng nội thất"
    ]) or s in ("full", "đầy đủ đồ", "đủ", "có"):
        return "Đầy đủ"

    # 5. Fallback heuristic: chứa "nội thất" hoặc liệt kê ≥ 2 đồ nội thất cụ thể → Đầy đủ
    _FURNITURE_KW = [
        "điều hòa", "điều hoà", "tủ lạnh", "máy giặt", "giường", "sofa",
        "tủ bếp", "bếp từ", "nóng lạnh", "sàn gỗ", "trần thạch cao",
        "tivi", "thiết bị vệ sinh", "tủ quần áo", "lò vi sóng", "hút mùi",
    ]
    if "nội thất" in s or sum(1 for kw in _FURNITURE_KW if kw in s) >= 2:
        return "Đầy đủ"

    # 6. "Hoàn thiện" (standalone, không kèm "mặt ngoài" — đã loại ở bước 2) → Cơ bản
    if "hoàn thiện" in s:
        return "Cơ bản"

    # 7. "Xịn"/"Quá xịn" → Cao cấp
    if any(kw in s for kw in ["xịn", "quá đẹp"]):
        return "Cao cấp"

    # 8. "Đẹp"/"Hiện đại" standalone (mô tả chất lượng nội thất chung) → Đầy đủ
    if any(kw in s for kw in ["đẹp", "hiện đại"]):
        return "Đầy đủ"

    # 9. Còn lại → gộp vào "Không rõ"
    return "Không rõ"


_HOUSE_TYPE_MAP = {
    "chung cư": "Căn hộ chung cư",
    "biệt thự": "Biệt thự/Liền kề",
    "liền kề": "Biệt thự/Liền kề",
    "mặt phố": "Nhà mặt phố",
    "phố thương mại": "Biệt thự/Liền kề",  # Shophouse gộp vào Biệt thự/Liền kề
    "shophouse": "Biệt thự/Liền kề",        # Shophouse gộp vào Biệt thự/Liền kề
    "riêng": "Nhà riêng",
    "đất": "Đất nền",  # Sẽ bị drop ở Phase 1 (đất nền ≠ nhà ở)
}

def extract_house_type(title2) -> str:
    """Trích xuất house_type từ title2 (phần trước 'tại')."""
    if pd.isna(title2):
        return "Không rõ"
    parts = str(title2).split(" tại ", maxsplit=1)
    raw = parts[0].strip().lower()
    for kw, label in _HOUSE_TYPE_MAP.items():
        if kw in raw:
            return label
    return "Không rõ"


_WARD_PATTERN = re.compile(r"(?:Phường|Xã|Thị trấn)\s+([^,()]+)", re.IGNORECASE)

def extract_ward(address) -> str:
    """Trích xuất Phường/Xã từ chuỗi address."""
    if pd.isna(address):
        return "Không rõ"
    m = _WARD_PATTERN.search(str(address))
    if m:
        return m.group(1).strip()
    return "Không rõ"


def get_geo_group(district_name: str) -> str:
    """Ánh xạ quận/huyện vào 4 phân vùng địa lý (Proposal §94)."""
    if pd.isna(district_name):
        return "unknown"
    d_clean = str(district_name).replace("Quận", "").replace("Huyện", "").replace("Thị xã", "").strip()
    for group, dists in DISTRICT_GROUPS.items():
        for d in dists:
            if d.lower() == d_clean.lower() or d.lower() in d_clean.lower():
                return group
    logger.warning(f"Quận/Huyện không tìm thấy trong DISTRICT_GROUPS: '{district_name}' → gán 'unknown'")
    return "unknown"


# =============================================================================
# LEAKAGE-AWARE TEXT PREPROCESSING (PROPOSAL)
# =============================================================================

class LeakageMasker:
    """
    Thực hiện che dấu rò rỉ thông tin mục tiêu (Target Leakage Masking)
    theo 7 phân loại trong Proposal:
    1. [PRICE]
    2. [UNIT_PRICE]
    3. [AREA]
    4. [ADDRESS]
    5. [PROJECT]
    6. [CONTACT]
    7. [PROMOTION]
    """
    def __init__(self, domain_dict=DOMAIN_DICT):
        self.domain_dict = domain_dict
        
        # Regex mẫu
        self.re_unit_price = re.compile(
            r"\b\d+[,.]?\d*\s*(?:triệu/m²|triệu/m2|tr/m²|tr/m2|tỷ/m²|tỷ/m2)\b", re.IGNORECASE
        )
        self.re_price = re.compile(
            r"\b\d+[,.]?\d*\s*(?:tỷ|ty|triệu|trieu|tr)\b(?!\s*/\s*m)", re.IGNORECASE
        )
        self.re_area = re.compile(
            r"\b\d+[,.]?\d*\s*(?:m²|m2|mét vuông|met vuong)\b", re.IGNORECASE
        )
        self.re_contact = re.compile(
            r"(?:0\d{1,3}[\s.]?\d{3}[\s.]?[\d*]{3,4}|lh\s*[:\.]?\s*\d+|liên hệ\s*[:\.]?\s*[\w\s\d]+(?=\.|\n|$))",
            re.IGNORECASE
        )
        self.re_promotion = re.compile(
            r"(?:cắt lỗ|ngộp thở|ngộp bank|bán gấp|giá sốc|giá hời|chính chủ cần tiền|hạ giá|thanh lý)",
            re.IGNORECASE
        )
        self.re_ward_district = re.compile(
            r"(?:Phường|Xã|Thị trấn|Quận|Huyện)\s+[\w\s]+?(?=[,.\n]|$)", re.IGNORECASE
        )

    def expand_domain_abbreviations(self, text: str) -> str:
        """
        Mở rộng từ viết tắt BĐS để chuẩn hóa ngữ nghĩa cho PhoBERT.
        Xử lý 2 loại:
          1. Token đơn: "sđcc" → "sổ đỏ chính chủ"
          2. Dạng dính số: "3pn" → "3 phòng ngủ", "2wc" → "2 vệ sinh"
        """
        # Bước 1: Tách viết tắt dính liền với số
        def _expand_numeric(m):
            num = m.group(1)
            abbr = m.group(2).lower()
            expanded = _NUMERIC_ABBREV_MAP.get(abbr, abbr)
            return f"{num} {expanded}"
        text = _RE_NUMERIC_ABBREV.sub(_expand_numeric, text)

        # Bước 2: Expand token đơn
        tokens = text.split()
        expanded_tokens = []
        for token in tokens:
            lower_token = token.lower().strip(".,;:!?()[]")
            if lower_token in self.domain_dict:
                expanded = self.domain_dict[lower_token]
                expanded_tokens.append(expanded)
            else:
                expanded_tokens.append(token)
        return " ".join(expanded_tokens)

    def mask_text(self, text: str, project_name: str = "", address: str = "", setting: str = "full") -> str:
        """
        Masking văn bản theo các thiết lập Ablation Study:
        - raw: Không mask
        - numeric: Chỉ mask số price và area
        - price_area: Mask price, unit price và area
        - full: Mask toàn bộ 7 category
        """
        if pd.isna(text) or not str(text).strip():
            return ""
        
        t = str(text)
        
        if setting == "raw":
            return t

        # Bước 1: Mở rộng viết tắt trước
        t = self.expand_domain_abbreviations(t)

        if setting == "numeric":
            # Chỉ mask giá tổng và diện tích (KHÔNG mask đơn giá/m²)
            t = self.re_price.sub("[PRICE]", t)
            t = self.re_area.sub("[AREA]", t)
            return t

        if setting == "price_area":
            t = self.re_unit_price.sub("[UNIT_PRICE]", t)
            t = self.re_price.sub("[PRICE]", t)
            t = self.re_area.sub("[AREA]", t)
            return t

        # setting == 'full'
        # 1. Project name mask
        if pd.notna(project_name) and str(project_name).strip() and str(project_name).strip().lower() != "n/a":
            p_name = re.escape(str(project_name).strip())
            t = re.sub(p_name, "[PROJECT]", t, flags=re.IGNORECASE)

        # 2. Price per m2 & Price
        t = self.re_unit_price.sub("[UNIT_PRICE]", t)
        t = self.re_price.sub("[PRICE]", t)

        # 3. Area
        t = self.re_area.sub("[AREA]", t)

        # 4. Contact
        t = self.re_contact.sub("[CONTACT]", t)

        # 5. Promotion
        t = self.re_promotion.sub("[PROMOTION]", t)

        # 6. Address / Ward
        t = self.re_ward_district.sub("[ADDRESS]", t)

        return t


# =============================================================================
# NEAR-DUPLICATE DETECTION (PROPOSAL)
# =============================================================================

def compute_similarity(s1: str, s2: str) -> float:
    """Tính độ tương đồng giữa 2 chuỗi title."""
    if HAS_RAPIDFUZZ:
        return fuzz.ratio(s1, s2) / 100.0
    else:
        return difflib.SequenceMatcher(None, s1, s2).ratio()


def flag_duplicates(df: pd.DataFrame, similarity_threshold: float = 0.90) -> pd.Series:
    """
    Xác định tin đăng gần trùng (Near-Duplicate) dựa trên exact title
    và fuzzy similarity trong cùng một quận + phân loại BĐS (tối ưu tốc độ).
    Trả về cờ boolean pd.Series (True = trùng).
    """
    # 1. Exact match trên title + property_type + district
    exact_dup = df.duplicated(subset=["title", "property_type", "district"], keep="first")
    dup_indices = set(df[exact_dup].index)

    # 2. Fast Fuzzy match: Sắp xếp title trong từng nhóm để các tin tương đồng nằm liền kề
    groups = df.groupby(["district", "property_type"])
    for (dist, ptype), grp in groups:
        if len(grp) < 2:
            continue
        sorted_grp = grp.sort_values("title")
        titles = sorted_grp["title"].fillna("").tolist()
        indices = sorted_grp["title"].index.tolist()
        n = len(titles)

        for i in range(n):
            idx_i = indices[i]
            if idx_i in dup_indices:
                continue
            t_i = titles[i]
            len_i = len(t_i)
            if len_i < 10:
                continue
            # Chỉ so khớp với 3 tin lân cận liền kề trong danh sách đã sort
            for j in range(i + 1, min(i + 4, n)):
                idx_j = indices[j]
                if idx_j in dup_indices:
                    continue
                t_j = titles[j]
                len_j = len(t_j)
                if abs(len_i - len_j) > 10:
                    continue
                if t_i[:8] != t_j[:8]:
                    continue
                score = compute_similarity(t_i, t_j)
                if score >= similarity_threshold:
                    dup_indices.add(idx_j)

    is_dup = pd.Series(False, index=df.index)
    is_dup.loc[list(dup_indices)] = True
    return is_dup


# =============================================================================
# OUTLIER DETECTION (PROPOSAL)
# =============================================================================

def flag_outliers(df: pd.DataFrame):
    """
    Gắn cờ ngoại lai theo IQR cho từng property_type riêng biệt.
    Không tự ý drop data, mà tạo các cờ:
      - is_outlier_price
      - is_luxury
      - is_outlier_area
    """
    df["is_outlier_price"] = False
    df["is_luxury"] = False

    for ptype, grp in df.groupby("property_type"):
        prices = grp["price_vnd"].dropna()
        if len(prices) < 10:
            continue
        q1 = prices.quantile(0.25)
        q3 = prices.quantile(0.75)
        iqr = q3 - q1

        lower_bound = q1 - 1.5 * iqr
        upper_bound = q3 + 1.5 * iqr
        luxury_bound = q3 + 3.0 * iqr

        outlier_mask = (df["property_type"] == ptype) & (
            (df["price_vnd"] < lower_bound) | (df["price_vnd"] > upper_bound)
        )
        luxury_mask = (df["property_type"] == ptype) & (df["price_vnd"] > luxury_bound)

        df.loc[outlier_mask, "is_outlier_price"] = True
        df.loc[luxury_mask, "is_luxury"] = True

    # Outlier diện tích: diện tích < 7m2 hoặc > 2000m2
    # (Giữ căn 7m² phố cổ Hoàn Kiếm — nhà thật, giá 2-6 tỷ)
    df["is_outlier_area"] = (df["area"] < 7.0) | (df["area"] > 2000.0)
    return df


# =============================================================================
# PIPELINE TIỀN XỬ LÝ CHÍNH (MAIN PIPELINE)
# =============================================================================

def run_pipeline(input_path: str, output_dir: str, drop_fuzzy: bool = False):
    start_time = datetime.now()
    os.makedirs(output_dir, exist_ok=True)
    backup_dir = os.path.join(os.path.dirname(input_path), "backup")
    os.makedirs(backup_dir, exist_ok=True)

    report_lines = []
    def log_report(msg: str):
        logger.info(msg)
        report_lines.append(msg)

    log_report("=" * 70)
    log_report("BẮT ĐẦU PIPELINE TIỀN XỬ LÝ DỮ LIỆU BẤT ĐỘNG SẢN HÀ NỘI 2026")
    log_report(f"File đầu vào: {input_path}")
    log_report(f"Thư mục xuất kết quả: {output_dir}")
    log_report("=" * 70)

    # -------------------------------------------------------------------------
    # PHASE 0: BACKUP & KIỂM TRA ĐẦU VÀO
    # -------------------------------------------------------------------------
    log_report("\n[PHASE 0] ĐANG TẠO BẢN SAO LƯU RAW VÀ KIỂM TRA DỮ LIỆU...")
    backup_path = os.path.join(backup_dir, "Hanoi_RealEstate_2026_Raw_BACKUP.csv")
    if not os.path.exists(backup_path):
        shutil.copyfile(input_path, backup_path)
        log_report(f"  -> Đã tạo backup tại: {backup_path}")
    else:
        log_report(f"  -> Bản backup đã tồn tại: {backup_path}")

    # Đọc dữ liệu
    df_raw = pd.read_csv(input_path, dtype={"id": str})
    n_raw_rows, n_raw_cols = df_raw.shape
    log_report(f"  -> Kích thước Raw: {n_raw_rows:,} dòng x {n_raw_cols} cột")

    # -------------------------------------------------------------------------
    # PHASE 1: LOẠI BỎ CÁC BẢN GHI KHÔNG HỢP LỆ (PROPOSAL)
    # -------------------------------------------------------------------------
    log_report("\n[PHASE 1] LỌC BỎ CÁC BẢN GHI KHÔNG HỢP LỆ...")
    df = df_raw.copy()

    # 1.1 Loại bỏ giá 'Thỏa thuận'
    is_thoa_thuan = df["price"].astype(str).str.contains("thuận|thoa thuan", case=False, na=False)
    n_thoa_thuan = is_thoa_thuan.sum()
    df = df[~is_thoa_thuan].reset_index(drop=True)
    log_report(f"  1.1 Loại bỏ {n_thoa_thuan:,} tin có giá 'Thỏa thuận'. Còn lại: {len(df):,} dòng.")

    # 1.2 Loại bỏ Duplicate IDs
    n_dup_ids = df.duplicated(subset=["id"]).sum()
    df = df.drop_duplicates(subset=["id"], keep="first").reset_index(drop=True)
    log_report(f"  1.2 Loại bỏ {n_dup_ids:,} dòng trùng ID. Còn lại: {len(df):,} dòng.")

    # 1.3 Loại bỏ các dòng "Bán đất" (đất nền không phải nhà ở, bị crawler gán nhầm property_type)
    is_ban_dat = df["title2"].astype(str).str.contains("Bán đất", case=False, na=False)
    n_ban_dat = is_ban_dat.sum()
    df = df[~is_ban_dat].reset_index(drop=True)
    log_report(f"  1.3 Loại bỏ {n_ban_dat:,} dòng 'Bán đất' (đất nền ≠ nhà ở). Còn lại: {len(df):,} dòng.")

    # 1.4 Near-Duplicate & Exact Title Duplicate
    log_report("  1.4 Đang kiểm tra Near-Duplicates (Structural & Fuzzy Matching)...")
    df["is_near_dup"] = flag_duplicates(df, similarity_threshold=0.90)
    n_near_dup = df["is_near_dup"].sum()
    log_report(f"  -> Phát hiện {n_near_dup:,} tin trùng lặp/gần trùng.")
    
    if drop_fuzzy:
        df = df[~df["is_near_dup"]].reset_index(drop=True)
        log_report(f"  -> Đã loại bỏ {n_near_dup:,} near-duplicates. Còn lại: {len(df):,} dòng.")
    else:
        log_report("  -> Giữ lại và gắn cờ 'is_near_dup' để phục vụ review/audit an toàn.")

    # -------------------------------------------------------------------------
    # PHASE 2: CHUẨN HÓA VÀ PARSE CÁC CỘT DỮ LIỆU
    # -------------------------------------------------------------------------
    log_report("\n[PHASE 2] PARSING VÀ CHUẨN HÓA CÁC ĐẶC TRƯNG STRUCTURED...")
    
    # Target: price_vnd & log_price
    df["price_vnd"] = df["price"].apply(parse_price_vnd)
    # Loại các dòng không parse được giá hoặc bản ghi lỗi nhập liệu (Invalid records, vd: Biệt thự 333m² gõ nhầm 76 triệu)
    valid_price_mask = df["price_vnd"].notna() & (df["price_vnd"] >= 100_000_000.0)
    n_invalid_price = (~valid_price_mask).sum()
    df = df[valid_price_mask].reset_index(drop=True)
    df["log_price"] = np.log(df["price_vnd"])
    log_report(f"  2.1 Parse price -> price_vnd & log_price (Loại {n_invalid_price} bản ghi không hợp lệ/gõ nhầm < 100tr). Còn lại: {len(df):,} dòng.")

    # Area
    df["area"] = df["area"].apply(parse_area)
    valid_area_mask = df["area"].notna() & (df["area"] > 5.0)
    df = df[valid_area_mask].reset_index(drop=True)
    log_report(f"  2.2 Parse area -> float. Còn lại: {len(df):,} dòng.")

    # Cross-validation: price_per_m2 (Chỉ dùng kiểm tra chất lượng, drop khỏi feature set)
    ppm2_parsed = df["price_per_m2"].apply(parse_price_per_m2)
    ppm2_computed = (df["price_vnd"] / df["area"]) / 1_000_000.0
    valid_check_mask = ppm2_parsed.notna() & (ppm2_parsed > 0)
    df["is_price_area_mismatch"] = False
    mismatch_indices = valid_check_mask & (abs(ppm2_computed - ppm2_parsed) / ppm2_parsed > 0.20)
    df.loc[mismatch_indices, "is_price_area_mismatch"] = True
    log_report(f"  2.3 Cross-check price / area vs price_per_m2: {mismatch_indices.sum():,} dòng lệch >20% (gắn cờ).")

    # House type trích từ title2
    df["house_type"] = df["title2"].apply(extract_house_type)

    # Ward trích từ address
    df["ward"] = df["address"].apply(extract_ward)

    # District & Geo group (Proposal)
    df["geo_group"] = df["district"].apply(get_geo_group)

    # Legal (Chuẩn hóa -> 5 nhóm)
    df["legal"] = df["legal"].apply(normalize_legal)

    # Interior (Chuẩn hóa -> 5 nhóm)
    df["interior"] = df["interior"].apply(normalize_interior)

    # Rooms: bedrooms, toilets
    df["bedrooms"] = df["bedrooms"].apply(parse_rooms)
    df["toilets"] = df["toilets"].apply(parse_rooms)

    # Imputation: Context-Aware Median Imputation theo (property_type, district)
    log_report("  2.4 Điền khuyết (Imputation) bedrooms, toilets bằng Grouped Median...")
    bed_median = df.groupby(["property_type", "district"])["bedrooms"].transform("median")
    toilet_median = df.groupby(["property_type", "district"])["toilets"].transform("median")
    
    # Fallback median toàn cục nếu nhóm quá nhỏ
    global_bed_median = df["bedrooms"].median()
    global_toilet_median = df["toilets"].median()

    df["bedrooms"] = df["bedrooms"].fillna(bed_median).fillna(global_bed_median).fillna(3).round().astype(int)
    df["toilets"] = df["toilets"].fillna(toilet_median).fillna(global_toilet_median).fillna(2).round().astype(int)

    # Floors, Street Frontage, Road Width
    df["floors"] = df["floors"].apply(parse_floors)
    df["street_frontage"] = df["street_frontage"].apply(parse_measurement)
    df["road_width"] = df["road_width"].apply(parse_measurement)

    # Đối với condo, mặt tiền và đường vào đặt là 0 (vì căn hộ nằm trong tòa nhà)
    df.loc[df["property_type"] == "condo", "street_frontage"] = df.loc[df["property_type"] == "condo", "street_frontage"].fillna(0.0)
    df.loc[df["property_type"] == "condo", "road_width"] = df.loc[df["property_type"] == "condo", "road_width"].fillna(0.0)
    # Nhà riêng/biệt thự: điền median theo quận
    for col in ["street_frontage", "road_width", "floors"]:
        col_med = df.groupby(["property_type", "district"])[col].transform("median")
        df[col] = df[col].fillna(col_med).fillna(df[col].median())

    # Hướng nhà và Hướng ban công (Yêu cầu giữ lại)
    df["house_direction"] = df["house_direction"].fillna("Không rõ").astype(str).str.strip()
    df["balcony"] = df["balcony"].fillna("Không rõ").astype(str).str.strip()

    # Temporal columns
    df["date_parsed"] = pd.to_datetime(df["date"], format="%d/%m/%Y", errors="coerce")
    df["crawl_week"] = df["date_parsed"].dt.isocalendar().week.fillna(0).astype(int)
    df["crawl_month"] = df["date_parsed"].dt.month.fillna(0).astype(int)

    # -------------------------------------------------------------------------
    # PHASE 3: LEAKAGE-AWARE TEXT PREPROCESSING (PROPOSAL)
    # -------------------------------------------------------------------------
    log_report("\n[PHASE 3] THỰC HIỆN TIỀN XỬ LÝ VĂN BẢN VÀ MASKING TRÁNH DATA LEAKAGE...")
    masker = LeakageMasker()
    df["text_raw"] = df["title"].fillna("") + " " + df["description"].fillna("")

    log_report("  3.1 Sinh 4 phiên bản text phục vụ Ablation Study...")
    # Setting 1: Raw
    df["text_ablation_raw"] = df["text_raw"]
    
    # Setting 2: Chỉ che số giá và diện tích
    df["text_ablation_numeric"] = [
        masker.mask_text(txt, setting="numeric") for txt in df["text_raw"]
    ]

    # Setting 3: Che giá, giá/m2 và diện tích
    df["text_ablation_price_area"] = [
        masker.mask_text(txt, setting="price_area") for txt in df["text_raw"]
    ]

    # Setting 4: Toàn diện 7 categories (Mặc định cho mô hình đề xuất)
    text_full_list = []
    for txt, proj, addr in zip(df["text_raw"], df["project_name"], df["address"]):
        text_full_list.append(masker.mask_text(txt, project_name=proj, address=addr, setting="full"))
    df["text_full_masked"] = text_full_list

    # 3.2 Xuất leakage audit 200 dòng
    audit_sample = df[["id", "title", "text_raw", "text_full_masked"]].sample(
        n=min(200, len(df)), random_state=42
    ).copy()
    audit_sample["price_spans_masked"] = audit_sample["text_full_masked"].str.count(r"\[PRICE\]")
    audit_sample["unit_price_spans_masked"] = audit_sample["text_full_masked"].str.count(r"\[UNIT_PRICE\]")
    audit_sample["area_spans_masked"] = audit_sample["text_full_masked"].str.count(r"\[AREA\]")
    audit_sample["project_spans_masked"] = audit_sample["text_full_masked"].str.count(r"\[PROJECT\]")
    audit_sample["address_spans_masked"] = audit_sample["text_full_masked"].str.count(r"\[ADDRESS\]")
    audit_sample["contact_spans_masked"] = audit_sample["text_full_masked"].str.count(r"\[CONTACT\]")
    audit_sample["promotion_spans_masked"] = audit_sample["text_full_masked"].str.count(r"\[PROMOTION\]")

    audit_path = os.path.join(output_dir, "leakage_audit.csv")
    audit_sample.to_csv(audit_path, index=False, encoding="utf-8-sig")
    log_report(f"  3.2 Đã xuất file Audit kiểm toán rò rỉ (200 mẫu): {audit_path}")

    # -------------------------------------------------------------------------
    # PHASE 4: OUTLIER DETECTION & CONTEXT-AWARE FLAGGING (PROPOSAL)
    # -------------------------------------------------------------------------
    log_report("\n[PHASE 4] GẮN CỜ NGOẠI LAI (OUTLIERS) THEO PHÂN LOẠI BĐS...")
    df = flag_outliers(df)
    log_report(f"  -> Outliers giá (IQR 1.5): {df['is_outlier_price'].sum():,} dòng ({df['is_outlier_price'].mean()*100:.2f}%)")
    log_report(f"  -> Phân khúc siêu sang (Luxury IQR 3.0): {df['is_luxury'].sum():,} dòng ({df['is_luxury'].mean()*100:.2f}%)")
    log_report(f"  -> Outliers diện tích (<10m2 hoặc >2000m2): {df['is_outlier_area'].sum():,} dòng")

    # -------------------------------------------------------------------------
    # PHASE 5: TẠO VÀ XUẤT CÁC TẬP TIN DỮ LIỆU ĐẦU RA (OUTPUT FILES)
    # -------------------------------------------------------------------------
    log_report("\n[PHASE 5] XUẤT CÁC TẬP TIN KẾT QUẢ ĐẦU RA...")

    # 5.1 File Tabular (Dành cho KAN, CatBoost, LightGBM, XGBoost)
    tabular_cols = [
        "id", "property_type", "house_type", "price_vnd", "log_price",
        "area", "bedrooms", "toilets", "floors", "street_frontage", "road_width",
        "district", "geo_group", "ward", "legal", "interior",
        "house_direction", "balcony", "crawl_week", "crawl_month",
        "is_near_dup", "is_outlier_price", "is_luxury", "is_outlier_area", "is_price_area_mismatch"
    ]
    df_tabular = df[tabular_cols].copy()
    tabular_path = os.path.join(output_dir, "Hanoi_Clean_Tabular.csv")
    df_tabular.to_csv(tabular_path, index=False, encoding="utf-8-sig")
    log_report(f"  [FILE 1] Hanoi_Clean_Tabular.csv : {df_tabular.shape[0]:,} dòng x {df_tabular.shape[1]} cột")

    # 5.2 File Text (Dành cho nhánh PhoBERT / Transformer)
    text_cols = [
        "id", "text_ablation_raw", "text_ablation_numeric", 
        "text_ablation_price_area", "text_full_masked"
    ]
    df_text = df[text_cols].copy()
    text_path = os.path.join(output_dir, "Hanoi_Clean_Text.csv")
    df_text.to_csv(text_path, index=False, encoding="utf-8-sig")
    log_report(f"  [FILE 2] Hanoi_Clean_Text.csv    : {df_text.shape[0]:,} dòng x {df_text.shape[1]} cột")

    # 5.3 File Full Merged (Dành cho mô hình Multimodal Fusion: KAN + PhoBERT)
    df_full = pd.merge(df_tabular, df_text, on="id")
    full_path = os.path.join(output_dir, "Hanoi_Clean_Full.csv")
    df_full.to_csv(full_path, index=False, encoding="utf-8-sig")
    log_report(f"  [FILE 3] Hanoi_Clean_Full.csv    : {df_full.shape[0]:,} dòng x {df_full.shape[1]} cột")

    # 5.4 Breakdown thống kê chi tiết
    log_report("\n[THỐNG KÊ CHI TIẾT]")
    log_report("  --- Phân bổ property_type ---")
    for ptype, count in df["property_type"].value_counts().items():
        log_report(f"    {ptype}: {count:,}")
    log_report("  --- Phân bổ house_type ---")
    for htype, count in df["house_type"].value_counts().items():
        log_report(f"    {htype}: {count:,}")
    log_report("  --- Phân bổ legal ---")
    for legal, count in df["legal"].value_counts().items():
        log_report(f"    {legal}: {count:,}")
    log_report("  --- Phân bổ interior ---")
    for interior, count in df["interior"].value_counts().items():
        log_report(f"    {interior}: {count:,}")
    log_report("  --- Phân bổ geo_group ---")
    for geo, count in df["geo_group"].value_counts().items():
        log_report(f"    {geo}: {count:,}")

    # 5.5 Báo cáo tổng kết quá trình tiền xử lý
    duration = datetime.now() - start_time
    log_report(f"\n[HOÀN THÀNH] Toàn bộ quy trình thực thi trong {duration.total_seconds():.2f} giây.")
    log_report(f"Tổng số bản ghi sạch sẵn sàng huấn luyện: {len(df):,} ({len(df)/n_raw_rows*100:.2f}% raw data).")

    report_path = os.path.join(output_dir, "cleaning_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(report_lines))
    log_report(f"  [FILE 4] cleaning_report.txt     : Báo cáo chi tiết đã lưu.")
    log_report(f"  [FILE 5] leakage_audit.csv       : Mẫu kiểm toán rò rỉ dữ liệu đã lưu.")
    log_report("=" * 70)


# =============================================================================
# CLI ENTRY POINT
# =============================================================================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Leakage-Aware Real Estate Data Preprocessing Pipeline")
    parser.add_argument("--input", type=str, default="Hanoi_RealEstate_2026_Raw.csv", help="Đường dẫn file Raw CSV")
    parser.add_argument("--output-dir", type=str, default="output", help="Thư mục xuất file sạch")
    parser.add_argument("--drop-fuzzy", action="store_true", default=False, help="Tự động drop fuzzy duplicates (mặc định chỉ flag)")
    args = parser.parse_args()

    # Tự động tìm file nếu người dùng chạy từ bất kỳ thư mục nào
    script_dir = os.path.dirname(os.path.abspath(__file__))
    input_file = args.input
    if not os.path.isabs(input_file):
        candidate = os.path.join(script_dir, input_file)
        if os.path.exists(candidate):
            input_file = candidate

    out_dir = args.output_dir
    if not os.path.isabs(out_dir):
        out_dir = os.path.join(script_dir, out_dir)

    run_pipeline(input_file, out_dir, drop_fuzzy=args.drop_fuzzy)
