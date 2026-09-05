# توثيق API — أربع ميزات: إنشاء الطلبات، المبيعات، المتابعة الصوتية، مكالمة الراجع

> ⚠️ **تنبيه أمني**: هذا الملف يحتوي على مفاتيح API الحقيقية الحالية للخادم (لأنها القيم
> الافتراضية المضبوطة فعلياً بـ `app/config.py` ولا يوجد `.env` يتجاوزها). لا تشارك هذا
> الملف أو تنشره علناً — أي شخص يملك المفتاح يقدر يستهلك الخدمة المرتبطة به مباشرة.
> إذا احتجت مشاركته مع طرف ثالث، بدّل قيم `*_API_KEY` بمتغيرات بيئة حقيقية أولاً.

## الرابط الأساسي (Base URL)

```
https://1spx1ivrqcvt1h-8000.proxy.runpod.net
```

كل مسار أدناه يُضاف لهذا الرابط مباشرة، مثلاً: `https://1spx1ivrqcvt1h-8000.proxy.runpod.net/orders/create`.

- توثيق OpenAPI التفاعلي (Swagger) متوفر تلقائياً على `/docs`.
- فحص صحة الخادم: `GET /health` (بلا مفتاح API).

## آلية الحماية (Authentication) — مشتركة بين الميزات الأربع

كل ميزة محمية بمفتاح API **خاص بها** (لا مفتاح عام مشترك)، يُرسَل بهيدر HTTP:

```
X-API-Key: <المفتاح الخاص بالميزة>
```

| الميزة | الهيدر | المفتاح الحالي (افتراضي الخادم) |
|---|---|---|
| إنشاء الطلبات (`/orders/*`) | `X-API-Key` | `sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c` |
| المبيعات (`/sales/*`) | `X-API-Key` | `sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2` |
| المتابعة الصوتية (`/voice_followup/*`) | `X-API-Key` | `sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b` |

**ملاحظات مهمّة:**
- ماكو رابط بين المفاتيح — مفتاح المبيعات ما يشتغل مع مسار إنشاء الطلبات وبالعكس.
- إذا الهيدر ناقص أو غلط ترجع الاستجابة:
  - `401 Unauthorized` — `{"detail": "مفتاح API غير صحيح لخدمة <اسم الخدمة>"}` (مفتاح خطأ).
  - `422 Unprocessable Entity` — إذا الهيدر `X-API-Key` غير موجود إطلاقاً بالطلب (FastAPI يرفضه قبل ما يوصل لمنطق التحقق).
  - `500 Internal Server Error` — لو الخادم نفسه ما عنده المفتاح مضبوط بإعداداته (خطأ إعداد من جهة الخادم، ليس من جهتك).
- الاتصال يفضّل أن يكون عبر HTTPS دائماً (الرابط أعلاه أصلاً HTTPS) حتى لا يمر المفتاح بنص صريح على الشبكة.

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
| `POST /voice_followup/ask` | `application/json` ✅ | جسم JSON عادي بالكامل (تفاصيل الطلب) |
| `POST /voice_followup/respond` | `multipart/form-data` ⚠️ **ليس JSON** | لازم يحمل ملف صوت فعلي، و`session_id` يُرسل كـ query parameter بالرابط نفسه لا بالجسم |

**خلاصة للمطوّر:** مساري المبيعات (`/sales/chat`, `/sales/chat/stream`) ومسار بدء المتابعة الصوتية (`/voice_followup/ask`) هي الوحيدة اللي تستقبل **JSON خالص** بالجسم مع هيدر `Content-Type: application/json` + هيدر `X-API-Key`. أما مسار إنشاء الطلبات ومسار الرد على المتابعة الصوتية فيحتاجان **رفع ملف** (صوت/صورة)، فهذولا حصراً بصيغة `multipart/form-data` — هيدر `X-API-Key` نفسه موجود بكل الحالات بلا استثناء، بس الجسم يتغيّر.

---

## 1) ميزة إنشاء الطلبات — `order_intake`

> 📎 **طريقة الإرسال: `multipart/form-data` (ليس JSON)** + هيدر `X-API-Key`

### الوصف
تُنشئ طلباً واحداً من **مدخل وحيد فقط** (نص، أو رسالة صوتية، أو صورة) — الخادم يفهم المدخل، يستخرج بيانات الزبون والمنتجات، يحسب السعر من قيمة `quoted_price` المذكورة صراحة، ثم يعيد تأكيد طلب جاهز. لو كانت كل الحقول الإلزامية مكتملة (اسم + هاتف + عنوان + منتج) يُرسل الطلب تلقائياً لنظام الطلبات الخارجي أيضاً.

### الاستخدام (الحالات)
- **نص**: رسالة واتساب/شات مكتوبة من الزبون بالعامية العراقية.
- **صوت**: تسجيل صوتي (WhatsApp voice note مثلاً) يتحول تلقائياً لنص عبر Whisper.
- **صورة**: لقطة شاشة محادثة، أو صورة منتج، أو قائمة مكتوبة بخط اليد.

### نقطة النهاية (Endpoint)

```
POST /orders/create
Content-Type: multipart/form-data
X-API-Key: sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c
```

### ماذا يجب أن ترسل
جسم الطلب `multipart/form-data` بثلاثة حقول اختيارية — **يجب إرسال حقل واحد فقط من الثلاثة** (لا أكثر ولا أقل، وإلا `400 Bad Request`):

| الحقل | النوع | الوصف |
|---|---|---|
| `text` | نص (Form field) | نص رسالة الزبون كما هي |
| `audio` | ملف (File) | ملف صوتي (wav/mp3/m4a/ogg...) |
| `image` | ملف (File) | صورة (jpg/png...) — تحتاج بيئة GPU مفعّلة على الخادم، غير ذلك ترجع `501` |

#### مثال — نص
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/orders/create" \
  -H "X-API-Key: sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c" \
  -F "text=اريد اطلب غسالة اتوماتيك، اسمي احمد، رقمي 07701234567، بغداد الكرادة"
```

#### مثال — صوت
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/orders/create" \
  -H "X-API-Key: sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c" \
  -F "audio=@voice_message.ogg"
```

#### مثال — صورة
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/orders/create" \
  -H "X-API-Key: sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c" \
  -F "image=@order_screenshot.jpg"
```

### كيف تُرسل الصورة أو الصوت من Java

جسم الطلب `multipart/form-data`، ونفس المسار `/orders/create` بغض النظر عن نوع الملف — يختلف فقط اسم الحقل (`audio` أو `image`). حقل `text` بنفس المسار حقل نص عادي داخل نفس الـ multipart، لا JSON (انظر مثال curl أعلاه).

**الطريقة الأسهل: OkHttp** (يتعامل مع الملفات والـ multipart جاهزاً — أنصح فيها):

```java
OkHttpClient client = new OkHttpClient();
String apiKey = "sk-orders-1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a2c";
String baseUrl = "https://1spx1ivrqcvt1h-8000.proxy.runpod.net";

// إرسال صوت
File audioFile = new File("voice_message.ogg");
RequestBody audioBody = new MultipartBody.Builder()
    .setType(MultipartBody.FORM)
    .addFormDataPart("audio", audioFile.getName(),
        RequestBody.create(audioFile, MediaType.parse("audio/ogg")))
    .build();

Request audioRequest = new Request.Builder()
    .url(baseUrl + "/orders/create")
    .header("X-API-Key", apiKey)
    .post(audioBody)
    .build();

try (Response response = client.newCall(audioRequest).execute()) {
    String json = response.body().string(); // OrderConfirmation JSON
}

// إرسال صورة — نفس المنطق، فقط بدّل اسم الحقل ونوع المحتوى
File imageFile = new File("order_screenshot.jpg");
RequestBody imageBody = new MultipartBody.Builder()
    .setType(MultipartBody.FORM)
    .addFormDataPart("image", imageFile.getName(),
        RequestBody.create(imageFile, MediaType.parse("image/jpeg")))
    .build();

Request imageRequest = new Request.Builder()
    .url(baseUrl + "/orders/create")
    .header("X-API-Key", apiKey)
    .post(imageBody)
    .build();

try (Response response = client.newCall(imageRequest).execute()) {
    String json = response.body().string();
}
```

**بدون أي مكتبة خارجية** (`java.net.http.HttpClient` القياسية بـ JDK 11+) — الـ JDK ما عنده بنّاء multipart جاهز، فتبنيه يدوياً بـ boundary:

```java
import java.io.ByteArrayOutputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.UUID;

public class MultipartUpload {

    // fieldName: "audio" أو "image" حسب المسار المطلوب — نفس /orders/create لكليهما
    public static HttpResponse<String> sendFile(
            String baseUrl, String apiKey, String fieldName, Path file, String contentType
    ) throws Exception {
        String boundary = "----Boundary" + UUID.randomUUID();
        ByteArrayOutputStream body = new ByteArrayOutputStream();

        body.write(("--" + boundary + "\r\n").getBytes());
        body.write(("Content-Disposition: form-data; name=\"" + fieldName
                + "\"; filename=\"" + file.getFileName() + "\"\r\n").getBytes());
        body.write(("Content-Type: " + contentType + "\r\n\r\n").getBytes());
        body.write(Files.readAllBytes(file));
        body.write("\r\n".getBytes());
        body.write(("--" + boundary + "--\r\n").getBytes());

        HttpRequest request = HttpRequest.newBuilder()
            .uri(URI.create(baseUrl + "/orders/create"))
            .header("X-API-Key", apiKey)
            .header("Content-Type", "multipart/form-data; boundary=" + boundary)
            .POST(HttpRequest.BodyPublishers.ofByteArray(body.toByteArray()))
            .build();

        return HttpClient.newHttpClient().send(request, HttpResponse.BodyHandlers.ofString());
    }
}

// استدعاء:
// sendFile(baseUrl, apiKey, "audio", Path.of("voice_message.ogg"), "audio/ogg");
// sendFile(baseUrl, apiKey, "image", Path.of("order_screenshot.jpg"), "image/jpeg");
```

> ⚠️ ملاحظة CRLF: الحدود (boundary) بصيغة multipart لازم تنتهي أسطرها بـ `\r\n` بالضبط (لا `\n` وحدها) وإلا يفشل الخادم بتحليل الجسم — الكود أعلاه يراعي هذا.

### شكل الاستجابة
`200 OK` — جسم JSON (`OrderConfirmation`):

```json
{
  "order_id": "ORD-000123456",
  "created_at": "2026-08-19T10:15:30.123456+00:00",
  "customer_name": "احمد",
  "customer_phone": "07701234567",
  "customer_phone2": null,
  "customer_address": "بغداد - الكرادة",
  "customer_city": "بغداد",
  "customer_district": "الكرادة",
  "state_code": "BGD",
  "items": [
    {
      "product_id": null,
      "product_name": "غسالة اتوماتيك",
      "quantity": 1,
      "unit_price": null,
      "currency": null,
      "line_total": null,
      "matched": false
    }
  ],
  "suggested_product": null,
  "subtotal": null,
  "total": null,
  "currency": null,
  "quoted_price": null,
  "notes": null,
  "confirmation_message": "تم تثبيت طلبك، وياتك بأقرب وقت ان شاء الله."
}
```

> ملاحظة: `total`/`subtotal` يرجعان فقط لو المدخل ذكر سعراً صريحاً (`quoted_price`) — لا حساب تلقائي من كتالوج محلي بهذا المسار. لو نقص أي حقل إلزامي (اسم/هاتف/عنوان/منتج فعلي)، ترجع نفس بنية الاستجابة لكن **بلا** إرسال الطلب فعلياً لنظام الطلبات الخارجي (يبقى الرد معلوماتياً فقط للعميل المستدعي).

### أخطاء محتملة
| الكود | السبب |
|---|---|
| `400` | أرسلت أكثر من حقل واحد (أو ولا حقل) من `text`/`audio`/`image` |
| `401` | مفتاح `X-API-Key` غلط |
| `422` | ما قدر النظام يفهم أي كلام من الملف الصوتي، أو فشل قراءة الصورة |
| `501` | الصورة أو الصوت يحتاجان بيئة GPU غير متوفرة حالياً على الخادم |

---

## 2) ميزة المبيعات — `sales`

> ✅ **طريقة الإرسال: `application/json` (JSON خالص)** + هيدر `X-API-Key`

### الوصف
وكيل محادثة (chatbot) يتحدث بالعامية العراقية كبائع حقيقي: يجاوب استفسارات المنتجات (يستعلم عن السعر والتوفر لحظياً من كتالوج حي)، يجمع بيانات الزبون تدريجياً (المنتج، الاسم، الهاتف، العنوان)، ولما تكتمل كل البيانات يثبّت الطلب تلقائياً ويرجعه ضمن الرد. تدعم الميزة جلسة محادثة مستمرة عبر `session_id`.

### الاستخدام
كل رسالة من الزبون تُرسل كطلب مستقل يحمل نفس `session_id` (من أول رد استلمته)، والخادم يحتفظ بذاكرة المحادثة داخلياً. أول رسالة بلا `session_id` يُنشئ الخادم واحداً جديداً ويرجعه بالاستجابة.

يوجد مساران:
- `POST /sales/chat` — رد كامل دفعة واحدة (JSON عادي).
- `POST /sales/chat/stream` — نفس المنطق لكن الرد يصل تدريجياً (Server-Sent Events / streaming)، مفيد لعرض الكتابة حرفاً-بحرف بالواجهة.

### نقطة النهاية — رد كامل

```
POST /sales/chat
Content-Type: application/json
X-API-Key: sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2
```

#### ماذا يجب أن ترسل (JSON)

| الحقل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `message` | string | ✅ | رسالة الزبون الحالية |
| `session_id` | string أو null | ❌ | معرّف الجلسة (أرجعه من أول رد لتكملة نفس المحادثة). اتركه فارغاً بأول رسالة |
| `max_tokens` | int أو null | ❌ | حد أقصى لطول الرد المولَّد (اختياري، افتراضي داخلي إن لم يُرسل) |
| `temperature` | float أو null | ❌ | درجة العشوائية بالتوليد (اختياري) |
| `image_base64` | string أو null | ❌ | صورة منتج (base64 خام، بلا بادئة `data:image/...;base64,`) — الوكيل يحللها ويطابقها مع الكتالوج، يقترح بديلاً مشابهاً لو ما لگى تطابقاً تاماً. يحتاج Pillow + خادم vLLM جاهز؛ بدونها `501` |

#### مثال — أول رسالة
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/sales/chat" \
  -H "X-API-Key: sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2" \
  -H "Content-Type: application/json" \
  -d '{"message": "عندكم غسالات اتوماتيك؟"}'
```

#### مثال — تكملة نفس المحادثة
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/sales/chat" \
  -H "X-API-Key: sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2" \
  -H "Content-Type: application/json" \
  -d '{"message": "اريدها، اسمي سارة", "session_id": "3f6e2b1a-....-...."}'
```

#### مثال — صورة منتج
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/sales/chat" \
  -H "X-API-Key: sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2" \
  -H "Content-Type: application/json" \
  -d "{\"message\": \"شنو هذا، عدكم شي مثله؟\", \"image_base64\": \"$(base64 -w0 product.jpg)\"}"
```

### شكل الاستجابة (`/sales/chat`)
`200 OK`:

```json
{
  "session_id": "3f6e2b1a-9c2d-4e1a-8f3b-1a2b3c4d5e6f",
  "answer": "أي حبيبتي، عدنا غسالة اتوماتيك 7 كيلو بسعر 350 الف دينار. أشگد اسمك الكريم؟",
  "order": null,
  "engine": "vllm",
  "tool_calls": [
    {
      "tool": "search_products",
      "args": {},
      "result": {
        "results": [
          {"id": "P-102", "name": "غسالة اتوماتيك 7 كيلو", "price": 350000, "currency": "IQD"}
        ]
      }
    }
  ]
}
```

- `order`: يبقى `null` طول المحادثة، ويمتلئ ببنية `OrderConfirmation` (نفس شكل استجابة `/orders/create` أعلاه) فقط بالدور اللي يكتمل فيه الطلب (اسم + هاتف + عنوان + منتج مذكورين فعلاً من الزبون).
- `engine`: `"vllm"` عند رد حقيقي من النموذج، أو `"fallback"` إذا الخادم بوضع بديل بلا GPU جاهز.
- `tool_calls`: للشفافية فقط. **`search_products` تُستدعى مرة وحدة لكل جلسة** — تجيب الكتالوج كاملاً ويُحقن تلقائياً بكل رسالة لاحقة، فتبقى `tool_calls` فاضية بأغلب الأدوار (الموديل يدوّر بالكتالوج المحقون بلا حاجة أداة جديدة).

### نقطة النهاية — رد متدفّق (Streaming)

```
POST /sales/chat/stream
Content-Type: application/json
X-API-Key: sk-sales-b3f7b6a1c94d4e8fa2e6c1d9f0b7a4e2
```

نفس جسم الطلب تماماً مثل `/sales/chat` (`message` + `session_id` اختياري + ...). الاستجابة هنا **ليست JSON عادي** بل `text/event-stream` (Server-Sent Events) — كل سطر بصيغة:

```
data: {"delta": "أي "}

data: {"delta": "حبيبتي"}

...

data: {"done": true, "session_id": "3f6e2b1a-...", "order": null, "tool_calls": [...]}
```

- كل رسالة `delta` هي جزء جديد من نص الرد (اربطها بالتسلسل لتكوين النص الكامل).
- الرسالة الأخيرة تحمل `"done": true` مع `session_id` النهائي و`order` (نفس بنية `OrderConfirmation` إن اكتمل الطلب، وإلا `null`) و`tool_calls`.
- استهلاكها من جهة العميل يكون بقراءة الاستجابة سطراً-سطراً (EventSource بالمتصفح، أو streaming HTTP client).

### أخطاء محتملة
| الكود | السبب |
|---|---|
| `401` | مفتاح `X-API-Key` غلط |
| `422` | جسم JSON ناقص الحقل الإلزامي `message` أو نوع بيانات غلط |

---

## 3) ميزة المتابعة الصوتية — `voice_followup`

> ✅ الخطوة 1 (`/ask`): **`application/json`** — ⚠️ الخطوة 2 (`/respond`): **`multipart/form-data` (ليس JSON)** — كلتاهما بهيدر `X-API-Key`

### الوصف
مخصّصة لمتابعة طلب موجود مسبقاً بنظام آخر (مثلاً طلب حالته "ملغي" أو "لم يتم التسليم"): تولّد **سؤالاً صوتياً** عراقياً طبيعياً حسب حالة الطلب، ترسله كملف صوت جاهز للتشغيل للزبون، تستقبل رد الزبون الصوتي، تحوّله لنص، تلخّص السبب، وترسله تلقائياً لنظام الطلبات الخارجي — وترجع صوت شكر جاهز للتشغيل. **لا تخزين دائم**: بيانات الجلسة تعيش بالذاكرة فقط بين خطوتي `/ask` و`/respond`.

### الاستخدام (تسلسل من خطوتين)
1. استدعِ `POST /voice_followup/ask` وأرسل تفاصيل الطلب (رقمه، حالته، بيانات الزبون، المنتجات) → تستلم ملف صوت WAV (السؤال) + `session_id` بالهيدرات.
2. شغّل الصوت للزبون، سجّل رده، ثم استدعِ `POST /voice_followup/respond` بنفس `session_id` + ملف صوت رد الزبون → تستلم ملف صوت شكر جاهز، مع تفاصيل التحليل بالهيدرات.

### الخطوة 1 — `POST /voice_followup/ask`

```
POST /voice_followup/ask
Content-Type: application/json
X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b
```

#### ماذا يجب أن ترسل (JSON)

| الحقل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `order_id` | string | ✅ | معرّف الطلب بنظامكم |
| `status` | string | ✅ | حالة الطلب الحالية بالعربي كما هي بنظامكم (مثلاً: "ملغي"، "مرتجع"، "لم يتم التسليم") — هي التي تحدد صياغة السؤال |
| `customer_name` | string أو null | ❌ | اسم الزبون |
| `customer_phone` | string أو null | ❌ | هاتف الزبون |
| `customer_city` | string أو null | ❌ | المحافظة |
| `customer_district` | string أو null | ❌ | المنطقة/الحي |
| `customer_address` | string أو null | ❌ | العنوان التفصيلي |
| `items` | array | ❌ | قائمة عناصر الطلب، كل عنصر: `{"product_name": "...", "quantity": 1}` |
| `reason_hint` | string أو null | ❌ | سبب أولي معروف بنظامكم إن وُجد (مثلاً "رفض الزبون الاستلام") |
| `notes` | string أو null | ❌ | ملاحظات إضافية |

#### مثال
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/voice_followup/ask" \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -H "Content-Type: application/json" \
  -o question.wav -D headers_ask.txt \
  -d '{
        "order_id": "ORD-000123456",
        "status": "لم يتم التسليم",
        "customer_name": "احمد",
        "customer_phone": "07701234567",
        "customer_city": "بغداد",
        "customer_district": "الكرادة",
        "items": [{"product_name": "غسالة اتوماتيك", "quantity": 1}],
        "reason_hint": null,
        "notes": null
      }'
```

#### شكل الاستجابة
`200 OK` — **الجسم ملف صوت خام `audio/wav`** (لا JSON) قابل للتشغيل مباشرة. التفاصيل تصل بالهيدرات:

| الهيدر | المحتوى |
|---|---|
| `X-Session-Id` | معرّف الجلسة — **احفظه** لإرساله بالخطوة 2 |
| `X-Question-Text` | نص السؤال المولَّد (Percent-encoded / RFC 5987 لأن هيدرات HTTP لا تقبل عربي مباشرة — يُفكّ بـ `urllib.parse.unquote` أو ما يعادلها بلغتك) |

مثال هيدرات الرد:
```
Content-Type: audio/wav
X-Session-Id: 8f1e2d3c-4b5a-6789-0abc-def123456789
X-Question-Text: %D9%87%D9%84%D8%A7%20%D8%A8%D9%8A%D9%83...
```

### الخطوة 2 — `POST /voice_followup/respond`

```
POST /voice_followup/respond?session_id=<من الخطوة 1>
Content-Type: multipart/form-data
X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b
```

#### ماذا يجب أن ترسل
- **Query parameter** إلزامي: `session_id` (القيمة المستلمة بهيدر `X-Session-Id` من الخطوة 1).
- **جسم multipart/form-data** بحقل ملف إلزامي:

| الحقل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `audio` | ملف (File) | ✅ | التسجيل الصوتي لرد الزبون (wav/mp3/m4a/ogg...) |

#### مثال
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/voice_followup/respond?session_id=8f1e2d3c-4b5a-6789-0abc-def123456789" \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -F "audio=@customer_reply.ogg" \
  -o thanks.wav -D headers_respond.txt
```

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

#### شكل الاستجابة
`200 OK` — **الجسم ملف صوت خام `audio/wav`** (رسالة شكر جاهزة للتشغيل). التفاصيل بالهيدرات:

| الهيدر | المحتوى |
|---|---|
| `X-Reason-Summary` | ملخّص السبب اللي فهمه النظام من كلام الزبون (Percent-encoded) |
| `X-Customer-Transcript` | نص كلام الزبون كما تحوّل من الصوت حرفياً (Percent-encoded) |
| `X-Query-Sent` | `"true"` أو `"false"` — هل انرسلت نتيجة المتابعة فعلاً لنظام الطلبات الخارجي بنجاح |

مثال هيدرات الرد:
```
Content-Type: audio/wav
X-Reason-Summary: %D8%A7%D9%84%D8%B2%D8%A8%D9%88%D9%86...
X-Customer-Transcript: %D9%85%D8%A7%20%D8%A7%D8%B3%D8%AA%D9%84%D9%85%D8%AA...
X-Query-Sent: true
```

### أخطاء محتملة
| الكود | السبب |
|---|---|
| `401` | مفتاح `X-API-Key` غلط |
| `404` | `session_id` بالخطوة 2 غير موجود أو انتهت صلاحيته (استدعِ `/ask` من جديد) |
| `422` | جسم الخطوة 1 ناقص حقلاً إلزامياً (`order_id`/`status`)، أو ما قدر النظام يفهم كلام الزبون بالخطوة 2 |
| `503` | تحويل النص لصوت (TTS) أو الصوت لنص غير متوفر حالياً على الخادم (يحتاج بيئة GPU) |

---

## 4) ميزة مكالمة الشحنة الراجعة — `voice_return`

> ✅ الخطوة 1 (`/start`): **`application/json`** — ⚠️ الخطوة 2 (`/respond`): **`multipart/form-data` (ليس JSON)** — كلتاهما بهيدر `X-API-Key` (نفس مفتاح المتابعة الصوتية)

### الوصف
مكالمة صوتية صادرة بشخصية **"صباح"** بخصوص شحنة **راجعة**: تفهم سبب الرجوع، تحاول تحلّ السبب **مرة واحدة فقط** بلا ضغط، ثم تنتهي بقرار واحد مؤكَّد بنعم/لا يُرسَل لنظام الطلبات.

الفرق الجوهري عن `voice_followup`: هذه مكالمة **متعددة الأدوار** — `/respond` يُستدعى مراراً بنفس `session_id` حتى يرجع `X-Call-Status: ended`، لا مرة واحدة.

**كل قرارات المكالمة حتمية بالخادم** (مطابقة نصية على رد الزبون) — النموذج لا يقرر شيئاً، فقط يصوغ القرار المحسوم بلهجة عراقية. الاستثناء الوحيد: تصنيف سبب الرجوع من كلام حر، وهو استخراج لا قرار.

### مراحل المكالمة (`X-Call-Stage`)

| المرحلة | ماذا تسأل صباح |
|---|---|
| `awaiting_permission` | التعريف + "إذا تسمح دقيقة؟" |
| `awaiting_identity` | تذكر الشحنة (المتجر + المبلغ) ثم "حضرتك المستلم؟" |
| `awaiting_authorization` | إذا قال "مو أنا": "حضرتك مخوّل تأكد القرار؟" |
| `awaiting_reason` | "شنو سبب رجوع الشحنة؟" |
| `awaiting_solution` | تعرض **حلاً واحداً** حسب السبب المصنَّف |
| `awaiting_final_confirm` | تلخّص القرار وتسأل نعم/لا |
| `closed` | انتهت — القرار أُرسل لنظام الطلبات |

### تصنيفات سبب الرجوع (`X-Return-Reason`) والحل المعروض

| التصنيف | متى | الحل الذي تعرضه صباح |
|---|---|---|
| `no_order` | ما طلبها أصلاً / صارت بالغلط | "ممكن أحد طالبها باسمك أو صارت من المتجر بالغلط؟ إذا لا، أثبتها راجع نهائي." |
| `no_time` | ما كان عنده وقت / ماكو أحد يستلم | "تحب نأجل توصيلها لوقت يناسبك؟" |
| `agent_issue` | المندوب ما اتصل / سوء تنسيق | "أعالجها وأخليها ترجع للتوصيل بشكل أوضح، تحب أعيد إرسالها إلك؟" |
| `hesitant` | متردد / مو متأكد | "أرتب إعادتها إلك ونخلي التوصيل اليوم، يناسبك؟" |
| `refused` | رافض الاستلام نهائياً | "أثبتها راجع نهائي؟" |

### جدول القرارات (`X-Decision`) — يُحسب بالخادم

| السبب | ردّ الزبون على الحل | القرار | الإجراء المُرسَل | الحالة الجديدة |
|---|---|---|---|---|
| `no_order` | نعم | `CUSTOMER_WILL_RECEIVE` | `TREATED` | `TRY_AGAIN` |
| `no_time` | نعم | `CONFIRMED_POSTPONED` | `APPROVAL_POSTPONED` | `APPROVAL_POSTPONED` |
| `agent_issue` | نعم | `CUSTOMER_WILL_RECEIVE` | `TREATED` | `TRY_AGAIN` |
| `hesitant` | نعم | `CUSTOMER_READY_NOW` | `TREATED` | `TRY_AGAIN` |
| `refused` | نعم | `CONFIRMED_RETURN` | `RETURN_APPROVED` | `APPROVAL_RETURN` |
| أي سبب | لا | `CONFIRMED_RETURN` | `RETURN_APPROVED` | `APPROVAL_RETURN` |
| رقم غلط / غير مخوّل / تعذّر الفهم | — | `NO_ACTION_NEEDED` | `NO_ACTION_NEEDED` | **بلا تغيير** |

> `refused` + "لا" حالة تضارب (رفض الاستلام ورفض إثبات الرجوع): لا تُحسم، بل تُعاد لسؤال السبب.

### الخطوة 1 — `POST /voice_return/start`

```
POST /voice_return/start
Content-Type: application/json
X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b
```

#### ماذا يجب أن ترسل (JSON)

| الحقل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `order_id` | string | ✅ | معرّف الطلب بنظامكم — **لا يُنطق بالمكالمة**، يُستخدم بالإرسال النهائي فقط |
| `status` | string | ❌ | حالة الشحنة (افتراضياً `"راجع"`) |
| `store_name` | string أو null | ❌ | اسم المتجر/المرسل — يُذكر بالافتتاح |
| `amount` | number أو null | ❌ | مبلغ الشحنة — يُذكر بالافتتاح |
| `currency` | string أو null | ❌ | العملة (مثلاً "دينار") |
| `items` | array | ❌ | `{"product_name": "...", "quantity": 1}` |
| `return_reason_hint` | string أو null | ❌ | ملاحظة النظام عن سبب الرجوع إن وُجدت |
| `goods_description` | string أو null | ❌ | وصف داخلي — **لا يُذكر إلا إذا سأل الزبون** |
| `tracking_number` | string أو null | ❌ | رقم تتبع داخلي — **لا يُذكر إلا إذا سأل الزبون** |

> 🔒 لا يوجد حقل لاسم الزبون أو عنوانه أو محافظته — الدليل يمنع ذكرها بالمكالمة نهائياً، فلا تُرسَل للنموذج أصلاً.

#### مثال
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/voice_return/start" \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -H "Content-Type: application/json" \
  -o opening.wav -D headers_start.txt \
  -d '{
        "order_id": "ORD-000123456",
        "status": "راجع",
        "store_name": "متجر النور",
        "amount": 75000,
        "currency": "دينار",
        "items": [{"product_name": "غسالة اتوماتيك", "quantity": 1}],
        "return_reason_hint": "لم يطلب"
      }'
```

#### شكل الاستجابة
`200 OK` — **الجسم ملف صوت خام `audio/wav`**. التفاصيل بالهيدرات:

| الهيدر | المحتوى |
|---|---|
| `X-Session-Id` | معرّف الجلسة — **احفظه** لكل أدوار `/respond` |
| `X-Reply-Text` | نص جملة الافتتاح (Percent-encoded) |
| `X-Call-Status` | `continue` دائماً بهذه الخطوة |

### الخطوة 2 — `POST /voice_return/respond` (تتكرر)

```
POST /voice_return/respond?session_id=<من الخطوة 1>
Content-Type: multipart/form-data
X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b
```

| الحقل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `session_id` | query string | ✅ | من هيدر `X-Session-Id` بالخطوة 1 |
| `audio` | file | ✅ | تسجيل رد الزبون (wav/mp3/m4a) |

#### مثال
```bash
curl -X POST "https://1spx1ivrqcvt1h-8000.proxy.runpod.net/voice_return/respond?session_id=8f1e2d3c-..." \
  -H "X-API-Key: sk-voicefu-4e6a8c0b2d4f6a8c0e2b4d6f8a0c2e4b" \
  -F "audio=@customer_reply.wav" \
  -o sabah_reply.wav -D headers_respond.txt
```

#### شكل الاستجابة
`200 OK` — **الجسم ملف صوت رد صباح `audio/wav`**. التفاصيل بالهيدرات:

| الهيدر | المحتوى |
|---|---|
| `X-Call-Status` | `continue` = أرسل دوراً آخر · `ended` = انتهت المكالمة |
| `X-Call-Stage` | المرحلة الحالية (انظر جدول المراحل أعلاه) |
| `X-Return-Reason` | تصنيف سبب الرجوع بعد استخراجه، أو فارغ |
| `X-Decision` | القرار النهائي — يمتلئ فقط عند `ended` |
| `X-Customer-Transcript` | نص رد الزبون (Percent-encoded) |
| `X-Reply-Text` | نص رد صباح (Percent-encoded) |
| `X-Result-Sent` | `true`/`false` — هل وصل القرار لنظام الطلبات (فارغ ما لم تنتهِ المكالمة) |

> ♻️ **حلقة الاستدعاء**: كرّر `/respond` بنفس `session_id` طالما `X-Call-Status: continue`. سكوت الزبون **ليس خطأ** — يُعامَل كرد غامض: تعيد صباح السؤال مرة، ثم تقفل بأدب.

### أخطاء محتملة
| الكود | السبب |
|---|---|
| `401` | مفتاح `X-API-Key` غلط |
| `404` | `session_id` غير موجود أو انتهت صلاحيته (٣٠ دقيقة) — استدعِ `/start` من جديد |
| `422` | جسم الخطوة 1 ناقص `order_id` |
| `503` | تحويل النص لصوت أو الصوت لنص غير متوفر (يحتاج بيئة GPU) |

---

## ملخص سريع

| الميزة | Endpoint | نوع الجسم | المفتاح |
|---|---|---|---|
| إنشاء الطلبات | `POST /orders/create` | `multipart/form-data` (حقل واحد: `text` أو `audio` أو `image`) | `sk-orders-...` |
| المبيعات (رد كامل) | `POST /sales/chat` | `application/json` (`message`, `session_id`) | `sk-sales-...` |
| المبيعات (بث) | `POST /sales/chat/stream` | `application/json` (نفس أعلاه) | `sk-sales-...` |
| المتابعة الصوتية — سؤال | `POST /voice_followup/ask` | `application/json` (تفاصيل الطلب) | `sk-voicefu-...` |
| المتابعة الصوتية — رد | `POST /voice_followup/respond?session_id=...` | `multipart/form-data` (`audio`) | `sk-voicefu-...` |
| مكالمة الراجع — بدء | `POST /voice_return/start` | `application/json` (تفاصيل الشحنة) | `sk-voicefu-...` |
| مكالمة الراجع — دور | `POST /voice_return/respond?session_id=...` | `multipart/form-data` (`audio`) | `sk-voicefu-...` |
