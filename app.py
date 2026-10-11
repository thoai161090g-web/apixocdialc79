import os
import math
import time
import threading
from collections import Counter, defaultdict
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

DEFAULT_API_URL = "https://wcl.tele68.com/v1/chanlefull/lite-sessions?cp=R&cl=R&pf=web"
# Set SOURCE_API_URL trong Render Environment nếu API nguồn yêu cầu tham số `at`.
API_URL = os.getenv("SOURCE_API_URL", DEFAULT_API_URL)
REQUEST_TIMEOUT = float(os.getenv("SOURCE_TIMEOUT", "12"))
CACHE_SECONDS = int(os.getenv("CACHE_SECONDS", "8"))
MAX_HISTORY = int(os.getenv("MAX_HISTORY", "500"))

_cache = {"at": 0.0, "data": None, "error": None}
_cache_lock = threading.Lock()


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
    """Trả về (red_count, white_count) khi có đúng 4 quân vị hợp lệ."""
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

    if parity is None:
        return None

    detail_labels = {
        0: "4 Trắng",
        1: "3 Trắng 1 Đỏ",
        2: "2 Trắng 2 Đỏ",
        3: "3 Đỏ 1 Trắng",
        4: "4 Đỏ"
    }

    return {
        "id": str(item.get("id", "")),
        "parity": parity,
        "parity_vi": "Chẵn" if parity == "CHAN" else "Lẻ",
        "red_count": number_red,
        "white_count": whites if whites is not None else (4 - number_red if number_red is not None else None),
        "chi_tiet_ket_qua": detail_labels.get(number_red, "Không xác định"),
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

        def sort_key(row):
            number = safe_int(row.get("id"))
            return (number is not None, number if number is not None else row.get("id", ""))

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
            return cached, f"Không lấy được dữ liệu mới; đang dùng cache gần nhất: {type(exc).__name__}"
        return [], f"Không đọc được API nguồn: {type(exc).__name__}: {str(exc)[:250]}"


OUTCOME_LABELS = {
    0: "4 TRẮNG",
    1: "3 TRẮNG - 1 ĐỎ",
    2: "2 ĐỎ - 2 TRẮNG",
    3: "3 ĐỎ - 1 TRẮNG",
    4: "4 ĐỎ",
}


def outcome_code(row):
    reds = row.get("red_count")
    if reds is not None and 0 <= reds <= 4:
        return reds
    dices = row.get("dices")
    reds, _ = normalize_dice(dices)
    return reds


def dice_outcome_analysis(history):
    counts = Counter()
    valid_codes = []
    for row in history:
        code = outcome_code(row)
        if code is not None:
            counts[code] += 1
            valid_codes.append(code)

    total = len(valid_codes)
    if total == 0:
        return {
            "prediction": None,
            "distribution": [],
            "sample_size": 0,
            "note": "Không có đủ dữ liệu quân vị để phân tích.",
        }

    alpha = 1.0
    denom = total + 5 * alpha
    distribution = []
    for code in range(5):
        p = (counts[code] + alpha) / denom
        distribution.append({
            "red_count": code,
            "white_count": 4 - code,
            "label": OUTCOME_LABELS[code],
            "parity": "CHẴN" if code in (0, 2, 4) else "LẺ",
            "count": counts[code],
            "estimated_frequency": round(p, 4),
            "estimated_percent": round(p * 100, 2),
        })

    recent_codes = valid_codes[:50]
    recent_counts = Counter(recent_codes)
    recent_n = len(recent_codes)
    scored = []
    for row in distribution:
        code = row["red_count"]
        long_rate = row["estimated_frequency"]
        recent_rate = (recent_counts[code] + alpha) / (recent_n + 5 * alpha)
        score = 0.55 * long_rate + 0.45 * recent_rate
        item = dict(row)
        item["model_score"] = round(score, 5)
        scored.append(item)

    best = max(scored, key=lambda x: x["model_score"])
    return {
        "prediction": best,
        "distribution": sorted(scored, key=lambda x: x["model_score"], reverse=True),
        "sample_size": total,
    }


def analyze(history):
    chronological = list(reversed(history))
    outcomes = [row["parity"] for row in chronological if row.get("parity") in ("CHAN", "LE")]
    n = len(outcomes)
    if n == 0:
        return {
            "sample_size": 0,
            "prediction": None,
            "message": "Chưa có dữ liệu hợp lệ để phân tích.",
        }

    overall = Counter(outcomes)
    base_chan = (overall["CHAN"] + 1) / (n + 2)
    base_le = (overall["LE"] + 1) / (n + 2)

    transitions = {"CHAN": Counter(), "LE": Counter()}
    for prev, nxt in zip(outcomes[:-1], outcomes[1:]):
        transitions[prev][nxt] += 1
    last = outcomes[-1]
    trans_total = sum(transitions[last].values())
    trans_chan = (transitions[last]["CHAN"] + 1) / (trans_total + 2)
    trans_le = (transitions[last]["LE"] + 1) / (trans_total + 2)

    recent = outcomes[-20:]
    recent_counts = Counter(recent)
    recent_chan = (recent_counts["CHAN"] + 1) / (len(recent) + 2)
    recent_le = (recent_counts["LE"] + 1) / (len(recent) + 2)

    scores = {
        "CHAN": 0.40 * base_chan + 0.25 * trans_chan + 0.35 * recent_chan,
        "LE": 0.40 * base_le + 0.25 * trans_le + 0.35 * recent_le,
    }
    predicted = "CHAN" if scores["CHAN"] >= scores["LE"] else "LE"

    return {
        "sample_size": n,
        "prediction": "Chẵn" if predicted == "CHAN" else "Lẻ",
        "chan_score": round(scores["CHAN"], 4),
        "le_score": round(scores["LE"], 4),
    }


@app.get("/")
def index():
    return jsonify({
        "name": "Xoc Dia Statistical Prediction API",
        "status": "online",
        "endpoints": {
            "/health": "Kiểm tra trạng thái",
            "/api/history": "Lịch sử phiên đã chuẩn hóa",
            "/api/predict": "Phân tích và dự đoán thống kê đầy đủ",
            "/api/xocdia": "Dữ liệu rút gọn (Phiên, Kết quả, Dự đoán)",
            "/api/stats": "Thống kê lịch sử",
        }
    })


@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": datetime.now(timezone.utc).isoformat()})


@app.get("/api/history")
def api_history():
    history, error = fetch_history(force=request.args.get("refresh") == "1")
    limit = safe_int(request.args.get("limit")) or 100
    limit = max(1, min(limit, MAX_HISTORY))
    result = {"count": len(history), "history": history[:limit]}
    if error:
        result["warning"] = error
        result["source_ok"] = False
    else:
        result["source_ok"] = True
    return jsonify(result), (502 if not history and error else 200)


@app.get("/api/xocdia")
def api_xocdia():
    """Endpoint trả về đúng định dạng yêu cầu của bạn."""
    history, error = fetch_history(force=request.args.get("refresh") == "1")
    if not history:
        return jsonify({"error": error or "Không có dữ liệu", "source_ok": False}), 502

    parity_analysis = analyze(history)
    dice_analysis = dice_outcome_analysis(history)
    
    # Phiên hiện tại (mới nhất)
    latest = history[0]
    latest_id_num = safe_int(latest.get("id"))
    
    # Dự đoán phiên tiếp theo (tăng ID lên 1)
    next_session = str(latest_id_num + 1) if latest_id_num is not None else None

    # Lấy thông tin dự đoán quân vị tốt nhất
    best = dice_analysis.get("prediction") or {}
    red_count = best.get("red_count")
    
    detail_labels = {
        0: "4 Trắng",
        1: "3 Trắng 1 Đỏ",
        2: "2 Trắng 2 Đỏ",
        3: "3 Đỏ 1 Trắng",
        4: "4 Đỏ"
    }
    chi_tiet_du_doan = detail_labels.get(red_count, "Không xác định")

    response = {
        "phien": str(latest.get("id")),
        "ket_qua": latest.get("parity_vi"),              # Chẵn hoặc Lẻ
        "chi_tiet_ket_qua": latest.get("chi_tiet_ket_qua"), # Ví dụ: 3 Đỏ 1 Trắng, 4 Trắng,...
        "du_doan_phien": next_session,                    # Phiên tiếp theo cần dự đoán
        "dự_đoán_chẵn_lẻ": parity_analysis.get("prediction"), # Chẵn hoặc Lẻ
        "dự_đoán_chi_tiet": chi_tiet_du_doan,              # Chi tiết màu (VD: 3 Đỏ 1 Trắng)
        "so_do": red_count,
        "so_trang": best.get("white_count"),
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
    
    result = analyze(history)
    dice_analysis = dice_outcome_analysis(history)
    latest = history[0] if history else {}
    
    result["phien"] = latest.get("id")
    result["ket_qua"] = latest.get("parity_vi")
    result["chi_tiet_ket_qua"] = latest.get("chi_tiet_ket_qua")
    result["du_doan_chi_tiet_quan_vi"] = dice_analysis
    result["source_ok"] = error is None
    result["history_count"] = len(history)
    
    if error:
        result["warning"] = error
    return jsonify(result)


@app.get("/api/stats")
def api_stats():
    history, error = fetch_history(force=request.args.get("refresh") == "1")
    if not history:
        return jsonify({"error": error or "Không có dữ liệu"}), 502
    stats = analyze(history)
    return jsonify({
        "history_count": len(history),
        "latest_sessions": history[:20],
        "analysis": stats,
        "warning": error,
    })


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)
