# توثيق API — back_end_iraqi

## الرابط الأساسي (Base URL)

- محلياً: `http://localhost:8000`
- على RunPod: `https://<POD_ID>-8000.proxy.runpod.net`

كل الأمثلة أدناه تفترض `http://localhost:8000` — بدّلها برابط الـ Pod الفعلي بعد الرفع.

## المصادقة — مفتاح API خاص لكل خدمة

كل خدمة (المبيعات، الدعم، إنشاء الطلبات، المتابعة الصوتية) محمية بمفتاحها الخاص المستقل تماماً عن غيرها. المفتاح يُرسَل بهيدر HTTP:

```
X-API-Key: <المفتاح>
```

| الخدمة | النقاط المحمية | المتغير (app/config.py) | القيمة الثابتة الحالية |
|---|---|---|---|
| المبيعات | `POST /sales/chat`, `POST /sales/chat/stream` | `sales_api_key` | `sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2` |
| الدعم | `POST /support/chat` | `support_api_key` | `sk-support-7a9c2e4f6b1d8a0c3e5f7b9d1a3c5e7f` |
| إنشاء الطلبات | `POST /orders/create` | `orders_api_key` | `sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c` |
| المتابعة الصوتية | `POST /voice_followup/ask`, `POST /voice_followup/respond` | `voice_followup_api_key` | `sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b` |

- **النقاط المفتوحة بلا مفتاح**: `GET /health`, `GET /gpu`, `GET /`, `GET /docs`.
- **مفتاح خدمة لا يشتغل بخدمة ثانية** — مفتاح المبيعات مرفوض على `/support/chat` وبالعكس، كل خدمة تتحقق من مفتاحها هي حصراً (انظر `app/auth.py`).
- **القيم مكتوبة ثابتة بالكود** (`app/config.py`) لتشتغل فوراً بلا أي إعداد `.env` — لو تحتاج قيماً مختلفة (مثلاً بيئة إنتاج منفصلة)، عرّف نفس الأسماء بمتغيرات بيئة (`SALES_API_KEY`, `SUPPORT_API_KEY`, `ORDERS_API_KEY`, `VOICE_FOLLOWUP_API_KEY`) وهي تتجاوز الثابتة بالكود تلقائياً (`.env` أو Environment Variables بإعدادات RunPod).

**أمثلة استدعاء:**

```bash
# مبيعات
curl -X POST http://localhost:8000/sales/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2" \
  -d '{"message": "شنو عندكم لابتوبات؟"}'

# دعم
curl -X POST http://localhost:8000/support/chat \
  -H "Content-Type: application/json" \
  -H "X-API-Key: sk-support-7a9c2e4f6b1d8a0c3e5f7b9d1a3c5e7f" \
  -d '{"message": "وين طلبي ORD-1001؟"}'

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

**لوحة الاختبار الجاهزة** (`GET /test`، أو افتح `static/index.html` مباشرة): فيها ثلاث خانات إدخال بالشريط الجانبي (مفتاح المبيعات/الدعم/إنشاء الطلبات) معبّأة مسبقاً بنفس القيم الثابتة أعلاه — كل نقطة بالقائمة ترسل تلقائياً هيدر `X-API-Key` بالمفتاح المطابق لخدمتها. القيم تُحفظ بمتصفحك (`localStorage`) فتقدر تغيّرها لو بدّلت المفاتيح بـ.env.

---

## آلية عمل الموديل داخلياً — مخطط JSON صارم لكل رد (tool_call / final_answer)

**هذا داخلي بحت وما يظهر بجسم استجابة `/sales/chat` أو `/support/chat`** — العميل المستهلك للـ API يشوف فقط `answer` النهائي كنص عادي. القسم هذا يشرح كيف يقرر النموذج نفسه، بجهة الخادم، متى يحتاج بيانات (منتج، حالة طلب) قبل ما يصيغ الجواب — لفهم دقيق لسلوك النظام ولو احتجت تبني/تصحّح أدوات جديدة (`app/tool_loop.py`).

بدل الاعتماد على tool-calling الأصلي لأي محرك، كل استدعاء توليد بميزتَي `sales`/`support` مقيَّد بـ **guided decoding** (`response_format.json_schema` بجهة vLLM — انظر `app/engine.py`) بهذا المخطط الصارم:

```json
{
  "response_format": {
    "type": "json_schema",
    "schema": {
      "type": "object",
      "properties": {
        "action": {"enum": ["tool_call", "final_answer"]},
        "tool_call": {
          "type": "object",
          "properties": {
            "tool": {"type": "string"},
            "args": {"type": "object"}
          },
          "required": ["tool"]
        },
        "final_answer": {"type": "string"}
      },
      "required": ["action"]
    }
  }
}
```

vLLM يقيّد التوليد بهذا المخطط فعلياً (guided decoding) — الموديل **لا يقدر** يخرج JSON خارج هذا الشكل، بعكس البروتوكول النصي القديم (`[TOOL_CALL]{...}[/TOOL_CALL]`) اللي كان عرضة لانحراف الموديل عن الصيغة.

### كيف تُستهلك النتيجة (`app/tool_loop.py::run_with_tools`)

1. الموديل يولّد رداً واحداً مطابقاً للمخطط أعلاه.
2. **إذا `action == "tool_call"`**: الباك اند يقرأ `tool_call.tool` (اسم الأداة) و`tool_call.args` (معاملاتها)، ينفّذ الأداة المطابقة فعلياً (استعلام حقيقي — مثل `search_products` أو `get_order_status`)، ويضيف نتيجتها كرسالة جديدة بالمحادثة، ثم يعيد التوليد بجولة ثانية (حتى `max_rounds`، افتراضياً 3).
3. **إذا `action == "final_answer"`**: `final_answer` هو النص الذي يصل للعميل فعلياً بحقل `answer` بجسم الاستجابة — تنتهي الحلقة.

**مثال `tool_call` فعلي** (النموذج يطلب بحث منتج بالمبيعات):
```json
{
  "action": "tool_call",
  "tool_call": {"tool": "search_products", "args": {"query": "لابتوب", "top_k": 5}}
}
```

**مثال `final_answer` فعلي** (بعد ما استلم نتيجة الأداة):
```json
{
  "action": "final_answer",
  "final_answer": "عدنا لابتوب لينوفو IdeaPad 15 بسعر 750,000 دينار، شنو رأيك؟"
}
```

### الأدوات المسجَّلة فعلياً بكل ميزة

| الميزة | الأداة (`tool`) | التنفيذ الفعلي |
|---|---|---|
| المبيعات | `search_products` | `app/tools/products.py::search_products_tool` — استعلام حي على باك اند السستم (`args.query`, `args.top_k`) |
| الدعم | `get_order_status` | `app/features/support/router.py::_get_order_status_tool` — استعلام حي (`args.order_id` أو `args.phone` أو `args.status` أو `args.all`) |

النموذج **ما يفترض وجود أدوات أخرى غير المسجَّلة**؛ لو رجّع `tool_call.tool` باسم غير معروف، الباك اند يرجّع `{"error": "أداة غير معروفة: ..."}` كنتيجة، والموديل يكمل الحلقة (بدل ما ينهار).

### حقل إضافي بالمبيعات: `order_ready`

مخطط `/sales/chat` يوسّع المخطط الأساسي بحقل إضافي واحد (عبر `app.tool_loop.build_schema`):

```json
{
  "action": "final_answer",
  "final_answer": "زين، ثبّتلك الطلب — تأكدلي الاسم والهاتف والعنوان صح؟",
  "order_ready": false
}
```

`order_ready: true` يعني الموديل قرر إن العميل أكّد الشراء صراحةً بعد ملخّص الطلب — هذا **يحل محل** علامة `[ORDER_READY]` النصية القديمة. القرار النهائي بتثبيت الطلب فعلياً لا يعتمد على هذا الحقل وحده: بوابة حتمية منفصلة (`_missing_order_fields`، انظر `app/features/sales/router.py`) تتحقق أن الاسم/الهاتف/العنوان مذكورة فعلاً بكلام العميل قبل قبول `order_ready=true` — لو نقص أي حقل، الطلب لا يُثبَّت مهما رجّع الموديل.

---

## عقد باك اند السستم — الشكل الرسمي الثابت لاستجابات المنتجات والطلبات

**هذا القسم موجّه لفريق باك اند السستم** (وليس للعميل المستهلك لـ `/sales/chat` أو `/support/chat`): يوثّق الشكل الرسمي المطلوب من نقاط باك اند السستم الخمس (`GET /products/search`، `GET /products/{id}`، `GET /orders/{order_id}`، `GET /orders/search`، `GET /orders`) — نفس العقد يُستخدم من **الميزات الثلاث بلا استثناء**: المبيعات (`search_products`)، الدعم (`get_order_status`)، والمتابعة الصوتية (تستهلك حقول الطلب نفسها عبر `VoiceFollowupOrderRequest`، انظر `app/features/voice_followup/schema.py`).

### لماذا هذا العقد أُضيف

قبله، `app/products.py`/`app/order_gateway.py` كانا يمرران `resp.json()` كما هو بلا أي تحقق برمجي — أي شكل يرجعه باك اند السستم فعلياً يمر للموديل حرفياً. الموديل **يقرأ** أي JSON كنص عربي عادي، لكن هذا لا يعني أنه **يتحقق** من اكتماله أو صحته؛ الانضباط الوحيد كان تعليمات البرومبت ("الأسعار والأرقام حصراً من نتيجة search_products كما هي حرفياً" — `app/features/sales/prompts.py`)، بلا أي شبكة أمان برمجية لو تغيّر شكل الحقول فعلياً. الآن كل استجابة تمر عبر Pydantic (`app/system_backend_schema.py`) قبل ما تصل لأي أداة أو مسار حتمي.

### مصدر أسماء الحقول

مبنية على أعمدة `catalog.products` / `catalog.stock_info` و`catalog.sells` / `catalog.sell_items` **الحقيقية** كما موثّقة بـ [assets/JENNI_STORES_SCHEMA_FOR_AI_QUERY_BUILDER (1).md](<assets/JENNI_STORES_SCHEMA_FOR_AI_QUERY_BUILDER (1).md>) — وليست أسماء مخترعة. مسطّحة لصيغة REST بسيطة بدل انعكاس الـ joins الداخلية حرفياً.

### `SystemProduct` — عنصر واحد بـ `results` (`GET /products/search`) أو جسم `GET /products/{id}`

| الحقل | النوع | إلزامي | يقابل عمود قاعدة البيانات | الوصف |
|---|---|---|---|---|
| `id` | string | **نعم** | `products.id` | معرّف المنتج |
| `name` | string | **نعم** | `products.name` | اسم المنتج — يُذكر حرفياً بالرد |
| `sku` | string \| null | لا | `products.sku` | رمز المخزون |
| `barcode` | string \| null | لا | `products.barcode` | الباركود |
| `description` | string \| null | لا | `products.description` | وصف — الموديل يستخرج منه المواصفات المذكورة بالرد فقط |
| `category` | string \| null | لا | `categories.pretty_name` (عبر `product_categories`) | اسم الفئة — نفس ما يُمرَّر بفلتر `args.category` بأداة `search_products` |
| `price` | number \| null | لا | سعر البيع (`products`) | numeric(19,2) — **المصدر الوحيد المسموح للأسعار بالرد** |
| `currency` | string \| null | لا (افتراضي `"IQD"`) | — | رمز العملة |
| `in_stock` | boolean \| null | لا | مشتق من مجموع `stock_info.qty` عبر المخازن | `true` إن وُجدت كمية > 0 بأي مخزن |
| `stock_quantity` | integer \| null | لا | مجموع `stock_info.qty` | الكمية الكلية المتوفرة |
| `photos` | string[] | لا (افتراضي `[]`) | `products.photos` (JSON) | روابط صور المنتج |
| `deleted_at` | string \| null | لا | `products.deleted_at` | ISO 8601 — منتج محذوف منطقياً إن وُجدت قيمة |

### `SystemOrder` — عنصر بـ `orders` (`GET /orders/search`, `GET /orders`) أو جسم `GET /orders/{order_id}`

| الحقل | النوع | إلزامي | يقابل عمود قاعدة البيانات | الوصف |
|---|---|---|---|---|
| `order_id` | string | **نعم** | `sells.receipt_number` أو `sells.id` | مثلاً `ORD-1001` — نفس الصيغة التي يستخرجها `app/features/support/router.py::extract_order_id` من رسالة الموظف |
| `status` | string \| null | لا | `sells.sell_status` (أو مرادفه العربي) | نص عربي حر — تُطابَق ضده مرادفات `_STATUS_SYNONYMS` وقت التشغيل، لا قائمة ثابتة بالعقد |
| `customer_name` | string \| null | لا | `sells.customer_name` | |
| `phone` | string \| null | لا | `sells.customer_phone_number` | صيغة عراقية `07XXXXXXXXX` |
| `customer_city` | string \| null | لا | اسم المحافظة (`commondata.cities.name_arabic`) | |
| `customer_district` | string \| null | لا | `delivery_info->>'districtId'` محلولاً لاسم | |
| `address` | string \| null | لا | `delivery_info` (jsonb) | نص العنوان الكامل |
| `items` | `SystemOrderItem[]` | لا (افتراضي `[]`) | `sell_items` | انظر الجدول التالي |
| `total` | number \| null | لا | `sells.total_price` | |
| `currency` | string \| null | لا (افتراضي `"IQD"`) | — | |
| `eta` | string \| null | لا | `sells.estimated_delivery_date` | نص جاهز للعرض المباشر |
| `created_at` | string \| null | لا | `sells.created_at` | ISO 8601 |

### `SystemOrderItem` — عنصر واحد بمصفوفة `items`

| الحقل | النوع | إلزامي | يقابل عمود قاعدة البيانات |
|---|---|---|---|
| `product_id` | string \| null | لا | `sell_items.product_id` |
| `product_name` | string | **نعم** | اسم المنتج بسطر الطلب |
| `quantity` | integer | لا (افتراضي `1`) | `sell_items.qty` |
| `unit_price` | number \| null | لا | `sell_items.unit_price` |
| `line_total` | number \| null | لا | `sell_items.net_amount` أو مكافئه |

### سياسة التسامح (مقصودة)

- **كل الحقول اختيارية عدا معرّف واحد لكل نموذج** (`id`/`name` للمنتج، `order_id`/`product_name` للطلب وسطوره). حقل ناقص بالاستجابة الفعلية **لا يفشّل الطلب بالكامل** — يتحوّل تلقائياً لـ `null`، والموديل مبرمج أصلاً (بالبرومبت) يقول "أتأكدلك من السعر" بدل ما يخترع قيمة لحقل ناقص، بدل ما نرفض الاستجابة كلها.
- **عنصر واحد فاشل التحقق (نوع بيانات خاطئ تماماً، لا حقل ناقص فقط) يُستبعَد بصمت من القائمة** — تحذير باللوق (`logger.warning`)، بلا رمي استثناء يكسر باقي النتائج السليمة بنفس الاستدعاء. التطبيق: `app/products.py::_parse_products`، `app/order_gateway.py::_parse_orders`.
- **حقول إضافية غير موثّقة هنا تمر بلا رفض** (`model_config = ConfigDict(extra="allow")`) — إضافة عمود جديد بباك اند السستم لا تكسر شيئاً، فقط لا تُتحقق ولا تصل تلقائياً للموديل إلا لو أُضيفت صراحة للعقد.

### أين التعريف الرسمي بالكود

`app/system_backend_schema.py` — `SystemProduct`, `SystemProductSearchResponse`, `SystemOrder`, `SystemOrderItem`, `SystemOrderListResponse` (Pydantic). هذا الملف هو **مصدر الحقيقة الوحيد**؛ الجداول أعلاه انعكاس مقروء بشرياً له. أي تعديل بشكل الاستجابة الفعلي من باك اند السستم يبدأ بتعديل هذا الملف، ثم `app/products.py`/`app/order_gateway.py` (نقاط الاستهلاك)، بلا حاجة لتغيير أي router.

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
  "engine": "vllm",
  "tool_calls": [
    {
      "tool": "search_products",
      "args": {"query": "لابتوب", "top_k": 5},
      "result": [{"id": "p001", "name": "لابتوب لينوفو IdeaPad 15", "price": 750000, "currency": "IQD"}]
    }
  ]
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
  "engine": "vllm"
}
```

| الحقل | النوع | الوصف |
|---|---|---|
| `session_id` | string | نفسه لو أرسلته، أو معرّف جديد تولّد تلقائياً |
| `answer` | string | رد الوكيل للعميل (نص المحادثة العادي، بدون أي علامات داخلية) |
| `order` | object \| null | `null` إلا لو العميل أكّد الشراء بنفس هذا الرد — عندها كائن `OrderConfirmation` كامل (تفصيله بالأسفل) |
| `engine` | string | `"vllm"` (توليد حقيقي) أو `"fallback"` (محلياً بدون GPU) |
| `tool_calls` | array | سجل استدعاءات أداة `search_products` بهذا الدور فقط (`{tool, args, result}` لكل استدعاء) — للشفافية/التصحيح فقط، لا يدخل بأي منطق قرار. مصفوفة فارغة لو الموديل جاوب بلا أداة أو بوضع `fallback` |

**ملاحظة مهمة**: `order` يظهر فقط بالرد اللي فيه العميل أكّد الشراء صراحة. أي رد بعده (لو رجع يسأل شي ثاني بنفس الجلسة) يرجّع `order: null` من جديد. القرار بالاكتمال يجيك من حقل `order_ready` داخلي بمخطط guided_json (انظر [README.md](README.md#آلية-الوكيل-يقرر-واستدعاء-الأدوات))، مو من علامة نصية بالرد.

### `POST /sales/chat/stream`
نفس المدخل بالضبط، لكن بصيغة SSE (`Content-Type: text/event-stream`) — **بث حقيقي توكن-بتوكن** (لا قطعة واحدة): جولة قرار مصغّرة أولاً وراءها بلا بث (استدعاء أداة `search_products` + تحديد `order_ready` عبر `guided_json` — لازم يكتمل هذا الجزء لأنه JSON مقيَّد)، ثم — إذا البوابة الحتمية لم تحجب تثبيت الطلب — جولة نص حرة تُبث فعلياً دلتا-بدلتا فور توليد كل توكن من vLLM (`stream: true`، بلا `guided_json`)، مستفيدة من prefix caching (نفس بادئة السياق محسوبة أصلاً بجولة القرار). أول توكن يصل العميل فور بدء جولة النص، بدل انتظار الرد كاملاً.

**تدفق الأحداث:**
```
data: {"delta": "عندنا"}

data: {"delta": " لابتوب"}

data: {"delta": " لينوفو IdeaPad 15 بسعر 750000 دينار..."}

data: {"done": true, "session_id": "...", "order": null, "tool_calls": [...]}

```

- عدة أحداث `delta` متتالية، كل واحد يحمل جزءاً من الرد بترتيب وصوله من الموديل (اربطها بالتسلسل — النص الكامل = تجميعها).
- استثناء: لو حُجب تثبيت الطلب لنقص بيانات (اسم/هاتف/عنوان)، يوصل سؤال حتمي واحد كدلتا وحيدة بدل توليد حر (أسرع وأدق من انتظار الموديل).
- الحدث الأخير دايماً `{"done": true, ...}` ويحمل `order` (نفس شكل `/sales/chat` — `null` أو كائن `OrderConfirmation` كامل) و`tool_calls` (سجل استدعاء `search_products` بهذا الدور، نفس شكل `/sales/chat`).
- محلياً بدون GPU (`engine` غير جاهز): دلتا واحدة بالرد الاحتياطي، ثم `done` بـ`tool_calls: []`.

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
  "engine": "deterministic",
  "tool_calls": []
}
```

| الحقل | النوع | الوصف |
|---|---|---|
| `session_id` | string | نفسه لو أرسلته، أو معرّف جديد تولّد تلقائياً |
| `answer` | string | رد الوكيل للعميل |
| `engine` | string | `"deterministic"` (رقم طلب/هاتف/حالة أُجيب مباشرة من `orders.json` بلا موديل — الحالة الأدق والأشيع)، `"vllm"` (سؤال عام أُجيب بالموديل+أداة `get_order_status`)، أو `"fallback"` (محلياً بدون GPU ولا تطابق حتمي) |
| `tool_calls` | array | سجل استدعاء أداة `get_order_status` — يمتلئ فقط لما `engine == "vllm"` (المسار الحتمي لا يمر بحلقة الأدوات أصلاً فيبقى فارغاً حتى لو أجاب بنفس المعلومة) |

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

### `POST /support/chat/stream`
نفس مدخل `/support/chat` بالضبط، بصيغة SSE. **رقم طلب/هاتف/حالة صريحة بالرسالة** يُجاب حتمياً (`orders.json` مباشرة، بلا موديل) ويصل كدلتا واحدة فورية (بلا زمن استدلال). **أي سؤال عام آخر** يمر بجولة قرار مصغّرة (أداة `get_order_status` عند الحاجة) ثم بث حقيقي توكن-بتوكن لجولة النص الحرة — نفس بنية `/sales/chat/stream` تماماً.

**تدفق الأحداث (سؤال عام):**
```
data: {"delta": "طلبك"}

data: {"delta": " قيد التوصيل"}

data: {"delta": "، متوقع يوصلك خلال يومين."}

data: {"done": true, "session_id": "...", "tool_calls": [...]}

```

**تدفق الأحداث (رقم/حالة صريحة — مسار حتمي):**
```
data: {"delta": "طلبك ORD-1001 حالته: قيد التوصيل، متوقع يوصلك خلال يومين."}

data: {"done": true, "session_id": "...", "tool_calls": []}

```

- الحدث الأخير دايماً `{"done": true, "session_id": ..., "tool_calls": [...]}` — بدون حقل `order` (هذا مختص بـ `/sales/chat*` فقط، الدعم لا يثبّت طلبات).

---

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

**ملاحظة**: مدخل `image` يستخدم نفس محرك vLLM ونفس أوزان الموديل المستخدَمة بـ `/sales/chat`/`/support/chat` — ماكو موديل ثانٍ يتحمّل ولا استهلاك ذاكرة إضافي.

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
- على RunPod (بعد `HF_TOKEN` صحيح وتشغيل `start.sh`/الـ Docker image)، تتحول تلقائياً لـ `engine: "vllm"` بدون أي تغيير بالكود أو بشكل الطلبات/الاستجابات.
- أول طلب بعد الإقلاع قد ياخذ وقت أطول (تحميل الموديل + المحوّل من Hugging Face أول مرة) — الطلبات اللاحقة أسرع.
