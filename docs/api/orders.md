# إنشاء الطلبات — `POST /orders/create`

## الوصف
تُنشئ طلباً واحداً من **مدخل وحيد فقط** (نص، أو رسالة صوتية، أو صورة) — الخادم يفهم المدخل، يستخرج بيانات الزبون والمنتجات، يحسب السعر من قيمة `quoted_price` المذكورة صراحة، ثم يعيد تأكيد طلب جاهز. لو كانت كل الحقول الإلزامية مكتملة (اسم + هاتف + عنوان + منتج) يُرسل الطلب تلقائياً لنظام الطلبات الخارجي أيضاً.

## الاستخدام (الحالات)
- **نص**: رسالة واتساب/شات مكتوبة من الزبون بالعامية العراقية.
- **صوت**: تسجيل صوتي (WhatsApp voice note مثلاً) يتحول تلقائياً لنص عبر Gemma المتعدد الوسائط.
- **صورة**: لقطة شاشة محادثة، أو صورة منتج، أو قائمة مكتوبة بخط اليد.

## الطلب

`multipart/form-data` — **مدخل واحد بس** من الثلاثة، بدون محادثة (طلب مباشر).

| الحقل (form) | النوع | الوصف |
|---|---|---|
| `text` | string | نص مباشر يصف الطلب |
| `audio` | file | ملف صوتي (wav/mp3/m4a/ogg...) — يتحول لنص عربي تلقائياً (Gemma، دون نموذج نسخ محلي)، ثم يُستخرج منه الطلب. الملفات الأطول من 30 ثانية تُقطَّع تلقائياً فما يضيع منها شي |
| `image` | file | صورة (طلب مكتوب بخط اليد، لقطة شاشة محادثة، صورة منتج...) — تُقرأ مباشرة بقدرة الموديل البصرية الأصلية ويُستخرج منها الطلب **باستدعاء واحد** (بدون خطوة وصف نصي وسيطة) |

## أمثلة

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

**ملاحظة — تصحيح المحافظة تلقائياً**: هذا المسار يستخدم برومت `plane.md` (data/locations/) مع مرجع جغرافي من `states.xlsx`/`districts.xlsx` (18 محافظة، ~4900 منطقة → `app/rag/locations.json`). المناطق الواردة بنص الزبون تُطابَق قبل التوليد وتُحقن بالبرومت، وبعد الاستخراج إذا كانت المنطقة معروفة وتتبع محافظة واحدة تُعتمد محافظتها حتمياً بدل تخمين الموديل، ويُرجَع كودها بـ `state_code`. بعد أي تحديث لملفي الإكسل: `python -m app.rag.prepare_locations`.

## أخطاء محتملة

| كود | السبب | الرسالة |
|---|---|---|
| `400` | ما زوّدت أي مدخل، أو زوّدت أكثر من وحد | "زوّد مدخل واحد بس: text أو audio أو image." |
| `422` | ملف صوتي مو مفهوم/فاضي | "ما كدرنا نفهم أي كلام بالملف الصوتي." |
| `501` | مدخل `image`/`audio` بسيرفر ماعنده `transformers`/`torch`/`Pillow` مثبَّتة (يصير محلياً بدون GPU؛ ما لازم يصير على RunPod بعد تثبيت `requirements-gpu.txt`) | نص يوضح السبب |
| `503` | تحويل الصوت لنص غير متوفر بالسيرفر | "تحويل الصوت لنص غير متوفر محلياً..." |

**ملاحظة**: مدخل `image` يستخدم نفس محرك vLLM ونفس أوزان الموديل المستخدَمة بـ `/sales/chat` — ماكو موديل ثانٍ يتحمّل ولا استهلاك ذاكرة إضافي.

## إرسال الصورة أو الصوت من Java

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

> ملاحظة: `total`/`subtotal` يرجعان فقط لو المدخل ذكر سعراً صريحاً (`quoted_price`) — لا حساب تلقائي من كتالوج محلي بهذا المسار. لو نقص أي حقل إلزامي (اسم/هاتف/عنوان/منتج فعلي)، ترجع نفس بنية الاستجابة لكن **بلا** إرسال الطلب فعلياً لنظام الطلبات الخارجي (يبقى الرد معلوماتياً فقط للعميل المستدعي).

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
