# دليل اختبار الـ API على Postman

> ⚠️ **تنبيه أمني**: هذا الملف وملف [`postman_collection.json`](postman_collection.json) يحتويان مفاتيح API حقيقية
> (نفس مفاتيح [`api-order-sales-voice.md`](api-order-sales-voice.md)). لا تشاركهما أو ترفعهما لمستودع عام.

مرجع الميزات الكامل موجود في [api-order-sales-voice.md](api-order-sales-voice.md) — هذا الملف خطوات عملية جاهزة للنسخ لتشغيلها في Postman مباشرة، بدون كتابة أي شي يدوياً.

---

## 0) الاستيراد (مرة وحدة فقط)

1. افتح Postman.
2. **File → Import** (أو زر **Import** أعلى يسار الشاشة).
3. اختر ملف [`docs/postman_collection.json`](postman_collection.json) من هذا المشروع.
4. بعد الاستيراد تظهر مجموعة باسم **"Order/Sales/Voice API — order_intake, sales, voice_followup"** بالشريط الجانبي، فيها 4 مجلدات:
   - `0) Health Check`
   - `1) Orders — Create Order`
   - `2) Sales — Chat`
   - `3) Voice Followup`

كل الرابط الأساسي والمفاتيح ومنطق حفظ `session_id` بين الطلبات **معبّأ مسبقاً** كمتغيّرات على مستوى الـ Collection — لا حاجة لإنشاء Postman Environment منفصل، ولا لنسخ أي قيمة يدوياً.

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

**الاستجابة المتوقعة**: `200 OK` مع جسم JSON من نوع `OrderConfirmation` (شكله كامل في [api-order-sales-voice.md § 1](api-order-sales-voice.md#1-ميزة-إنشاء-الطلبات--order_intake)).

---

## 3) اختبار المبيعات (`sales`)

المجلد **`2) Sales — Chat`** فيه 3 طلبات — **شغّلها بهذا الترتيب بالضبط**:

1. **Sales Chat — أول رسالة**: اضغط **Send** مباشرة (الجسم معبّأ مسبقاً). لاحظ تبويب **Tests** بأسفل الاستجابة — سكربت مرفق يحفظ `session_id` تلقائياً بمتغيّر `sales_session_id` (تقدر تتأكد منه بتبويب Variables بالمجموعة).
2. **Sales Chat — تكملة المحادثة**: اضغط **Send** — الجسم يستخدم `{{sales_session_id}}` المحفوظ تلقائياً من الخطوة السابقة، فتكمل نفس المحادثة بلا نسخ يدوي لأي معرّف.
3. **Sales Chat — Streaming (SSE)**: نفس الجسم لكن الاستجابة `text/event-stream` — Postman يعرضها كنص متدفّق (أسطر `data: {...}`) بتبويب Response بدل JSON واحد.

عدّل حقل `message` بالجسم (تبويب **Body**) لتجربة رسائل مختلفة — بقية الجلسة تستمر تلقائياً طالما لم تفرّغ متغيّر `sales_session_id`.

---

## 4) اختبار المتابعة الصوتية (`voice_followup`)

المجلد **`3) Voice Followup`** فيه خطوتين — **شغّلهما بهذا الترتيب**:

1. **Step 1 — Ask**: اضغط **Send** مباشرة (الجسم معبّأ ببيانات طلب تجريبي). الاستجابة ملف صوت `audio/wav` خام:
   - لسماعه: بتبويب **Response**، اضغط **Save Response → Save to a file**.
   - سكربت Tests المرفق يحفظ الهيدر `X-Session-Id` تلقائياً بمتغيّر `voice_session_id`.
   - نص السؤال المولَّد موجود بهيدر `X-Question-Text` (مُرمّز Percent-encoded — Postman يعرضه بتبويب **Headers** بالاستجابة، تقدر تفكّه بموقع [urldecoder.org](https://www.urldecoder.org) أو أي أداة urldecode).
2. **Step 2 — Respond**: افتح تبويب **Body**، بحقل `audio` اضغط **Select Files** واختر تسجيل صوتي (رد الزبون)، ثم **Send** — الرابط يستخدم `{{voice_session_id}}` المحفوظ تلقائياً من الخطوة السابقة.
   - الاستجابة ملف صوت شكر `audio/wav` (احفظه بنفس طريقة Step 1).
   - الهيدرات `X-Reason-Summary` و `X-Customer-Transcript` و `X-Query-Sent` تظهر بتبويب **Headers** بالاستجابة (أول اثنين Percent-encoded).

---

## 5) جدول أخطاء سريع (لكل الطلبات)

| الكود | يعني |
|---|---|
| `401 Unauthorized` | هيدر `X-API-Key` غلط — تأكد من متغيّرات `orders_api_key` / `sales_api_key` / `voice_api_key` بالمجموعة |
| `422 Unprocessable Entity` | حقل إلزامي ناقص أو نوعه غلط بالجسم |
| `400 Bad Request` | (خاص بـ `/orders/create`) أرسلت أكثر من حقل واحد أو ولا حقل من `text`/`audio`/`image` |
| `404 Not Found` | (خاص بـ `/voice_followup/respond`) الـ `session_id` غير موجود أو انتهت صلاحيته — أعد تشغيل Step 1 |
| `501` / `503` | ميزة تحتاج بيئة GPU غير متوفرة حالياً على الخادم |

تفاصيل كل كود حسب المسار موجودة بجداول "أخطاء محتملة" في [api-order-sales-voice.md](api-order-sales-voice.md).
