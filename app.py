import os
import time
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests
from Flask import Flask, jsonify, request

app = Flask(__name__)

DEFAULT_API_URL = "https://wcl.tele68.com/v1/chanlefull/lite-sessions?cp=R&cl=R&pf=web"
API_URL = os.getenv("SOURCE_API_URL", DEFAULT_API_URL)
REQUEST_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT", "12"))
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "8"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "500"))

_cache = {"at": 0.0, "data": None, "error": None}
_cache_lock = threading.Lock()

# Quy ước hiển thị chi tiết theo yêu cầu
DETAIL_LABELS = {
    0: "4 Trắng (0 Đỏ)",
    1: "3 Trắng 1 Đỏ",
    2: "2 Trắng 2 Đỏ",
    3: "3 Đỏ 1 Trắng",
    4: "4 Đỏ"
}


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_parity(value):
    if value is None:
        return None
    v = str(value).strip().lower()
    if v in {"chan", "chẵn", "even"} or "chan" == v:
        return "CHAN"
    if v in {"le", "lẻ", "odd"}:
        return "LE"
    return None


def normalize_dice(dices):
    if not isinstance(dices, list) or len(dices) != 4:
        return None, None
    reds = 0
    whites = 0
    for die in dices:
        value = str(die).strip().lower()
        if value in {"do", "đỏ", "red"}:
            reds += 1
        elif value in {"trang", "trắng", "white"}:
            whites += 1
        else:
            return None, None
    return reds, whites


def parse_item(item):
    if not isinstance(item, dict):
        return None

    dices = item.get("dices")
    reds, whites = normalize_dice(dices)

    if reds is not None:
        parity = "CHAN" if reds in (0, 2, 4) else "LE"
        number_red = reds
    else:
        parity = normalize_parity(item.get("resultTruyenThong"))
        result_vi = str(item.get("resultVi", "")).strip().lower()
        label_map = {
            "four_trang": 0,
            "three_trang": 1,
            "two_trang": 2,
            "two_do": 2,
            "three_do": 3,
            "four_do": 4,
        }
        number_red = label_map.get(result_vi)
        if number_red is not None:
            parity = "CHAN" if number_red % 2 == 0 else "LE"

    if number_red is None or not (0 <= number_red <= 4):
        return None

    if parity is None:
        parity = "CHAN" if number_red % 2 == 0 else "LE"

    return {
        "id": str(item.get("id", "")),
        "parity": parity,
        "parity_vi": "Chẵn" if parity == "CHAN" else "Lẻ",
        "red_count": number_red,
        "white_count": 4 - number_red,
        "chi_tiet_ket_qua": DETAIL_LABELS.get(number_red, "Không xác định"),
        "resultVi": item.get("resultVi"),
        "dices": dices if isinstance(dices, list) else None,
    }


def fetch_history(force=False):
    now = time.time()
    with _cache_lock:
        if not force and _cache["data"] is not None and now - _cache["at"] < CACHE_SECONDS:
            return _cache["data"], None

    try:
        response = requests.get(
            API_URL,
            timeout=REQUEST_TIMEOUT,
            headers={"User-Agent": "XocDiaStatsAPI/1.0", "Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        raw_list = payload.get("list", []) if isinstance(payload, dict) else []
        if not isinstance(raw_list, list):
            raise ValueError("API JSON không có trường 'list' dạng danh sách.")

        parsed = []
        for item in raw_list:
            row = parse_item(item)
            if row:
                parsed.append(row)

        if parsed and all(safe_int(x["id"]) is not None for x in parsed):
            parsed.sort(key=lambda row: safe_int(row["id"]), reverse=True)

        parsed = parsed[:MAX_HISTORY]
        with _cache_lock:
            _cache.update({"at": time.time(), "data": parsed, "error": None})
        return parsed, None
    except Exception as exc:
        with _cache_lock:
            cached = _cache.get("data")
        if cached is not None:
            return cached, f"Không lấy được cache: {type(exc).__name__}"
        return [], f"Lỗi đọc API nguồn: {type(exc).__name__}"


def pattern_matching_50_analysis(history):
    chronological = list(reversed(history))  # Cũ nhất -> Mới nhất
    
    # Bắt buộc lấy đúng 50 phiên gần nhất (hoặc ít hơn nếu lịch sử không đủ 50 phiên)
    window_len = 50
    if len(chronological) <= window_len:
        window_len = max(5, len(chronological) - 1)

    if window_len < 2:
        return {
            "prediction": None,
            "sample_size": len(chronological),
            "history_matched_count": 0,
        }

    # Lấy đúng chuỗi 50 phiên mới nhất làm khung soi cầu
    current_pattern = tuple(row.get("red_count") for row in chronological[-window_len:] if row.get("red_count") is not None)
    
    # Quét toàn bộ lịch sử để tìm xem khung 50 phiên này khớp ở những đoạn nào và ván sau đó ra gì
    next_outcomes_counter = Counter()
    overall_counts = Counter()
    
    for i in range(len(chronological) - window_len):
        historical_segment = tuple(chronological[i + j].get("red_count") for j in range(window_len))
        next_code = chronological[i + window_len].get("red_count")
        
        if next_code is not None:
            overall_counts[next_code] += 1
            if historical_segment == current_pattern:
                next_outcomes_counter[next_code] += 1

    total_matched = sum(next_outcomes_counter.values())

    distribution = []
    total_overall = sum(overall_counts.values())

    for code in range(5):
        matched_count = next_outcomes_counter[code]
        if total_matched > 0:
            score = matched_count / total_matched
        else:
            # Nếu khung 50 phiên này quá mới chưa từng lặp lại y hệt, dùng tần suất của các ván cuối trong khung đó để dự đoán
            score = (overall_counts[code] + 1) / (total_overall + 5) if total_overall > 0 else 0.2

        distribution.append({
            "red_count": code,
            "white_count": 4 - code,
            "label": DETAIL_LABELS[code],
            "parity": "CHẴN" if code in (0, 2, 4) else "LẺ",
            "history_matched_count": matched_count,
            "model_score": round(score, 4),
            "estimated_percent": round(score * 100, 2)
        })

    sorted_dist = sorted(distribution, key=lambda x: (x["history_matched_count"], x["model_score"]), reverse=True)
    best = sorted_dist[0]

    return {
        "pattern_length_used": window_len, # Cố định đúng khung số phiên yêu cầu
        "prediction": best,
        "distribution": sorted_dist,
        "sample_size": len(chronological),
        "history_matched_count": total_matched
    }


@app.get("/")
def index():
    return jsonify({
        "name": "Xoc Dia Pattern Matching API",
        "status": "online",
        "endpoint": "/api/xocdia"
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": datetime.now(timezone.utc).isoformat()})


@app.get("/api/xocdia")
def api_xocdia():
    history, error = fetch_history(force=request.args.get("refresh") == "1")
    if not history:
        return jsonify({"error": error or "Không có dữ liệu", "source_ok": False}), 502

    analysis = pattern_matching_50_analysis(history)
    latest = history[0]
    latest_id_num = safe_int(latest.get("id"))
    next_session = str(latest_id_num + 1) if latest_id_num is not None else None

    best_pred = analysis.get("prediction", {})

    response = {
        "phien_hien_tai": str(latest.get("id")),
        "ket_qua_hien_tai": latest.get("chi_tiet_ket_qua"),
        "khung_soi_cau": f"Đã gom khung {analysis.get('pattern_length_used', 50)} phiên gần nhất để quét lịch sử",
        "phien_du_doan": next_session,
        "dự_đoán_chẵn_lẻ": best_pred.get("parity"),
        "dự_đoán_chi_tiet": best_pred.get("label"),
        "so_do": best_pred.get("red_count"),
        "so_trang": best_pred.get("white_count"),
        "so_lan_khop_khung_trong_lich_su": analysis.get("history_matched_count"),
        "source_ok": error is None
    }
    
    if error:
        response["warning"] = error
        
    return jsonify(response)


@app.get("/api/predict")
def api_predict():
    history, error = fetch_history(force=request.args.get("refresh") == "1")
    if not history:
        return jsonify({"error": error or "Không có dữ liệu", "source_ok": False}), 502
    
    analysis = pattern_matching_50_analysis(history)
    latest = history[0] if history else {}
    
    return jsonify({
        "phien_hien_tai": latest.get("id"),
        "chi_tiet_phan_tich": analysis,
        "source_ok": error is None
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
