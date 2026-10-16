# المتابعة الصوتية — `/voice_followup/*`

## سؤال ورد (`/ask` و`/respond`)

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
| `503` (على `/ask` أو `/respond`) | تحويل النص لصوت (`chatterbox-tts`) أو الصوت لنص (`transformers`) غير مثبَّت محلياً | نص يوضح السبب |

**ملاحظة**: بدون محرك vLLM جاهز (محلياً بدون GPU)، السؤال والتحليل يرجعان بنصوص احتياطية عامة ثابتة بدل توليد حقيقي — الميزة تبقى تشتغل بالكامل (توليد صوت فعلي، تحويل صوت لنص فعلي) إلا خطوة صياغة النص بالموديل.

#### كيف تُرسل صوت رد الزبون من Java
فرق وحيد عن مثال `/orders/create` أعلاه: `session_id` يروح بالرابط (query parameter) لا بجسم الـ multipart، والاستجابة نفسها ملف صوت لازم تُحفظ + هيدراتها تُفكّ ترميزها (percent-encoded):

```java
OkHttpClient client = new OkHttpClient();
String apiKey = "sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b";
String baseUrl = "https://1spx1ivrqcvt1h-8000.proxy.runpod.net";
String sessionId = "8f1e2d3c-4b5a-6789-0abc-def123456789"; // من هيدر X-Session-Id بالخطوة 1

File replyAudio = new File("customer_reply.ogg");
RequestBody body = new MultipartBody.Builder()
    .setType(MultipartBody.FORM)
    .addFormDataPart("audio", replyAudio.getName(),
        RequestBody.create(replyAudio, MediaType.parse("audio/ogg")))
    .build();

Request request = new Request.Builder()
    .url(baseUrl + "/voice_followup/respond?session_id=" + sessionId)
    .header("X-API-Key", apiKey)
    .post(body)
    .build();

try (Response response = client.newCall(request).execute()) {
    byte[] thanksWav = response.body().bytes();
    Files.write(Path.of("thanks.wav"), thanksWav);

    String reasonSummary = URLDecoder.decode(
        response.header("X-Reason-Summary", ""), StandardCharsets.UTF_8);
    String customerTranscript = URLDecoder.decode(
        response.header("X-Customer-Transcript", ""), StandardCharsets.UTF_8);
    boolean querySent = "true".equals(response.header("X-Query-Sent"));
}
```

## مكالمة تأجيل التسليم — `voice_followup/postpone` (شخصية "صباح")

> ✅ الخطوة 1 (`/postpone/start`): **`application/json`** — ⚠️ الخطوة 2 (`/postpone/respond`): **`multipart/form-data`** — كلتاهما بمفتاح المتابعة الصوتية نفسه

### الوصف
مكالمة صوتية صادرة بشخصية **"صباح"** لشحنة **لسه ما انسلّمت**: تسأل الزبون يريد التسليم اليوم لو يفضّل يأجّله، وتنتهي بموعد مؤكَّد يُرسَل لنظام الطلبات كتاريخ ISO صريح.

الفرق الجوهري عن `/ask` و`/respond` أعلاه: هذه **مكالمة متعددة الأدوار** — `/postpone/respond` يُستدعى مراراً بنفس `session_id` حتى يرجع `X-Call-Status: ended`.

**كل قرارات المكالمة تُحسب بالخادم حتمياً** (مطابقة نصية على رد الزبون): هل طابق موعداً؟ هل أكّد؟ هل نغلق؟ — والنموذج يُستدعى بعد الحسم ليصوغ الجملة بلهجة عراقية فقط. هذا يمنع أن يخترع النموذج تاريخاً، وخطأ التاريخ هنا له ثمن تشغيلي مباشر.

### المواعيد المقبولة

| ما يقوله الزبون | يُفهم كـ |
|---|---|
| اليوم · هسه · الحين | اليوم |
| باجر · باچر · بكرة · غدًا | +1 يوم |
| بعد باجر · بعد بكرة · بعد غد · يومين | +2 يوم |
| **بعد N أيام** — رقمًا (`4` / `٤`) أو حروفًا ("أربع أيام") | +N يوم |
| بعد أسبوع · بعد أسبوعين | +7 / +14 يوم |
| **اسم يوم بالأسبوع** ("خليها الخميس") | أقرب خميس قادم |

- **السقف: ١٤ يومًا** (`MAX_POSTPONE_DAYS`). أي موعد أبعد ("بعد شهر") يُرفض، فتعتذر صباح وتطلب موعدًا أقرب — ولا يُثبَّت شيء.
- **صباح لا تقترح موعدًا من عندها** — تعرض القريب فقط (اليوم/باجر/بعد باجر)، والزبون حرّ يحدد غيره.
- عند التأكيد ترجع **بصيغة الزبون نفسها**: من قال "الخميس" تسمع "خلّيها يوم الخميس؟" لا "بعد ٥ أيام".

### الخطوة 1 — `POST /voice_followup/postpone/start`

```
POST /voice_followup/postpone/start
Content-Type: application/json
X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b
```

الجسم هو **نفس** `VoiceFollowupOrderRequest` المستعمل بـ `/ask` (انظر جدول الحقول بقسم `/ask` أعلاه). الحقول المفيدة هنا:

| الحقل | إلزامي | ملاحظة |
|---|---|---|
| `order_id` | ✅ | **لا يُنطق بالمكالمة** — صباح تذكر الشحنة بالمنتج |
| `status` | ✅ | حالة الشحنة كما بنظامكم |
| `items` | ❌ | منها تبني صباح جملة "الغسالة اللي طلبتها" |
| `scheduled_date` | ❌ | الموعد الحالي — تذكّر به الزبون قبل عرض التأجيل |

> 🔒 `customer_name` و`customer_phone` **لا يُمرَّران للنموذج** بهذا المسار حتى لو أرسلتهما: صباح ممنوعة من ذكر اسم المستلم، وحذف الحقلين من سياق النموذج يمنع التسريب بدل الاعتماد على تعليمة نصية وحدها.

#### مثال
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/voice_followup/postpone/start" \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -H "Content-Type: application/json" \
  -o opening.wav -D headers_start.txt \
  -d '{
        "order_id": "ORD-000123456",
        "status": "قيد التوصيل",
        "items": [{"product_name": "غسالة اتوماتيك", "quantity": 1}],
        "scheduled_date": "2026-09-05"
      }'
```

#### شكل الاستجابة
`200 OK` — **الجسم ملف صوت خام `audio/wav`**:

| الهيدر | المحتوى |
|---|---|
| `X-Session-Id` | معرّف الجلسة — **احفظه** لكل أدوار الخطوة 2 |
| `X-Reply-Text` | نص جملة الافتتاح (Percent-encoded) |
| `X-Call-Status` | `continue` دائمًا بهذه الخطوة |

### الخطوة 2 — `POST /voice_followup/postpone/respond` (تتكرر)

```
POST /voice_followup/postpone/respond?session_id=<من الخطوة 1>
Content-Type: multipart/form-data
X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b
```

| الحقل | النوع | إلزامي |
|---|---|---|
| `session_id` | query string | ✅ |
| `audio` | file (wav/mp3/m4a) | ✅ |

#### مثال
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/voice_followup/postpone/respond?session_id=8f1e2d3c-..." \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -F "audio=@customer_reply.wav" \
  -o sabah_reply.wav -D headers_respond.txt
```

#### شكل الاستجابة
`200 OK` — **الجسم صوت رد صباح `audio/wav`**:

| الهيدر | المحتوى |
|---|---|
| `X-Call-Status` | `continue` = أرسل دورًا آخر · `ended` = انتهت المكالمة |
| `X-Chosen-Option` | الموعد المختار: `today` / `plus_N` / `weekday_D` (فارغ ما لم يُختَر بعد) |
| `X-Postpone-Saved` | `true`/`false` — هل وصل الموعد لنظام الطلبات (فارغ ما لم تنتهِ المكالمة بتأكيد) |
| `X-Customer-Transcript` | نص رد الزبون (Percent-encoded) |
| `X-Reply-Text` | نص رد صباح (Percent-encoded) |

> ♻️ **حلقة الاستدعاء**: كرّر `/postpone/respond` بنفس `session_id` طالما `X-Call-Status: continue`. سكوت الزبون **ليس خطأ** — يُعامَل كرد غامض: تعيد صباح السؤال مرة، ثم تقفل بأدب.

#### ما يصل لنظام الطلبات عند التأكيد
```json
{
  "order_id": "ORD-000123456",
  "new_scheduled_date": "2026-09-09",
  "postpone_choice": "plus_4",
  "reason": "تأجيل بطلب الزبون عبر مكالمة صباح"
}
```
`new_scheduled_date` تاريخ ISO محسوب بالخادم — لا تُرسَل الكلمة العربية وحدها، حتى يستقبل نظام الطلبات تاريخًا صالحًا للحفظ مباشرة.

### أخطاء محتملة
| الكود | السبب |
|---|---|
| `401` | مفتاح `X-API-Key` غلط |
| `404` | `session_id` غير موجود أو انتهت صلاحيته (٣٠ دقيقة) — استدعِ `/postpone/start` من جديد |
| `422` | جسم الخطوة 1 ناقص `order_id` أو `status` |
| `503` | تحويل النص لصوت أو الصوت لنص غير متوفر (يحتاج بيئة GPU) |
