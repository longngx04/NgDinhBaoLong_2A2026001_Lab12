# 📝 Solution — Day 12 Lab Exercises (Part 1–5)

> **AICB-P1 · VinUniversity 2026**
> Đáp án chi tiết cho các bài tập codelab.

---

## Part 1: Localhost vs Production

### Exercise 1.1 — Phát hiện anti-patterns trong `01-localhost-vs-production/develop/app.py`

**Tìm được 7 vấn đề:**

| # | Anti-pattern | Dòng code | Giải thích |
|---|-------------|-----------|------------|
| 1 | **Hardcoded API key** | `OPENAI_API_KEY = "sk-hardcoded-..."` | Push lên GitHub → key bị lộ, bất kỳ ai cũng dùng được |
| 2 | **Hardcoded database URL** | `DATABASE_URL = "postgresql://admin:password123@..."` | Password lộ trong source code |
| 3 | **Hardcoded config** | `DEBUG = True`, `MAX_TOKENS = 500` | Không thể thay đổi giữa các environments mà không sửa code |
| 4 | **Print thay vì logging** | `print(f"[DEBUG] Got question: ...")` | Không structured, không filter theo level, khó parse trong log aggregator |
| 5 | **Log ra secrets** | `print(f"[DEBUG] Using key: {OPENAI_API_KEY}")` | Secret bị ghi vào log, ai đọc log cũng thấy |
| 6 | **Không có health check** | Không có `/health` endpoint | Platform không biết khi nào cần restart container |
| 7 | **Host/Port cứng** | `host="localhost"`, `port=8000`, `reload=True` | `localhost` = chỉ chạy trên local; port cứng = conflict trên cloud; reload = chậm và nguy hiểm trong production |

### Exercise 1.2 — Chạy basic version

```bash
cd 01-localhost-vs-production/develop
pip install -r requirements.txt
python app.py
# → Server starts at localhost:8000
# → curl -X POST "http://localhost:8000/ask?question=hello" → trả về mock response
```

**Nhận xét:** Nó chạy được, nhưng **KHÔNG production-ready** vì tất cả anti-patterns ở trên.

### Exercise 1.3 — So sánh Basic vs Advanced

| Feature | Basic (`develop/app.py`) | Advanced (`production/app.py`) | Tại sao quan trọng? |
|---------|--------------------------|-------------------------------|---------------------|
| **Config** | Hardcode (`DEBUG = True`) | Env vars (`settings.debug`) via `config.py` | Dễ thay đổi giữa dev/staging/prod mà không sửa code. Không commit secrets lên Git |
| **Health check** | ❌ Không có | ✅ `/health` + `/ready` + `/metrics` | Platform biết khi nào restart, LB biết khi nào route traffic |
| **Logging** | `print()` — text thô | JSON structured logging | Dễ parse, filter, search trong Datadog/Loki/CloudWatch |
| **Shutdown** | Đột ngột (Ctrl+C) | Graceful via SIGTERM + lifespan | Hoàn thành requests đang xử lý, không mất data |
| **Host binding** | `localhost` (chỉ local) | `0.0.0.0` (nhận kết nối từ ngoài container) | Container/cloud cần bind 0.0.0.0 để nhận traffic |
| **Port** | Cứng `8000` | Từ `PORT` env var | Cloud platforms inject PORT tự động |
| **CORS** | ❌ Không có | ✅ Configured từ env | Cho phép frontend khác domain gọi API |
| **Secrets** | Hardcode trong code | Đọc từ env var, validate khi startup | Bảo mật, dễ rotate, không lộ trong Git |

---

## Part 2: Docker Containerization

### Exercise 2.1 — Đọc `02-docker/develop/Dockerfile`

**1. Base image là gì?**
`python:3.11` — Full Python distribution, bao gồm OS (Debian), Python runtime, pip, và nhiều system packages. Kích thước ~1 GB.

**2. Working directory là gì?**
`/app` — Đây là thư mục bên trong container nơi code được copy vào và chạy. Tương tự `cd /app`.

**3. Tại sao COPY requirements.txt trước?**
Docker sử dụng **layer caching**. Nếu `requirements.txt` không thay đổi, Docker sẽ dùng cached layer cho `pip install` (rất nhanh). Nếu COPY toàn bộ code trước, mỗi lần thay đổi 1 dòng code → Docker phải install lại toàn bộ dependencies (rất chậm).

```
Layer 1: FROM python:3.11           ← cached
Layer 2: COPY requirements.txt      ← cached (nếu không đổi)
Layer 3: RUN pip install ...         ← cached (nếu requirements không đổi)
Layer 4: COPY app.py .              ← rebuild (code thay đổi)
```

**4. CMD vs ENTRYPOINT khác nhau thế nào?**
- **CMD**: Lệnh mặc định khi container start, **có thể override** bằng `docker run <image> <new-command>`.
- **ENTRYPOINT**: Lệnh cố định, **không thể override** (chỉ thêm arguments). Dùng khi container luôn chạy 1 chương trình.
- Kết hợp: ENTRYPOINT = fixed command, CMD = default arguments.

### Exercise 2.3 — Multi-stage build

**Stage 1 (Builder) làm gì?**
- Dùng image đầy đủ có gcc, build tools
- Install tất cả Python dependencies (có thể cần compile C extensions)
- Dependencies được install vào `--user` directory

**Stage 2 (Runtime) làm gì?**
- Dùng image `slim` (nhẹ hơn nhiều)
- Chỉ COPY dependencies đã compiled từ stage 1
- COPY application code
- Không có build tools, gcc → image nhỏ hơn

**Tại sao image nhỏ hơn?**
- Stage 1 có gcc, build tools (~400MB) → **không nằm trong final image**
- Final image chỉ chứa: Python slim + compiled packages + app code
- Kết quả: **giảm 50-70% kích thước** (từ ~1GB xuống ~200-300MB)

### Exercise 2.4 — Docker Compose architecture

```
┌─────────────┐     port 80     ┌─────────────┐     port 8000    ┌───────────┐
│   Client    │ ──────────────→ │    Nginx    │ ───────────────→ │   Agent   │
│  (browser)  │                 │  (reverse   │                  │ (FastAPI) │
└─────────────┘                 │   proxy)    │                  └─────┬─────┘
                                └─────────────┘                        │
                                                                       │ port 6379
                                                                ┌──────▼──────┐
                                                                │    Redis    │
                                                                │  (session   │
                                                                │   store)    │
                                                                └─────────────┘
```

**Services:** Nginx (load balancer), Agent (FastAPI app), Redis (session/cache store)
**Communication:** Client → Nginx (port 80) → Agent (port 8000) → Redis (port 6379)
Tất cả services trong cùng Docker network nên có thể gọi nhau bằng service name.

---

## Part 3: Cloud Deployment

### Exercise 3.2 — So sánh `render.yaml` vs `railway.toml`

| Aspect | `railway.toml` | `render.yaml` |
|--------|---------------|---------------|
| **Format** | TOML | YAML |
| **Build config** | `builder = "DOCKERFILE"` | `runtime: docker` |
| **Start command** | Trong `[deploy].startCommand` | Trong `startCommand` |
| **Health check** | `healthcheckPath = "/health"` | `healthCheckPath: /health` |
| **Env vars** | Set qua CLI: `railway variables set` | Inline trong YAML hoặc dashboard |
| **Auto-generate secrets** | ❌ Manual | ✅ `generateValue: true` |
| **Multi-service** | Mỗi service 1 project riêng | Tất cả trong 1 file (Blueprint) |
| **Redis** | Thêm Redis plugin riêng | Define inline `type: redis` |
| **Region** | Auto hoặc dashboard | `region: singapore` |

**Nhận xét:** Render YAML mạnh hơn vì khai báo cả infrastructure (Redis, envs) trong 1 file. Railway đơn giản hơn nhưng cần thao tác manual nhiều hơn.

---

## Part 4: API Security

### Exercise 4.1 — API Key authentication (04-api-gateway/develop/app.py)

**API key được check ở đâu?**
Trong function `verify_api_key()`, được inject vào endpoint `/ask` qua FastAPI `Depends()`:
```python
def verify_api_key(api_key: str = Security(api_key_header)) -> str:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing API key...")
    if api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key.")
    return api_key
```

**Điều gì xảy ra nếu sai key?**
- Không có key → HTTP 401 Unauthorized
- Sai key → HTTP 403 Forbidden
- Đúng key → request được xử lý bình thường

**Làm sao rotate key?**
1. Set env var mới: `AGENT_API_KEY=new-key-value`
2. Restart server (hoặc redeploy)
3. Cập nhật key cho clients
4. **Best practice:** Support cả old key + new key trong transition period

### Exercise 4.3 — Rate Limiting (04-api-gateway/production/rate_limiter.py)

**Algorithm:** **Sliding Window** (sorted set trong memory, giống Redis ZSET)
- Mỗi request được lưu với timestamp
- Xóa entries cũ hơn window (60 giây)
- Đếm entries còn lại → nếu >= limit → reject 429

**Limit:**
- User role: **10 requests/phút**
- Admin role: **100 requests/phút**

**Bypass limit cho admin:**
Sử dụng role-based rate limiter:
```python
limiter = rate_limiter_admin if role == "admin" else rate_limiter_user
```
Admin có limit cao hơn (100 vs 10 req/min).

### Exercise 4.4 — Cost Guard

```python
import redis
from datetime import datetime

r = redis.Redis()

def check_budget(user_id: str, estimated_cost: float) -> bool:
    """
    Return True nếu còn budget, False nếu vượt.
    Mỗi user có budget $10/tháng. Track spending trong Redis. Reset đầu tháng.
    """
    month_key = datetime.now().strftime("%Y-%m")
    key = f"budget:{user_id}:{month_key}"

    current = float(r.get(key) or 0)
    if current + estimated_cost > 10:
        return False

    r.incrbyfloat(key, estimated_cost)
    r.expire(key, 32 * 24 * 3600)  # 32 days TTL
    return True
```

**Giải thích:**
- Key format: `budget:<user_id>:<YYYY-MM>` → tự động reset mỗi tháng (key khác)
- `incrbyfloat`: atomic operation, thread-safe
- `expire 32 days`: tự cleanup keys cũ, không cần cron job

---

## Part 5: Scaling & Reliability

### Exercise 5.1 — Health + Readiness checks

```python
@app.get("/health")
def health():
    """Liveness probe — container còn sống không?"""
    return {"status": "ok"}

@app.get("/ready")
def ready():
    """Readiness probe — sẵn sàng nhận traffic không?"""
    try:
        r.ping()           # Check Redis
        # db.execute("SELECT 1")  # Check database (nếu có)
        return {"status": "ready"}
    except Exception:
        return JSONResponse(
            status_code=503,
            content={"status": "not ready"}
        )
```

**Khác biệt:**
- `/health` (Liveness): Chỉ check process còn sống → Platform restart nếu fail
- `/ready` (Readiness): Check dependencies (Redis, DB) → LB ngừng route traffic nếu fail

### Exercise 5.2 — Graceful Shutdown

```python
import signal
import logging

logger = logging.getLogger(__name__)

def shutdown_handler(signum, frame):
    """Handle SIGTERM from container orchestrator"""
    logger.info(f"Received signal {signum} — initiating graceful shutdown")
    # uvicorn tự handle shutdown qua lifespan
    # Bước 1: Stop accepting new requests (is_ready = False)
    # Bước 2: Wait for in-flight requests to finish
    # Bước 3: Close connections (Redis, DB)
    # Bước 4: Exit cleanly

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)
```

**Kết hợp với FastAPI lifespan:**
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: init connections
    yield
    # Shutdown: cleanup
    _is_ready = False
    # Wait for in-flight requests
    # Close Redis connection
```

### Exercise 5.3 — Stateless Design

**Anti-pattern (Stateful):**
```python
# ❌ State trong memory → mất khi restart, không share giữa instances
conversation_history = {}

@app.post("/ask")
def ask(user_id: str, question: str):
    history = conversation_history.get(user_id, [])
```

**Correct (Stateless):**
```python
# ✅ State trong Redis → persist qua restart, share giữa instances
import redis
r = redis.from_url(os.getenv("REDIS_URL"))

@app.post("/ask")
def ask(user_id: str, question: str):
    history = r.lrange(f"history:{user_id}", 0, -1)
```

**Tại sao stateless quan trọng?**
1. **Scale horizontally:** Khi có 3 instances, request có thể đến bất kỳ instance nào. Nếu state trong memory, instance 2 không biết gì về conversation ở instance 1.
2. **Survive restarts:** Memory bị xóa khi restart. Redis persist data.
3. **Zero-downtime deploys:** Rolling deploy thay instance mới, state vẫn còn trong Redis.

---

## ✅ Hoàn thành!

Tất cả exercises Part 1-5 đã được trả lời.
Tiếp theo: xem Part 6 Final Project trong `06-lab-complete/`.

---

## Part 6 Submission URL

- Railway project: `ai-agent-production`
- Service: `agent`
- Public API URL: `https://agent-production-164a.up.railway.app`
- Health endpoint: `https://agent-production-164a.up.railway.app/health`
- Verification date: 2026-06-12

Verified:
- `GET /health` returns `200 OK`
- `POST /ask` with the production `X-API-Key` returns an agent response
- `POST /ask` without `X-API-Key` returns `401`
