# Xoc Dia Statistical API (Render)

## Files
- `app.py`: Flask API, fetches the upstream Tele68 session endpoint, normalizes dice and computes statistics.
- `requirements.txt`: Python dependencies.

## Render deployment
1. Create a GitHub repository and upload `app.py` and `requirements.txt`.
2. On Render choose **New → Web Service** and connect the repository.
3. Set:
   - Runtime: Python
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn app:app`
4. Deploy. Render serves on the port provided by `PORT`.

Optional environment variables:
- `SOURCE_API_URL`: full upstream API URL. If the source requires the `at` parameter, add the full URL here in Render → Environment. Do not commit access tokens into a public GitHub repository.
- `SOURCE_TIMEOUT`: request timeout in seconds (default 12)
- `CACHE_SECONDS`: upstream response cache (default 8)
- `MAX_HISTORY`: max sessions to retain in memory (default 500)

## Endpoints
- `/` service information
- `/health` health check
- `/api/history?limit=100` normalized history
- `/api/history?refresh=1` force source refresh
- `/api/predict` statistical parity prediction and metrics
- `/api/xocdia` compact response: current session/result, estimated next session, predicted four-dice color pattern, and estimated frequency for all five red-count outcomes
- `/api/stats` recent sessions plus statistics

## Important
This is a statistical baseline, not a guaranteed predictor. It combines smoothed overall frequency, recent frequency, and one-step transition counts. The displayed model score is not a calibrated probability of winning. The backtest is a simple walk-forward baseline and should be compared against a 50% baseline on more data before making any claims.

The service uses in-memory caching only; cache/history resets when the instance restarts. The upstream endpoint may change, require valid access, or block server-side requests. If the source API does not allow access from Render, the API will return a source error that should be checked in Render logs.
