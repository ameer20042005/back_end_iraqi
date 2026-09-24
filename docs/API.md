# توثيق API — back_end_iraqi

## الرابط الأساسي (Base URL)

- محلياً: `http://localhost:8000`
- على RunPod: `https://<POD_ID>-8000.proxy.runpod.net`

كل الأمثلة أدناه تفترض `http://localhost:8000` — بدّلها برابط الـ Pod الفعلي بعد الرفع.

## المصادقة — مفتاح API خاص لكل خدمة

كل خدمة محمية (المبيعات، إنشاء الطلبات، المتابعة الصوتية) تستخدم مفتاحها الخاص المستقل تماماً عن غيرها. المفتاح يُرسَل بهيدر HTTP:

```
X-API-Key: <المفتاح>
```

| الخدمة | النقاط المحمية | مصدر المفتاح | القيمة الثابتة الحالية |
|---|---|---|---|
| واجهة OpenAI | `POST /v1/chat/completions` | `openai_compat_api_key` في `app/config.py` | `sk-openai-7a9c2e4f6b1d8a0c3e5f7b9d1a3c5e7f` |
| المبيعات | `POST /sales/chat`, `POST /sales/chat/stream` | `sales_api_key` | `sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2` |
| إنشاء الطلبات | `POST /orders/create` | `orders_api_key` | `sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c` |
| المتابعة الصوتية | `POST /voice_followup/ask`, `POST /voice_followup/respond` | `voice_followup_api_key` | `sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b` |

- **النقاط المفتوحة بلا مفتاح**: `GET /health`, `GET /gpu`, `GET /`, `GET /docs`.
- **مفتاح خدمة لا يشتغل بخدمة ثانية** — كل خدمة تتحقق من مفتاحها هي حصراً (انظر `app/auth.py`).
- **القيم مكتوبة ثابتة بالكود** (`app/config.py`) لتشتغل فوراً بلا أي إعداد خارجي. لتغييرها، عدّل حقول المفاتيح في الملف نفسه ثم أعد تشغيل الخادم.

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

# متابعة صوتية (يرجع ملف WAV، انظر تفصيل كامل بقسم المتابعة الصوتية أدناه)
curl -X POST http://localhost:8000/voice_followup/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -d '{"order_id": "ORD-1001", "status": "ملغي"}' \
  -o question.wav
```

**أخطاء المصادقة:**

| الحالة | الاستجابة |
|---|---|
| الهيدر `X-API-Key` غير مرسَل إطلاقاً | `422 Unprocessable Entity` (FastAPI يرفض الطلب لغياب حقل إلزامي) |
| المفتاح مرسَل لكنه غير صحيح لهذي الخدمة | `401 Unauthorized` — `{"detail": "مفتاح API غير صحيح لخدمة <الاسم>"}` |
| مفتاح الخدمة غير مضبوط بالخادم إطلاقاً (فارغ) | `500 Internal Server Error` |

## واجهة تفاعلية جاهزة

`GET /docs` — Swagger UI يبني نفسه تلقائياً من الكود؛ لتجربة نقطة محمية منه اضغط زر **Authorize** وأدخل قيمة الهيدر `X-API-Key` المطابقة للخدمة. هذا الملف توثيق مرجعي إضافي (سياق الاستخدام، أمثلة، شكل الـ SSE).

**لوحة الاختبار الجاهزة** (`GET /test`، أو افتح `static/index.html` مباشرة): فيها خانات مفاتيح الخدمات المحمية معبّأة مسبقاً بنفس القيم الثابتة أعلاه — كل نقطة بالقائمة ترسل تلقائياً هيدر `X-API-Key` بالمفتاح المطابق لخدمتها. القيم تُحفظ بمتصفحك (`localStorage`) فتقدر تغيّرها بعد تعديل مفاتيح `app/config.py`.

---

## آلية المبيعات الحالية

المبيعات تستخدم native OpenAI tool calling بتوليد واحد. العميل يدير التاريخ وينفّذ الأدوات. [عقد المبيعات الكامل](sales-openai-compatible.md).

## أدوات الشحن في واجهة OpenAI

مسار `POST /v1/chat/completions` لا ينفذ أدوات الشحن داخل هذا الخادم. تطبيق العميل
يرسل تعريفات الأدوات ضمن `tools`، ينفذ الاستدعاء الناتج بهوية الموظف المخوّلة، ثم يعيد
نتيجته برسالة `role: "tool"`. أسماء الأدوات المعتمدة هي:

| الاسم | الغرض | المعاملات المطلوبة |
|---|---|---|
| `searchShipments` | البحث عن شحنات مطابقة لمعيار مثل رقم الوصل أو الهاتف أو الحالة. | معيار بحث واحد على الأقل، مثل `receiptNumber` أو `phone` |
| `countShipments` | إرجاع العدد الكلي للشحنات المطابقة لنفس فلاتر البحث، من دون اعتبار صفحة النتائج عدداً كاملاً. | فلتر واحد على الأقل، مثل `status` أو `phone` أو نطاق تاريخ |
| `getShipmentHistory` | إرجاع تسلسل مراحل شحنة واحدة، مع مددها وأسباب التأخير المتاحة. | `receiptNumber` أو معرّف الشحنة الذي يتوقعه تطبيق العميل |

هذه أسماء `function.name` حرفياً بحالة الأحرف نفسها. لا يرسل العميل إلا الأدوات التي
يستطيع تنفيذها؛ إذا لم يرسل أداة مناسبة، يطلب الوكيل معياراً أو يوضح أن البيانات غير
متاحة بدلاً من التخمين.

مثال لتعريف الأدوات الثلاث:

```json
[
  {"type":"function","function":{"name":"searchShipments","description":"Search authorized shipments","parameters":{"type":"object","properties":{"receiptNumber":{"type":"string"},"phone":{"type":"string"},"status":{"type":"string"}},"minProperties":1}}},
  {"type":"function","function":{"name":"countShipments","description":"Count authorized shipments matching filters","parameters":{"type":"object","properties":{"phone":{"type":"string"},"status":{"type":"string"},"fromDate":{"type":"string"},"toDate":{"type":"string"}},"minProperties":1}}},
  {"type":"function","function":{"name":"getShipmentHistory","description":"Get stage history for one authorized shipment","parameters":{"type":"object","properties":{"receiptNumber":{"type":"string"}},"required":["receiptNumber"]}}}
]
```

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

---

## وكيل المبيعات

POST /sales/chat/completions يستقبل model وmessages وtools ويرجع chat.completion. العميل يرسل تعريفات الأدوات وينفذها مثل `/v1/chat/completions`. المساران /sales/chat و/sales/chat/stream أسماء بديلة بنفس المدخل الجديد. [الحقول والأمثلة وترحيل العملاء](sales-openai-compatible.md).

## إنشاء طلب من نص/صوت/صورة

### `POST /orders/create`
`multipart/form-data` — **مدخل واحد بس** من الثلاثة، بدون محادثة (طلب مباشر).

| الحقل (form) | النوع | الوصف |
|---|---|---|
| `text` | string | نص مباشر يصف الطلب |
| `audio` | file | ملف صوتي (wav/mp3/m4a/ogg...) — يتحول لنص عربي تلقائياً (Whisper، نسخ عربي مُثبَّت لا اكتشاف تلقائي)، ثم يُستخرج منه الطلب. الملفات الأطول من 30 ثانية تُقطَّع تلقائياً فما يضيع منها شي |
| `image` | file | صورة (طلب مكتوب بخط اليد، لقطة شاشة محادثة، صورة منتج...) — تُقرأ مباشرة بقدرة الموديل البصرية الأصلية ويُستخرج منها الطلب **باستدعاء واحد** (بدون خطوة وصف نصي وسيطة) |

**مثال — نص:**
```bash
curl -X POST http://localhost:8000/orders/create -F "text=اريد لابتوب لينوفو وحبة ماوس لوجيتك"
```

**مثال — صوت:**
```bash
curl -X POST http://localhost:8000/orders/create -F "audio=@order.wav"
```

**مثال — صورة:**
```bash
curl -X POST http://localhost:8000/orders/create -F "image=@order.jpg"
```

**استجابة 200** (نفس شكل `order` بـ `/sales/chat` تماماً — كائن `OrderConfirmation` مباشرة، بدون تغليف):
```json
{
  "order_id": "9f5cfeae-4ffd-447e-8599-2ffa07625eba",
  "created_at": "2026-07-09T15:54:35.246950+00:00",
  "customer_name": null,
  "customer_phone": null,
  "customer_phone2": null,
  "customer_address": null,
  "customer_city": null,
  "customer_district": null,
  "state_code": null,
  "items": [
    {
      "product_id": "p003",
      "product_name": "ماوس لاسلكي لوجيتك",
      "quantity": 1,
      "unit_price": 15000.0,
      "currency": "IQD",
      "line_total": 15000.0,
      "matched": true
    }
  ],
  "suggested_product": null,
  "subtotal": 15000.0,
  "total": 15000.0,
  "currency": "IQD",
  "quoted_price": null,
  "notes": null,
  "confirmation_message": "تم تثبيت طلبك، وياتك بأقرب وقت ان شاء الله."
}
```

**ملاحظة — تصحيح المحافظة تلقائياً**: هذا المسار يستخدم برومت `plane.md` (بجذر المستودع) مع مرجع جغرافي من `states.xlsx`/`districts.xlsx` (18 محافظة، ~4900 منطقة → `app/rag/locations.json`). المناطق الواردة بنص الزبون تُطابَق قبل التوليد وتُحقن بالبرومت، وبعد الاستخراج إذا كانت المنطقة معروفة وتتبع محافظة واحدة تُعتمد محافظتها حتمياً بدل تخمين الموديل، ويُرجَع كودها بـ `state_code`. بعد أي تحديث لملفي الإكسل: `python -m app.rag.prepare_locations`.

**أخطاء محتملة:**

| كود | السبب | الرسالة |
|---|---|---|
| `400` | ما زوّدت أي مدخل، أو زوّدت أكثر من وحد | "زوّد مدخل واحد بس: text أو audio أو image." |
| `422` | ملف صوتي مو مفهوم/فاضي | "ما كدرنا نفهم أي كلام بالملف الصوتي." |
| `501` | مدخل `image`/`audio` بسيرفر ماعنده `transformers`/`torch`/`Pillow` مثبَّتة (يصير محلياً بدون GPU؛ ما لازم يصير على RunPod بعد تثبيت `requirements-gpu.txt`) | نص يوضح السبب |
| `503` | تحويل الصوت لنص غير متوفر بالسيرفر | "تحويل الصوت لنص غير متوفر محلياً..." |

**ملاحظة**: مدخل `image` يستخدم نفس محرك vLLM ونفس أوزان الموديل المستخدَمة بـ `/sales/chat` — ماكو موديل ثانٍ يتحمّل ولا استهلاك ذاكرة إضافي.

---

## المتابعة الصوتية للطلبات

مسار موجَّه لباك اند السستم (مو للزبون مباشرة): باك اند السستم يزوّدنا تفاصيل طلب بحالة معيّنة (ملغي، مرتجع...)، نولّد سؤالاً صوتياً عراقياً طبيعياً ونرجعه، باك اند السستم يشغّله للزبون ويسجّل رده، يرسل التسجيل لينا، نحلّل السبب ونرسله لباك اند السستم، ونرجع صوت شكر جاهز للزبون. **جسم الرد بكلا النقطتين ملف صوت WAV خام (`audio/wav`) — التفاصيل النصية (السؤال، النص المفرَّغ، الملخّص) توصل حصراً بهيدرات HTTP**، حتى يبقى جسم الرد صالحاً للتشغيل المباشر بلا أي تفكيك JSON مسبق.

الجلسة (`session_id`) تعيش بالذاكرة فقط بين `/ask` و`/respond` (بلا تخزين دائم)، صالحة **30 دقيقة** وتُستهلك مرة واحدة (`/respond` يحذفها فور القراءة، حتى لو نجح الطلب) — استدعاء ثانٍ بنفس `session_id` يرجع `404`.

⚠️ **هيدرات الرد نصوص عربية مرمَّزة percent-encoding (RFC 5987)** لأن هيدرات HTTP لازم Latin-1 فقط — فكّها بجهتك بـ `decodeURIComponent(...)` (JS) أو `urllib.parse.unquote(...)` (Python) قبل الاستخدام.

### `POST /voice_followup/ask`

**جسم الطلب** (`Content-Type: application/json`):
```json
{
  "order_id": "ORD-1001",
  "status": "ملغي",
  "customer_name": "أحمد",
  "customer_phone": "07701234567",
  "customer_city": "بغداد",
  "customer_district": "الكرادة",
  "customer_address": "قرب مطعم كذا",
  "items": [{"product_name": "لابتوب لينوفو IdeaPad 15", "quantity": 1}],
  "reason_hint": "رفض الزبون الاستلام",
  "notes": null
}
```

| الحقل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `order_id` | string | نعم | معرّف الطلب بنظام السستم |
| `status` | string | نعم | حالة الطلب الحالية بالعربي (مثلاً "ملغي"، "مرتجع"، "لم يتم التسليم") — هي المحرّك الرئيسي لنوع السؤال المولَّد، بلا قائمة ثابتة بجهتنا |
| `customer_name` / `customer_phone` / `customer_city` / `customer_district` / `customer_address` | string \| null | لا | بيانات الزبون من الطلب الأصلي — تُعاد لاحقاً كما هي مع نتيجة `/respond`، لا تُستنتَج من رد الزبون الصوتي |
| `items` | array of `{product_name, quantity}` | لا | عناصر الطلب (`quantity` افتراضياً `1`) |
| `reason_hint` | string \| null | لا | سبب أوّلي معروف بالسستم إن وُجد — يُستخدم كسياق إضافي بالسؤال |
| `notes` | string \| null | لا | ملاحظات إضافية |

**استجابة 200** — جسم صوت WAV خام + هيدرات:

| الهيدر | الوصف |
|---|---|
| `X-Session-Id` | مرّره كما هو لـ `POST /voice_followup/respond` |
| `X-Question-Text` | نص السؤال المولَّد (مرمَّز percent-encoding) — نفسه الذي حُوّل لصوت |

```bash
curl -X POST http://localhost:8000/voice_followup/ask \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -d '{"order_id": "ORD-1001", "status": "ملغي", "customer_name": "أحمد"}' \
  -o question.wav -D -
```

### `POST /voice_followup/respond`

`multipart/form-data` + معامل استعلام `session_id` (من `/ask`).

| المدخل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `session_id` | query param | نعم | من هيدر `X-Session-Id` بردّ `/ask` |
| `audio` | file (form) | نعم | تسجيل رد الزبون الصوتي — يتحول لنص عربي تلقائياً (نفس محرك Whisper المستخدَم بـ `/orders/create`) |

**استجابة 200** — جسم صوت شكر WAV خام + هيدرات:

| الهيدر | الوصف |
|---|---|
| `X-Reason-Summary` | ملخّص سبب الزبون كما فهمه الموديل (مرمَّز percent-encoding) |
| `X-Customer-Transcript` | نص رد الزبون كاملاً بعد تحويل الصوت لنص (مرمَّز percent-encoding) |
| `X-Query-Sent` | `"true"` لو انرسل الملخّص فعلاً لباك اند السستم، `"false"` لو فشل الإرسال (الصوت يرجع للزبون بكل الأحوال) |

```bash
curl -X POST "http://localhost:8000/voice_followup/respond?session_id=<من /ask>" \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -F "audio=@customer_reply.wav" \
  -o thanks.wav -D -
```

عند نجاح `/respond`، تُرسَل هذي الصيغة لباك اند السستم (`POST {SYSTEM_BACKEND_BASE_URL}/orders/{order_id}/feedback`، مسار مبدئي — TODO بالكود لحين توفر المسار الحقيقي):
```json
{
  "order_id": "ORD-1001",
  "status": "ملغي",
  "customer_name": "أحمد",
  "customer_phone": "07701234567",
  "customer_city": "بغداد",
  "customer_district": "الكرادة",
  "customer_address": "قرب مطعم كذا",
  "items": [{"product_name": "لابتوب لينوفو IdeaPad 15", "quantity": 1}],
  "reason_summary": "الزبون يقول التوصيل تأخر وقرر يلغي الطلب",
  "customer_transcript": "لا ماريده الحين، تأخر علي كثير..."
}
```

**أخطاء محتملة:**

| كود | السبب | الرسالة |
|---|---|---|
| `404` | `session_id` غير موجود أو منتهي (استُهلك بطلب `/respond` سابق، أو لم يصدر من `/ask` أصلاً) | "الجلسة غير موجودة أو انتهت صلاحيتها — استدعِ /ask من جديد." |
| `422` | ملف صوتي مو مفهوم/فاضي | "ما كدرنا نفهم أي كلام بالملف الصوتي." |
| `503` (على `/ask` أو `/respond`) | تحويل النص لصوت (`f5-tts`) أو الصوت لنص (`transformers`) غير مثبَّت محلياً | نص يوضح السبب |

**ملاحظة**: بدون محرك vLLM جاهز (محلياً بدون GPU)، السؤال والتحليل يرجعان بنصوص احتياطية عامة ثابتة بدل توليد حقيقي — الميزة تبقى تشتغل بالكامل (توليد صوت فعلي، تحويل صوت لنص فعلي) إلا خطوة صياغة النص بالموديل.

---

## صيغة `OrderConfirmation` (مشتركة بين `/sales/chat*` و`/orders/create`)

| الحقل | النوع | الوصف |
|---|---|---|
| `order_id` | string (UUID) | معرّف الطلب — يولَّد بالسيرفر، فريد لكل طلب |
| `created_at` | string (ISO 8601, UTC) | وقت تثبيت الطلب |
| `customer_name` | string \| null | اسم العميل إن ذُكر بالمحادثة/النص |
| `customer_phone` | string \| null | رقم الهاتف إن ذُكر |
| `customer_phone2` | string \| null | رقم هاتف ثانٍ إن ذُكر (`/orders/create` فقط) |
| `customer_address` | string \| null | العنوان إن ذُكر (بـ `/orders/create`: "محافظة - منطقة - تفصيل") |
| `customer_city` | string \| null | المحافظة بالاسم الرسمي بقاعدة بيانات شركة التوصيل — تُصحَّح تلقائياً من مرجع المناطق (states.xlsx/districts.xlsx) |
| `customer_district` | string \| null | المنطقة/الحي كما وردت برسالة الزبون |
| `state_code` | string \| null | كود المحافظة بنظام شركة التوصيل (`BGD`, `BAS`...) |
| `quoted_price` | string \| null | السعر كما ورد برسالة الزبون — للاطلاع فقط، لا يدخل بحساب `total` |
| `items` | array of `ResolvedOrderItem` | عناصر الطلب بعد مطابقتها بالكتالوج |
| `suggested_product` | object \| null | `{id, name, price, currency}` — المنتج الإضافي المقترَح إن وافق عليه العميل |
| `subtotal` / `total` | number \| null | مجموع أسعار العناصر المطابَقة فقط (`matched: true`) — محسوبة بالسيرفر من الكتالوج، مو من الموديل. `null` إذا ماكو أي عنصر مطابق (يعني "السعر غير معروف" — المرجع حينها `quoted_price` — وليس "مجاني") |
| `currency` | string \| null | عملة الأسعار (مثلاً `"IQD"`) |
| `notes` | string \| null | ملاحظات إضافية من العميل |
| `confirmation_message` | string | جملة تأكيد للعميل باللهجة العراقية |

### عنصر `ResolvedOrderItem`

| الحقل | النوع | الوصف |
|---|---|---|
| `product_id` | string \| null | معرّف المنتج بالكتالوج — `null` لو ما انطبق |
| `product_name` | string | اسم المنتج كما فهمه الموديل، أو الاسم الفعلي بالكتالوج لو انطبق |
| `quantity` | int | الكمية |
| `unit_price` / `line_total` | number \| null | `null` لو ما انطبق على منتج بالكتالوج |
| `matched` | bool | هل انطبق الصنف على منتج بالكتالوج. `false` **مو خطأ ولا رفض** — الطلب يُقبل لأي منتج حتى لو مو بالكتالوج، ويُمرَّر باسمه وكميته كما وردا؛ يعني فقط إن السعر ما جا من الكتالوج (شوف `quoted_price`). |

---

## ملاحظات نشر على RunPod

- كل النقاط تشتغل محلياً بوضع "fallback" (بدون GPU) — `engine: "fallback"` بالرد، والردود تبدأ بـ `[وضع محلي بدون GPU]`. هذا طبيعي ومتوقّع، ومفيد لاختبار شكل الـ API قبل الرفع.
- على RunPod (بعد ضبط `hf_token` الصحيح في `app/config.py` وتشغيل `start.sh`/الـ Docker image)، تتحول تلقائياً لـ `engine: "vllm"` بدون أي تغيير بالكود أو بشكل الطلبات/الاستجابات.
- أول طلب بعد الإقلاع قد ياخذ وقت أطول (تحميل الموديل + المحوّل من Hugging Face أول مرة) — الطلبات اللاحقة أسرع.
