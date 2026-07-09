# توثيق API — back_end_iraqi

## الرابط الأساسي (Base URL)

- محلياً: `http://localhost:8000`
- على RunPod: `https://<POD_ID>-8000.proxy.runpod.net`

كل الأمثلة أدناه تفترض `http://localhost:8000` — بدّلها برابط الـ Pod الفعلي بعد الرفع.

## المصادقة

ماكو مصادقة حالياً — كل النقاط مفتوحة.

## واجهة تفاعلية جاهزة

`GET /docs` — Swagger UI يبني نفسه تلقائياً من الكود، تكدر تجرب كل نقطة منه مباشرة بالمتصفح. هذا الملف توثيق مرجعي إضافي (سياق الاستخدام، أمثلة، شكل الـ SSE).

---

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

### `POST /sales/chat`
رد كامل (بدون بث). يحاول يقنع العميل بالشراء، يقترح منتج إضافي، ويثبّت الطلب تلقائياً (`order`) لما العميل يوافق صراحة.

**جسم الطلب:**
```json
{
  "message": "شنو عندكم لابتوبات؟",
  "session_id": null,
  "max_tokens": null,
  "temperature": null
}
```

| الحقل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `message` | string | نعم | رسالة العميل |
| `session_id` | string \| null | لا | لاستمرار نفس المحادثة؛ اتركه فارغ أول مرة وخزّن القيمة اللي ترجع لك واستخدمها بالطلبات التالية |
| `max_tokens` | int \| null | لا | يتجاوز `MAX_NEW_TOKENS` الافتراضي لهذا الطلب فقط |
| `temperature` | float \| null | لا | يتجاوز `TEMPERATURE` الافتراضي لهذا الطلب فقط |

**استجابة 200 (بدون تثبيت طلب):**
```json
{
  "session_id": "6ca92bdb-98fd-4843-a1e1-824b736c8587",
  "answer": "عندنا لابتوب لينوفو IdeaPad 15 بسعر 750000 دينار...",
  "order": null,
  "sources": {"words": [...], "products": [...]},
  "engine": "vllm"
}
```

**استجابة 200 (العميل وافق على الشراء — `order` معبّى):**
```json
{
  "session_id": "6ca92bdb-98fd-4843-a1e1-824b736c8587",
  "answer": "زين، ثبّتلك الطلب...",
  "order": {
    "order_id": "068e8271-3bf3-43c5-8958-f3353a8472f3",
    "created_at": "2026-07-09T16:28:56.225180+00:00",
    "customer_name": null,
    "customer_phone": null,
    "customer_address": null,
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
    "notes": null,
    "confirmation_message": "تم تثبيت طلبك، وياتك بأقرب وقت ان شاء الله."
  },
  "sources": {"words": [...], "products": [...]},
  "engine": "vllm"
}
```

| الحقل | النوع | الوصف |
|---|---|---|
| `session_id` | string | نفسه لو أرسلته، أو معرّف جديد تولّد تلقائياً |
| `answer` | string | رد الوكيل للعميل (نص المحادثة العادي، بدون أي علامات داخلية) |
| `order` | object \| null | `null` إلا لو العميل أكّد الشراء بنفس هذا الرد — عندها كائن `OrderConfirmation` كامل (تفصيله بالأسفل) |
| `sources` | object | `{"words": [...], "products": [...]}` — نتائج RAG المستخدَمة ببناء الرد (للتصحيح/الشفافية، اختياري عرضها بالواجهة) |
| `engine` | string | `"vllm"` (توليد حقيقي) أو `"fallback"` (محلياً بدون GPU) |

**ملاحظة مهمة**: `order` يظهر فقط بالرد اللي فيه العميل أكّد الشراء صراحة. أي رد بعده (لو رجع يسأل شي ثاني بنفس الجلسة) يرجّع `order: null` من جديد.

### `POST /sales/chat/stream`
نفس المدخل بالضبط، لكن بث Server-Sent Events (`Content-Type: text/event-stream`) بدل انتظار الرد كامل — يظهر أول جزء من كلام الوكيل بسرعة.

**تدفق الأحداث:**
```
data: {"delta": "عند"}

data: {"delta": "نا لابتوب"}

data: {"delta": " لينوفو..."}

data: {"done": true, "session_id": "...", "sources": {"words": [...], "products": [...]}, "order": null}

```

- كل حدث `delta` يحمل جزء نصي إضافي — اجمعهم بالترتيب للحصول على الرد الكامل.
- الحدث الأخير دايماً `{"done": true, ...}` ويحمل `order` (نفس شكل `/sales/chat` — `null` أو كائن `OrderConfirmation` كامل).

**مثال عميل (JavaScript، `fetch` + `ReadableStream`، أو `EventSource` لو عدّلت الطلب لـ GET — حالياً POST فتحتاج `fetch`):**
```js
const res = await fetch("http://localhost:8000/sales/chat/stream", {
  method: "POST",
  headers: {"Content-Type": "application/json"},
  body: JSON.stringify({message: "شلونكم؟", session_id: sessionId}),
});
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
while (true) {
  const {done, value} = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, {stream: true});
  for (const line of buffer.split("\n\n")) {
    if (!line.startsWith("data: ")) continue;
    const event = JSON.parse(line.slice(6));
    // event.delta أو event.done
  }
}
```

---

## دعم العملاء

### `POST /support/chat`
تتبع حالة طلب (برقم الطلب أو رقم الهاتف)، أو أي سؤال عام (يستخدم بحث ويب تلقائياً).

**جسم الطلب:**
```json
{"message": "وين طلبي ORD-1001؟", "session_id": null}
```

| الحقل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `message` | string | نعم | رسالة العميل |
| `session_id` | string \| null | لا | نفس فكرة `/sales/chat` |

**استجابة 200:**
```json
{
  "session_id": "edcfa5ad-18c3-4995-a96f-fb8ff9b3bf26",
  "answer": "طلبك ORD-1001 حالته: قيد التوصيل، متوقع يوصلك خلال يومين.",
  "engine": "vllm"
}
```

بيانات الطلبات نفسها (Mock حالياً — `app/order_gateway.py`) بصيغة:
```json
{
  "order_id": "ORD-1001",
  "phone": "07701234567",
  "status": "قيد التوصيل",
  "items": [{"product_name": "لابتوب لينوفو IdeaPad 15", "quantity": 1}],
  "eta": "خلال يومين"
}
```

---

## إنشاء طلب من نص/صوت/صورة

### `POST /orders/create`
`multipart/form-data` — **مدخل واحد بس** من الثلاثة، بدون محادثة (طلب مباشر).

| الحقل (form) | النوع | الوصف |
|---|---|---|
| `text` | string | نص مباشر يصف الطلب |
| `audio` | file | ملف صوتي (wav/mp3/m4a...) — يتحول لنص تلقائياً (Whisper) |
| `image` | file | صورة (طلب مكتوب بخط اليد، لقطة شاشة محادثة، صورة منتج...) — توصف عبر قدرة الموديل البصرية الأصلية، ثم يُستخرج الطلب من الوصف |

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
  "customer_address": null,
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
  "notes": null,
  "confirmation_message": "تم تثبيت طلبك، وياتك بأقرب وقت ان شاء الله."
}
```

**أخطاء محتملة:**

| كود | السبب | الرسالة |
|---|---|---|
| `400` | ما زوّدت أي مدخل، أو زوّدت أكثر من وحد | "زوّد مدخل واحد بس: text أو audio أو image." |
| `422` | ملف صوتي مو مفهوم/فاضي | "ما كدرنا نفهم أي كلام بالملف الصوتي." |
| `501` | مدخل `image`/`audio` بسيرفر ماعنده `transformers`/`torch`/`Pillow` مثبَّتة (يصير محلياً بدون GPU؛ ما لازم يصير على RunPod بعد تثبيت `requirements-gpu.txt`) | نص يوضح السبب |
| `503` | تحويل الصوت لنص غير متوفر بالسيرفر | "تحويل الصوت لنص غير متوفر محلياً..." |

**ملاحظة**: مدخل `image` يستخدم نفس محرك vLLM ونفس أوزان الموديل المستخدَمة بـ `/sales/chat`/`/support/chat` — ماكو موديل ثانٍ يتحمّل ولا استهلاك ذاكرة إضافي.

---

## صيغة `OrderConfirmation` (مشتركة بين `/sales/chat*` و`/orders/create`)

| الحقل | النوع | الوصف |
|---|---|---|
| `order_id` | string (UUID) | معرّف الطلب — يولَّد بالسيرفر، فريد لكل طلب |
| `created_at` | string (ISO 8601, UTC) | وقت تثبيت الطلب |
| `customer_name` | string \| null | اسم العميل إن ذُكر بالمحادثة/النص |
| `customer_phone` | string \| null | رقم الهاتف إن ذُكر |
| `customer_address` | string \| null | العنوان إن ذُكر |
| `items` | array of `ResolvedOrderItem` | عناصر الطلب بعد مطابقتها بالكتالوج |
| `suggested_product` | object \| null | `{id, name, price, currency}` — المنتج الإضافي المقترَح إن وافق عليه العميل |
| `subtotal` / `total` | number \| null | مجموع أسعار العناصر المطابَقة فقط (`matched: true`) — محسوبة بالسيرفر من الكتالوج، مو من الموديل |
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
| `matched` | bool | **مهم**: لو `false`، يعني المنتج المطلوب ما انطبق على أي شي بالكتالوج الحالي — يحتاج مراجعة يدوية قبل التنفيذ الفعلي، لا تعتمد على السعر/المجموع بهذا الطلب. |

---

## ملاحظات نشر على RunPod

- كل النقاط تشتغل محلياً بوضع "fallback" (بدون GPU) — `engine: "fallback"` بالرد، والردود تبدأ بـ `[وضع محلي بدون GPU]`. هذا طبيعي ومتوقّع، ومفيد لاختبار شكل الـ API قبل الرفع.
- على RunPod (بعد `HF_TOKEN` صحيح وتشغيل `start.sh`/الـ Docker image)، تتحول تلقائياً لـ `engine: "vllm"` بدون أي تغيير بالكود أو بشكل الطلبات/الاستجابات.
- أول طلب بعد الإقلاع قد ياخذ وقت أطول (تحميل الموديل + المحوّل من Hugging Face أول مرة) — الطلبات اللاحقة أسرع.
