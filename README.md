# back_end_iraqi — مزوّد الذكاء الاصطناعي (FastAPI + vLLM)

باك اند FastAPI يقدّم خدمات الذكاء الاصطناعي باللهجة العراقية: وكيل مبيعات، واجهة
OpenAI متوافقة، استخراج طلب من نص/صوت/صورة، مكالمات صوتية حية (LiveKit)، وتصحيح مناطق شركات
التوصيل. لا يخزّن بيانات أعمال — البيانات عند باك اند السستم، وتنفيذ الأدوات عند العميل.

يشتغل محلياً، وعلى RunPod بصورة `ubuntu:22.04` خام: `start.sh` يبني كل شي من
الصفر بأول إقلاع (Python/pip، vLLM nightly بدعم Gemma 4 مع torch المتوافق،
ffmpeg/libsndfile1، Whisper وChatterbox TTS).

## الميزات

| الميزة | الفولدر | النقاط | التوثيق |
|---|---|---|---|
| وكيل مبيعات | `app/features/sales/` | `/sales/*` | [sales.md](docs/api/sales.md) |
| واجهة OpenAI | `app/features/openai_compat/` | `/v1/chat/completions` | [openai-compat.md](docs/api/openai-compat.md) |
| إنشاء طلب متعدد الوسائط | `app/features/order_intake/` | `/orders/create` | [orders.md](docs/api/orders.md) |
| مكالمات صوتية حية (LiveKit أو WebSocket محلي) + مساعد الموظف | `app/features/voice_followup/` | `/voice_followup/*` | [voice-followup.md](docs/api/voice-followup.md) |
| تصحيح مناطق شركات التوصيل | `app/features/district_correction/` | `/v1/district-correction*` | [district-correction.md](docs/api/district-correction.md) |

المصادقة، الرابط الأساسي، نقاط الفحص (`/health`، `/health/voice`، `/gpu`، `/metrics`)، وملخص كل
النقاط: [docs/api/overview.md](docs/api/overview.md).

## هيكل المشروع

```text
app/
  main.py, config.py, auth.py   نقطة الدخول، الإعدادات الثابتة، مفاتيح الخدمات
  core/        عميل vLLM، خيارات التوليد، حالة الموديل، الرد الاحتياطي، تطبيع النص، الصور، أخطاء باك اند السستم
  orders/      مخطط الطلب، استخراجه، حسابه، وإرساله لباك اند السستم
  rag/         بحث BM25 للهجة والمواقع (locations.json مولَّد من data/locations)
  features/    ميزة بكل فولدر: router.py + prompts.py + منطقها
data/
  address/     كتالوج مناطق شركات التوصيل (Excel لكل شركة)
  locations/   states.xlsx + districts.xlsx (مصدر app/rag/locations.json)
docs/          التوثيق (انظر docs/README.md)
scripts/       قياس ودقة تصحيح المناطق
training/      تدريب الـ reranker لتصحيح المناطق
static/        لوحة الاختبار /test
tests/         اختبارات pytest
```

تفصيل المعمارية (محرك vLLM، باك اند السستم، الإعدادات، التنزيل التلقائي):
[docs/architecture.md](docs/architecture.md).

## التشغيل محلياً (Windows)

```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

ثم افتح http://localhost:8000/docs. محلياً ماكو خادم vLLM (يحتاج GPU/Linux)، فنقاط
الذكاء ما تولّد رداً إلا مع خادم LLM محلي:

### مع خادم LLM محلي (LM Studio)

شغّل خادم LLM متوافق مع OpenAI على `http://localhost:1234/v1` وحمّل فيه موديلاً، ثم من Git Bash:

```bash
bash start_local.sh
LLM_MODEL=<model-id> LLM_BASE_URL=http://localhost:11434/v1 bash start_local.sh   # خادم أو موديل آخر
```

السكربت يكتشف الموديل المحمَّل من `/v1/models` ويمرّر `LLM_BASE_URL` و`LLM_MODEL` لـ `app/config.py`. لوحة الاختبار على http://127.0.0.1:8000/test تعرض الرابط والموديل المستعملين فعلياً.

> ⚠️ LM Studio يعرض بـ `/v1/models` **كل** الموديلات المنزَّلة، محمَّلة أم لا. إذا اكتشف السكربت موديلاً غير محمَّل يرد `/v1/chat/completions` بـ `AI_MODEL_NOT_LOADED`؛ حدّد الموديل صراحةً بـ `LLM_MODEL=<id>` (مثل `google/gemma-4-e4b`). `GET /health/llm` يكشف الحالة الحقيقية.

| المتغيّر | الافتراضي | الوصف |
|---|---|---|
| `LLM_REQUEST_TIMEOUT_SECONDS` | `120` | مهلة طلب توليد واحد؛ تجاوزها يرجع `AI_REQUEST_TIMEOUT` (504) |
| `CATALOG_BASE_URL` | فارغ (معطّل) | باك اند البيانات Catalog لأدوات الطلبات/المنتجات بالمكالمات ومساعد الموظف |
| `CATALOG_ACCESS_TOKEN` / `CATALOG_TENANT_ID` | فارغ | `Authorization: Bearer` و`X-Tenant-Id` لـ Catalog (التوكن سر — بيئة فقط) |
| `CATALOG_TIMEOUT_SECONDS` / `CATALOG_STORAGE_ID` | `15` / فارغ | مهلة Catalog، ومخزن المتجر لفلترة السعر والتوفّر |
| `CATALOG_PATH_*` | مسارات `ai_agent` | كل مسار Catalog قابل للتغيير (`CATALOG_PATH_CALL_OUTCOME` فارغ = ما ترسل النتيجة) |
| `SYSTEM_BACKEND_CALL_OUTCOME_PATH` | `/calls/outcome` | فارغ = ما ترسل نتيجة المكالمة لباك اند السستم |
| `VOICE_STREAM_*` | انظر التوثيق | الوكيل المحلي عبر WebSocket (`/voice_followup/stream`): السقف، VAD، المهلات |
| `VOICE_STREAM_AGC` | `1` | تطبيع مستوى صوت الزبون قبل VAD وWhisper (`VOICE_STREAM_AGC_TARGET_DBFS`=-20، `VOICE_STREAM_AGC_MAX_GAIN_DB`=20) |
| `VOICE_STREAM_WARMUP` | غير مضبوط = على GPU بس | `1`/`0`: تحميل Whisper وChatterbox عند إقلاع uvicorn (بعد جاهزية vLLM) بدل أول مكالمة |
| `GPU_MEMORY_UTILIZATION` | `0.85` | حصة vLLM من ذاكرة الكرت؛ الباقي لموديلات الصوت. `start.sh` يحترمه (انظر ميزانية الذاكرة بـ [voice-followup.md](docs/api/voice-followup.md)) |
| `TTS_POOL_SIZE` | `2` | عدد نسخ Chatterbox (~3–4.5GB لكل نسخة)؛ على A40 مشترك مع vLLM `1` |

الجدول الكامل لمتغيرات Catalog والوكيل المحلي: [voice-followup.md](docs/api/voice-followup.md).

## الرفع على RunPod

دليل كامل خطوة بخطوة مع حل المشاكل: [docs/deploy/runpod.md](docs/deploy/runpod.md). باختصار:

1. Pod بصورة **`ubuntu:22.04`**، Container Disk **60GB+**، وأضف `8000` إلى **Expose HTTP Ports**.
2. انسخ المشروع إلى `/workspace/back_end_iraqi` وعدّل `app/config.py` عند الحاجة (لا يلزم توكن Hugging Face).
3. `cd /workspace/back_end_iraqi && bash start.sh`
4. الرابط: `https://<POD_ID>-8000.proxy.runpod.net`

## الاختبارات

```bash
.venv/Scripts/python -m pytest -q
```

> **تنبيه أمني**: مفاتيح الخدمات ثابتة في `app/config.py` ومكررة في `docs/postman/collection.json` — لا تنشر المستودع علناً.
