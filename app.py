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
# Set SOURCE_API_URL in Render Environment to the full URL if the upstream API requires `at`.
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
    """Returns (red_count, white_count) only when exactly 4 known dice are present."""
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

    # Prefer the dice themselves for parity because they are the rawest visible outcome.
    if reds is not None:
        parity = "CHAN" if reds in (0, 2, 4) else "LE"
        number_red = reds
    else:
        parity = normalize_parity(item.get("resultTruyenThong"))
        result_vi = str(item.get("resultVi", "")).strip().lower()
        # Fallback labels observed in the supplied API screenshot.
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

    return {
        "id": str(item.get("id", "")),
        "parity": parity,
        "parity_vi": "Chẵn" if parity == "CHAN" else "Lẻ",
        "red_count": number_red,
        "white_count": whites if whites is not None else (4 - number_red if number_red is not None else None),
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

        # The API screenshot shows descending IDs (newest first). Sort numerically when possible.
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
    # Counts empirical five-way outcomes from the API's actual dice history.
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

    # Smoothed frequencies avoid returning zero for an unseen category.
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

    # Give recent observations additional weight, but expose this as a heuristic.
    recent_codes = valid_codes[:50]  # API history is newest-first
    recent_counts = Counter(recent_codes)
    recent_n = len(recent_codes)
    scored = []
    for row in distribution:
        code = row["red_count"]
        long_rate = row["estimated_frequency"]
        recent_rate = (recent_counts[code] + alpha) / (recent_n + 5 * alpha)
        # Simple blended score, not a calibrated probability of the next round.
        score = 0.55 * long_rate + 0.45 * recent_rate
        item = dict(row)
        item["model_score"] = round(score, 5)
        scored.append(item)

    best = max(scored, key=lambda x: x["model_score"])
    return {
        "prediction": best,
        "distribution": sorted(scored, key=lambda x: x["model_score"], reverse=True),
        "sample_size": total,
        "method": "Tần suất lịch sử có làm trơn + tần suất 50 phiên gần nhất",
        "note": "Điểm mô hình không phải xác suất chắc chắn; kết quả ngẫu nhiên có thể không theo lịch sử.",
    }

def analyze(history):
    # API list is expected newest-first. Reverse for chronological sequence.
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
    windows = {}
    for size in (10, 20, 50, 100):
        segment = outcomes[-size:]
        counts = Counter(segment)
        windows[str(size)] = {
            "sample_size": len(segment),
            "chan": counts["CHAN"],
            "le": counts["LE"],
            "chan_rate": round(counts["CHAN"] / len(segment), 4),
            "le_rate": round(counts["LE"] / len(segment), 4),
        }

    # Smoothed base rate: Beta(1,1) prior.
    base_chan = (overall["CHAN"] + 1) / (n + 2)
    base_le = (overall["LE"] + 1) / (n + 2)

    # Transition estimate based on the immediately previous outcome, smoothed.
    transitions = {"CHAN": Counter(), "LE": Counter()}
    for prev, nxt in zip(outcomes[:-1], outcomes[1:]):
        transitions[prev][nxt] += 1
    last = outcomes[-1]
    trans_total = sum(transitions[last].values())
    trans_chan = (transitions[last]["CHAN"] + 1) / (trans_total + 2)
    trans_le = (transitions[last]["LE"] + 1) / (trans_total + 2)

    # Recent rate over up to 20 outcomes, smoothed.
    recent = outcomes[-20:]
    recent_counts = Counter(recent)
    recent_chan = (recent_counts["CHAN"] + 1) / (len(recent) + 2)
    recent_le = (recent_counts["LE"] + 1) / (len(recent) + 2)

    # Weighted statistical score, not a trained/validated AI model.
    scores = {
        "CHAN": 0.40 * base_chan + 0.25 * trans_chan + 0.35 * recent_chan,
        "LE": 0.40 * base_le + 0.25 * trans_le + 0.35 * recent_le,
    }
    predicted = "CHAN" if scores["CHAN"] >= scores["LE"] else "LE"
    # Normalize the two scores to sum to 100%; these are model scores, not guaranteed odds.
    score_sum = scores["CHAN"] + scores["LE"]
    confidence = scores[predicted] / score_sum if score_sum else 0.5

    # Current streak, based on the latest outcome.
    streak = 0
    for outcome in reversed(outcomes):
        if outcome != last:
            break
        streak += 1

    # A prediction for the next session is a candidate next ID, not a guarantee.
    latest_id = safe_int(history[0].get("id")) if history else None
    next_id = latest_id + 1 if latest_id is not None else None

    # Naive backtest: one-step transition predictor, expanding history.
    # Avoid pretending this is a prospective guarantee.
    correct = 0
    tested = 0
    for i in range(10, n):
        prior = outcomes[:i]
        prev = prior[-1]
        counts = Counter()
        for a, b in zip(prior[:-1], prior[1:]):
            if a == prev:
                counts[b] += 1
        guess = "CHAN" if counts["CHAN"] >= counts["LE"] else "LE"
        if guess == outcomes[i]:
            correct += 1
        tested += 1

    return {
        "sample_size": n,
        "latest_session_id": history[0].get("id") if history else None,
        "next_session_id_estimate": str(next_id) if next_id is not None else None,
        "last_result": "Chẵn" if last == "CHAN" else "Lẻ",
        "current_streak": {"parity": "Chẵn" if last == "CHAN" else "Lẻ", "length": streak},
        "overall": {
            "chan": overall["CHAN"],
            "le": overall["LE"],
            "chan_rate": round(overall["CHAN"] / n, 4),
            "le_rate": round(overall["LE"] / n, 4),
        },
        "windows": windows,
        "transition_after_last": {
            "previous_result": "Chẵn" if last == "CHAN" else "Lẻ",
            "sample_size": trans_total,
            "chan_probability": round(trans_chan, 4),
            "le_probability": round(trans_le, 4),
        },
        "prediction": "Chẵn" if predicted == "CHAN" else "Lẻ",
        "model_score_percent": round(confidence * 100, 2),
        "model_scores": {
            "chan": round(scores["CHAN"], 4),
            "le": round(scores["LE"], 4),
        },
        "backtest": {
            "method": "walk-forward one-step transition baseline",
            "tested_predictions": tested,
            "correct": correct,
            "accuracy": round(correct / tested, 4) if tested else None,
            "note": "Kết quả quá khứ không đảm bảo hiệu quả ở các phiên sau.",
        },
        "method_note": "Điểm mô hình là thống kê mô tả, không phải xác suất chắc chắn hoặc cam kết thắng.",
    }


@app.get("/")
def index():
    return jsonify({
        "name": "Xoc Dia Statistical Prediction API",
        "status": "online",
        "endpoints": {
            "/health": "Kiểm tra trạng thái",
            "/api/history": "Lịch sử phiên đã chuẩn hóa",
            "/api/predict": "Phân tích và dự đoán thống kê",
            "/api/stats": "Thống kê lịch sử",
        },
        "warning": "Chỉ là phân tích thống kê; không thể đảm bảo dự đoán kết quả ngẫu nhiên.",
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


@app.get("/api/predict")
def api_predict():
    history, error = fetch_history(force=request.args.get("refresh") == "1")
    if not history:
        return jsonify({"error": error or "Không có dữ liệu", "source_ok": False}), 502
    result = analyze(history)
    dice_analysis = dice_outcome_analysis(history)
    latest = history[0] if history else {}
    latest_id = latest.get("id")
    result["phien"] = latest_id
    result["ket_qua"] = latest.get("parity_vi")
    result["du_doan_dang_quan"] = dice_analysis
    result["source_ok"] = error is None
    result["history_count"] = len(history)
    if error:
        result["warning"] = error
    return jsonify(result)



@app.get("/api/xocdia")
def api_xocdia():
    """Compact response designed for a frontend prediction panel."""
    history, error = fetch_history(force=request.args.get("refresh") == "1")
    if not history:
        return jsonify({"error": error or "Không có dữ liệu", "source_ok": False}), 502

    parity_analysis = analyze(history)
    dice_analysis = dice_outcome_analysis(history)
    latest = history[0]
    best = dice_analysis.get("prediction") or {}
    latest_id_num = safe_int(latest.get("id"))
    next_session = str(latest_id_num + 1) if latest_id_num is not None else None

    red_count = best.get("red_count")
    white_count = best.get("white_count")
    color_pattern = (
        ["ĐỎ"] * red_count + ["TRẮNG"] * white_count
        if isinstance(red_count, int) and isinstance(white_count, int)
        else []
    )

    response = {
        "phien_hien_tai": latest.get("id"),
        "ket_qua_phien_hien_tai": latest.get("parity_vi"),
        "phien_du_doan": next_session,
        "du_doan": {
            "cua": best.get("parity"),
            "dang_ket_qua": best.get("label"),
            "mau_quan": color_pattern,
            "so_do": red_count,
            "so_trang": white_count,
            "diem_mo_hinh": best.get("model_score"),
        },
        "xac_suat_tham_khao": dice_analysis.get("distribution", []),
        "du_doan_chan_le": parity_analysis.get("prediction"),
        "thong_ke": {
            "so_phien_phan_tich": dice_analysis.get("sample_size", 0),
            "ty_le_chan": parity_analysis.get("overall", {}).get("chan_rate"),
            "ty_le_le": parity_analysis.get("overall", {}).get("le_rate"),
        },
        "source_ok": error is None,
        "ghi_chu": "Đây là dự đoán thống kê, không đảm bảo đúng kết quả phiên tiếp theo."
    }
    if error:
        response["warning"] = error
    return jsonify(response)


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
