# نظرة عامة على الـ API

> ⚠️ **تنبيه أمني**: هذا الملف يحتوي على مفاتيح API الحقيقية الحالية للخادم (لأنها القيم
> الافتراضية المضبوطة فعلياً بـ `app/config.py`). لا تشارك هذا
> الملف أو تنشره علناً — أي شخص يملك المفتاح يقدر يستهلك الخدمة المرتبطة به مباشرة.
> إذا احتجت مشاركته مع طرف ثالث، بدّل قيم `*_api_key` في `app/config.py` أولاً.

## الرابط الأساسي (Base URL)

- محلياً: `http://localhost:8000`
- على RunPod: `https://<POD_ID>-8000.proxy.runpod.net`

كل الأمثلة أدناه تفترض `http://localhost:8000` — بدّلها برابط الـ Pod الفعلي بعد الرفع.
- الـ Pod الحالي: `https://1spx1ivrqcvt1h-8000.proxy.runpod.net`

## المصادقة — مفتاح API خاص لكل خدمة

كل خدمة محمية (المبيعات، إنشاء الطلبات، المتابعة الصوتية، تصحيح المناطق) تستخدم مفتاحها الخاص المستقل تماماً عن غيرها. المفتاح يُرسَل بهيدر HTTP:

```
X-API-Key: <المفتاح>
```

| الخدمة | النقاط المحمية | مصدر المفتاح | القيمة الثابتة الحالية |
|---|---|---|---|
| واجهة OpenAI | `POST /v1/chat/completions` | `openai_compat_api_key` في `app/config.py` | `sk-openai-7a9c2e4f6b1d8a0c3e5f7b9d1a3c5e7f` |
| المبيعات | `POST /sales/chat`, `POST /sales/chat/stream` | `sales_api_key` | `sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2` |
| إنشاء الطلبات | `POST /orders/create` | `orders_api_key` | `sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c` |
| المكالمات الصوتية | `POST /voice_followup/calls`, `GET /voice_followup/calls/{room}/outcome`, `POST /voice_followup/operator/chat/completions` | `voice_followup_api_key` | `sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b` |
| تصحيح المناطق | `POST /v1/district-correction` | `district_api_key` | `sk-district-7ea5de0fb1b68f0283df460dea42b2df` |

- **تصحيح المناطق**: `POST /v1/district-correction` يستخدم مفتاحاً مستقلاً `district_api_key`؛ [العقد الكامل وPostman](district-correction.md).
- **النقاط المفتوحة بلا مفتاح**: `GET /health`, `GET /gpu`, `GET /`, `GET /docs`, `GET /v1/district-correction/ready`.
- **مفتاح خدمة لا يشتغل بخدمة ثانية** — كل خدمة تتحقق من مفتاحها هي حصراً (انظر `app/auth.py`).
- كل مفاتيح الخدمات، بما فيها `district_api_key`، معرفة في `app/config.py`.

**أمثلة استدعاء:**

```bash
# OpenAI-compatible
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sk-openai-7a9c2e4f6b1d8a0c3e5f7b9d1a3c5e7f" \
  -d '{"model":"jbot","messages":[{"role":"user","content":"هلا"}]}'

# مبيعات
curl -X POST http://localhost:8000/sales/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2" \
  -d '{"model":"sales","messages":[{"role":"user","content":"شنو عندكم لابتوبات؟"}]}'

# إنشاء طلب (multipart)
curl -X POST http://localhost:8000/orders/create \
  -H "X-API-Key: sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c" \
  -F "text=أريد لابتوب لينوفو"

# بدء مكالمة صوتية حية (LiveKit) — انظر voice-followup.md
curl -X POST http://localhost:8000/voice_followup/calls \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -d '{"company_name": "متجر النور", "goal": "اسأل الزبون عن رأيه بالطلب", "contact": {"phone": "07701234567"}, "reference_id": "ORD-1001"}'
```

**أخطاء المصادقة:**

| الحالة | الاستجابة |
|---|---|
| الهيدر `X-API-Key` غير مرسَل إطلاقاً | `422 Unprocessable Entity` (FastAPI يرفض الطلب لغياب حقل إلزامي) |
| المفتاح مرسَل لكنه غير صحيح لهذي الخدمة | `401 Unauthorized` — `{"detail": "مفتاح API غير صحيح لخدمة <الاسم>"}` |
| مفتاح الخدمة غير مضبوط بالخادم إطلاقاً (فارغ) | `500 Internal Server Error` |

## طريقة الإرسال — القاعدة العامة لكل الطلبات

كل طلب لأي مسار من الثلاثة **يجب** أن يحمل هيدر المفتاح دائماً:

```
X-API-Key: <مفتاح الخدمة المستهدفة>
```

لكن **نوع جسم الطلب يختلف حسب المسار** — هذا أهم شي ينتبهله مطوّر الباك اند الخارجي قبل الربط:

| المسار | نوع الجسم (Content-Type) | ليش |
|---|---|---|
| `POST /orders/create` | `multipart/form-data` ⚠️ **ليس JSON** | حتى لو المدخل نص فقط (`text`)، لازم يُرسل كحقل Form جوّا multipart — لأن نفس المسار يقبل بديلاً ملف صوت أو صورة |
| `POST /sales/chat` | `application/json` ✅ | جسم JSON عادي بالكامل |
| `POST /sales/chat/stream` | `application/json` ✅ | نفس جسم `/sales/chat` تماماً، فقط الاستجابة تختلف (تدفّق لا JSON واحد) |
| `POST /voice_followup/operator/chat/completions` | `application/json` ✅ | عقد OpenAI (`model`, `messages`, `tools`) + `company_name` اختياري |
| `POST /voice_followup/calls` | `application/json` ✅ | `company_name` + `goal` (+ `questions`, `context`, `contact`, `reference_id`...) — نفس وسائط أداة `start_call` |

**خلاصة للمطوّر:** كل المسارات تستقبل **JSON خالص** مع `Content-Type: application/json`، إلا `/orders/create` اللي يحتاج **رفع ملف** (صوت/صورة) فيكون حصراً `multipart/form-data`. هيدر `X-API-Key` موجود بكل الحالات بلا استثناء.

## واجهة تفاعلية جاهزة

`GET /docs` — Swagger UI يبني نفسه تلقائياً من الكود؛ لتجربة نقطة محمية منه اضغط زر **Authorize** وأدخل قيمة الهيدر `X-API-Key` المطابقة للخدمة. هذا الملف توثيق مرجعي إضافي (سياق الاستخدام، أمثلة، شكل الـ SSE).

**لوحة الاختبار الجاهزة** (`GET /test`، أو افتح `static/index.html` مباشرة): فيها خانات مفاتيح الخدمات المحمية معبّأة مسبقاً بنفس القيم الثابتة أعلاه — كل نقطة بالقائمة ترسل تلقائياً هيدر `X-API-Key` بالمفتاح المطابق لخدمتها. القيم تُحفظ بمتصفحك (`localStorage`) فتقدر تغيّرها بعد تعديل مفاتيح `app/config.py`.

## توثيق كل ميزة

| الميزة | الملف |
|---|---|
| إنشاء الطلبات `/orders/create` | [orders.md](orders.md) |
| المبيعات `/sales/*` | [sales.md](sales.md) |
| واجهة OpenAI `/v1/chat/completions` | [openai-compat.md](openai-compat.md) |
| المكالمات الصوتية الحية `/voice_followup/*` | [voice-followup.md](voice-followup.md) |
| تصحيح المناطق `/v1/district-correction` | [district-correction.md](district-correction.md) |
| مجموعة Postman | [../postman/README.md](../postman/README.md) |

## فحص الحالة

### `GET /health`
فحص صحة بسيط.

**استجابة 200:**
```json
{"status": "healthy"}
```

### `GET /gpu`
معلومات GPU/CUDA وحالة محرك vLLM — للتأكد إن الموديل شغال فعلاً على RunPod.

**استجابة 200 (على RunPod مع GPU):**
```json
{
  "torch": "2.8.0",
  "cuda": true,
  "vllm_ready": true,
  "device_count": 1,
  "device_name": "NVIDIA A100-SXM4-80GB",
  "cuda_version": "12.8",
  "vram_total_gb": 80.0,
  "vram_free_gb": 62.3
}
```

**استجابة 200 (محلياً بدون GPU):**
```json
{"torch": null, "cuda": false, "note": "torch غير مثبت محلياً"}
```

### `GET /`
معلومات عامة عن الخدمة.
```json
{"status": "ok", "service": "back_end_iraqi", "docs": "/docs"}
```

## ملخص سريع لكل النقاط

| الميزة | Endpoint | نوع الجسم | المفتاح |
|---|---|---|---|
| إنشاء الطلبات | `POST /orders/create` | `multipart/form-data` (حقل واحد: `text` أو `audio` أو `image`) | `sk-orders-...` |
| المبيعات (رد كامل) | `POST /sales/chat` | `application/json` (`model`, `messages`, `tools`) | `sk-sales-...` |
| المبيعات (بث) | `POST /sales/chat/stream` | `application/json` (نفس أعلاه) | `sk-sales-...` |
| مساعد الموظف (اتصل بهؤلاء) | `POST /voice_followup/operator/chat/completions` | `application/json` (`model`, `messages`, `tools`, `company_name`) | `sk-voicefu-...` |
| بدء مكالمة حية (LiveKit) | `POST /voice_followup/calls` | `application/json` (`company_name`, `goal`, `contact`...) | `sk-voicefu-...` |
| نتيجة مكالمة (سحب) | `GET /voice_followup/calls/{room}/outcome` | — | `sk-voicefu-...` |

## ملاحظات نشر على RunPod

- كل النقاط تشتغل محلياً بوضع "fallback" (بدون GPU) — `engine: "fallback"` بالرد، والردود تبدأ بـ `[وضع محلي بدون GPU]`. هذا طبيعي ومتوقّع، ومفيد لاختبار شكل الـ API قبل الرفع.
- على RunPod (بعد تشغيل `start.sh` — بلا حاجة لتوكن Hugging Face)، تتحول تلقائياً لـ `engine: "vllm"` بدون أي تغيير بالكود أو بشكل الطلبات/الاستجابات.
- أول طلب بعد الإقلاع قد ياخذ وقت أطول (تحميل الموديل + المحوّل من Hugging Face أول مرة) — الطلبات اللاحقة أسرع.
