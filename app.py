import os
import time
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

DEFAULT_API_URL = "https://wcl.tele68.com/v1/chanlefull/lite-sessions?cp=R&cl=R&pf=web"
API_URL = os.getenv("SOURCE_API_URL", DEFAULT_API_URL)
REQUEST_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT", "12"))
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "8"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "500"))

_cache = {"at": 0.0, "data": None, "error": None}
_cache_lock = threading.Lock()

# Quy ước hiển thị chuẩn theo yêu cầu của bạn
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


def dice_transition_analysis(history):
    """
    Soi cầu chuẩn xác theo lịch sử tuần tự:
    history trả về từ API là danh sách [Mới nhất, Cũ hơn, Cũ nhất,...]
    Đảo ngược lại thành [Cũ nhất, ..., Cũ hơn, Mới nhất] để duyệt theo dòng thời gian tăng dần của phiên.
    """
    chronological = list(reversed(history))
    
    # transitions[ket_qua_hien_tai][ket_qua_phien_sau] = số lần xuất hiện
    transitions = defaultdict(Counter)
    overall_counts = Counter()

    for i in range(len(chronological) - 1):
        curr_code = chronological[i].get("red_count")
        next_code = chronological[i+1].get("red_count")
        
        if curr_code is not None and next_code is not None:
            transitions[curr_code][next_code] += 1
            overall_counts[next_code] += 1

    # Lấy kết quả của phiên MỚI NHẤT hiện tại
    latest_row = history[0] if history else {}
    latest_code = latest_row.get("red_count")

    # Dò xem sau con số của phiên hiện tại, lịch sử đã từng ra những con số nào ở phiên kế tiếp
    target_transitions = transitions[latest_code] if latest_code is not None else Counter()
    total_matched = sum(target_transitions.values())

    distribution = []
    
    # Nếu lịch sử của trạng thái này chưa có nhiều, kết hợp thêm tần suất chung để không bị lỗi trống
    for code in range(5):
        count_in_pattern = target_transitions[code]
        # Tính điểm dựa trên số lần thực tế xuất hiện sau trạng thái này trong lịch sử
        if total_matched > 0:
            score = count_in_pattern / total_matched
        else:
            # Fallback nếu trạng thái này chưa từng xuất hiện trước đó: dùng tần suất tổng thể
            total_overall = sum(overall_counts.values())
            score = (overall_counts[code] + 1) / (total_overall + 5) if total_overall > 0 else 0.2

        distribution.append({
            "red_count": code,
            "white_count": 4 - code,
            "label": DETAIL_LABELS[code],
            "parity": "CHẴN" if code in (0, 2, 4) else "LẺ",
            "history_matched_count": count_in_pattern,
            "model_score": round(score, 4),
            "estimated_percent": round(score * 100, 2)
        })

    # Sắp xếp kết quả có tỉ lệ xuất hiện sau trạng thái này cao nhất lên đầu
    sorted_dist = sorted(distribution, key=lambda x: (x["history_matched_count"], x["model_score"]), reverse=True)
    best = sorted_dist[0]

    return {
        "latest_code": latest_code,
        "latest_label": DETAIL_LABELS.get(latest_code, "Không rõ") if latest_code is not None else None,
        "prediction": best,
        "distribution": sorted_dist,
        "sample_size": len(chronological),
        "history_matched_count": total_matched
    }


@app.get("/")
def index():
    return jsonify({
        "name": "Xoc Dia True History Pattern API",
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

    analysis = dice_transition_analysis(history)
    latest = history[0]
    latest_id_num = safe_int(latest.get("id"))
    next_session = str(latest_id_num + 1) if latest_id_num is not None else None

    best_pred = analysis.get("prediction", {})

    response = {
        "phien_hien_tai": str(latest.get("id")),
        "ket_qua_hien_tai": latest.get("chi_tiet_ket_qua"), # Ví dụ: 4 Trắng (0 Đỏ) hoặc 2 Trắng 2 Đỏ
        "phien_du_doan": next_session,
        "du_doan_chan_le": best_pred.get("parity"),
        "du_doan_chi_tiet": best_pred.get("label"),        # Soi ra con số tiếp theo sau kết quả hiện tại
        "so_do": best_pred.get("red_count"),
        "so_trang": best_pred.get("white_count"),
        "so_lan_xuat_hien_trong_lich_su": analysis.get("history_matched_count"),
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
    
    analysis = dice_transition_analysis(history)
    latest = history[0] if history else {}
    
    return jsonify({
        "phien_hien_tai": latest.get("id"),
        "ket_qua_hien_tai": latest.get("chi_tiet_ket_qua"),
        "soi_cau_lich_su": analysis,
        "source_ok": error is None
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
