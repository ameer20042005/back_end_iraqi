# دليل اختبار الـ API على Postman

لتجربة ميزة تصحيح مناطق شركات التوصيل المضافة إلى الباك اند نفسه، استورد [مجموعة Postman الخاصة بها](district-correction.json) واتبع [عقد المراسلة](../api/district-correction.md). تستخدم متغير `baseUrl` نفسه ومفتاحاً مستقلاً هو `district_api_key` في `app/config.py`.

> ⚠️ **تنبيه أمني**: هذا الملف وملف [`collection.json`](collection.json) يحتويان مفاتيح API حقيقية
> (نفس مفاتيح [`overview.md`](../api/overview.md)). لا تشاركهما أو ترفعهما لمستودع عام.

مرجع الميزات الكامل موجود في [docs/api/](../api/overview.md) — هذا الملف خطوات عملية جاهزة للنسخ لتشغيلها في Postman مباشرة، بدون كتابة أي شي يدوياً.

---

## 0) الاستيراد (مرة وحدة فقط)

1. افتح Postman.
2. **File → Import** (أو زر **Import** أعلى يسار الشاشة).
3. اختر ملف [`docs/postman/collection.json`](collection.json) من هذا المشروع.
4. بعد الاستيراد تظهر مجموعة باسم **"Order/Sales/Voice API — order_intake, sales, voice_followup"** بالشريط الجانبي، فيها 5 مجلدات:
   - `0) Health Check`
   - `1) Orders — Create Order`
   - `2) Sales — Chat`
   - `4) Live Calls — LiveKit`

كل الرابط الأساسي والمفاتيح **معبّأ مسبقاً** كمتغيّرات على مستوى الـ Collection — لا حاجة لإنشاء Postman Environment منفصل، ولا لنسخ أي قيمة يدوياً.

للتأكد من القيم أو تعديلها لاحقاً (مثلاً لو تغيّر الرابط الأساسي أو المفاتيح): اضغط بالزر اليمين على اسم المجموعة → **Edit** → تبويب **Variables**.

---

## 1) فحص أن الخادم شغّال

شغّل الطلب **`0) Health Check`** مباشرة (بلا مفتاح). لو رجع `200 OK` فالخادم جاهز.

---

## 2) اختبار إنشاء الطلبات (`order_intake`)

المجلد **`1) Orders — Create Order`** فيه 3 طلبات جاهزة:

| الطلب | ماذا يفعل |
|---|---|
| **Create Order — Text** | جسمه معبّأ مسبقاً بنص جاهز (حقل `text`) — اضغط **Send** فوراً بلا أي تعديل. |
| **Create Order — Audio** | افتح تبويب **Body**، بحقل `audio` اضغط **Select Files** واختر ملف صوت من جهازك (wav/mp3/m4a/ogg)، ثم **Send**. |
| **Create Order — Image** | نفس الشي بحقل `image`، اختر صورة (jpg/png). يحتاج بيئة GPU على الخادم وإلا يرجع `501`. |

**مهم:** كل طلب يرسل حقل واحد فقط — لا تفعّل أكثر من حقل بنفس الطلب (يرجع `400`).

**الاستجابة المتوقعة**: `200 OK` مع جسم JSON من نوع `OrderConfirmation` (شكله كامل في [orders.md](../api/orders.md)).

---

## 3) اختبار المبيعات (`sales`)

المجلد **`2) Sales — Chat`** فيه 3 طلبات — **شغّلها بهذا الترتيب بالضبط**:

1. **Sales Chat — أول رسالة**: اضغط **Send** مباشرة (الجسم معبّأ مسبقاً). لاحظ تبويب **Tests** بأسفل الاستجابة — سكربت مرفق يحفظ `session_id` تلقائياً بمتغيّر `sales_session_id` (تقدر تتأكد منه بتبويب Variables بالمجموعة).
2. **Sales Chat — تكملة المحادثة**: اضغط **Send** — الجسم يستخدم `{{sales_session_id}}` المحفوظ تلقائياً من الخطوة السابقة، فتكمل نفس المحادثة بلا نسخ يدوي لأي معرّف.
3. **Sales Chat — صورة منتج (image_base64)**: الصق base64 خام لصورة منتج (بلا بادئة `data:image/...;base64,`) بحقل `image_base64` قبل الإرسال — الموديل يحللها ويطابقها مع الكتالوج، يقترح بديلاً مشابهاً لو ما لگى تطابقاً تاماً.
4. **Sales Chat — Streaming (SSE)**: نفس الجسم لكن الاستجابة `text/event-stream` — Postman يعرضها كنص متدفّق (أسطر `data: {...}`) بتبويب Response بدل JSON واحد.

عدّل حقل `message` بالجسم (تبويب **Body**) لتجربة رسائل مختلفة — بقية الجلسة تستمر تلقائياً طالما لم تفرّغ متغيّر `sales_session_id`.

---

## 4) اختبار المكالمات الحية (`voice_followup`، LiveKit)

المجلد **`4) Live Calls — LiveKit`** فيه 4 طلبات:

1. **Operator — اتصل بهؤلاء**: الموظف يكتب أمراً مع `company_name`. المتوقع `tool_calls: search_contacts` (أداة ينفّذها تطبيق سطح المكتب).
2. **Operator — بعد نتيجة البحث → start_call**: نفس المحادثة مع نتيجة الأداة. المتوقع `tool_calls: start_call`، والسكربت يحفظ وسائطها بمتغيّر `start_call_args`.
3. **Start Call**: الجسم = وسائط `start_call` + `company_name` (`goal`، `questions`، `context`، `contact`، `reference_id`). يرجع `room` و`operator_token` و`dial_mode`. يحتاج `LIVEKIT_*` بالخادم، وإلا يرجع `503` واضحة.
4. **Call Outcome**: `GET /voice_followup/calls/{{livekit_room}}/outcome` — `pending` لحد ما تنحفظ النتيجة، بعدها `done`.

المكالمة نفسها صوت حي عبر LiveKit، فما تنجرّب من Postman: ادخل الغرفة بـ `operator_token` من [LiveKit Meet](https://meet.livekit.io) (Custom: `livekit_url` + التوكن) واحچي مع الـ agent. التفاصيل: [voice-followup.md](../api/voice-followup.md).

### الوكيل المحلي عبر WebSocket (`WS /voice_followup/stream`)

Postman يدعم طلبات WebSocket (**New → WebSocket**)، فتگدر تجرّب البروتوكول النصي بلا LiveKit:

1. الرابط: `{{baseUrl}}` بعد تبديل `https://` بـ `wss://` (أو `http://` بـ `ws://`) + `/voice_followup/stream`، وبتبويب **Headers** أضف `X-API-Key` بمفتاح الصوت (أو بالرابط `?api_key=...`). اضغط **Connect**.
2. أرسل كأول رسالة (Text): `{"type":"start","call":{"company_name":"متجر النور","goal":"اسأل الزبون عن رأيه بالخدمة","contact":{"phone":"07701234567"},"reference_id":"ORD-1042"},"sample_rate":16000}` ← يرجع `ready` بـ `call_id`.
3. `{"type":"ping"}` ← `pong`. `{"type":"call_state","state":"ended"}` ← حدث `outcome` احتياطي ("الزبون لم يرد على المكالمة") ثم إغلاق، والنتيجة تبين بطلب **Call Outcome** بعد ما تحط `call_id` بمتغيّر `livekit_room`.

الصوت نفسه (PCM ثنائي) ما ينجرّب عملياً من Postman؛ العقد الكامل بـ [voice-followup.md](../api/voice-followup.md#الوكيل-المحلي-عبر-websocket).

---

## 6) جدول أخطاء سريع (لكل الطلبات)

| الكود | يعني |
|---|---|
| `401 Unauthorized` | هيدر `X-API-Key` غلط — تأكد من متغيّرات `orders_api_key` / `sales_api_key` / `voice_api_key` بالمجموعة |
| `422 Unprocessable Entity` | حقل إلزامي ناقص أو نوعه غلط بالجسم |
| `400 Bad Request` | (خاص بـ `/orders/create`) أرسلت أكثر من حقل واحد أو ولا حقل من `text`/`audio`/`image` |
| `502` | (خاص بـ `/voice_followup/calls`) خادم LiveKit رفض الطلب أو ما يستجيب |
| `501` / `503` | ميزة تحتاج بيئة GPU غير متوفرة حالياً، أو `LIVEKIT_*` غير مضبوطة (`/voice_followup/calls`) |

تفاصيل كل كود حسب المسار موجودة بجداول "أخطاء محتملة" في ملفات [docs/api/](../api/overview.md).
