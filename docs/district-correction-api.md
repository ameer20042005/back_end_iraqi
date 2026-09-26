# مراسلة خدمة تصحيح المناطق

هذه ميزة ضمن الباك اند الحالي على `http://127.0.0.1:8000`. نقطة المعالجة هي **`POST /v1/district-correction`**. يستخدم Spring V2 عنوان الباك اند المنشور بدلاً من الرابط المحلي. الطلب والاستجابة `application/json`؛ لا تُرفع ملفات Excel إلى هذه النقطة. يقرأ Spring الصفوف من Excel ويرسلها كمصفوفة `cases`.

## الحماية

لكل طلب تصحيح أرسل الهيدر:

```http
X-API-Key: <district_api_key>
Content-Type: application/json
```

المفتاح هو الحقل `district_api_key` في [app/config.py](../app/config.py) مثل بقية مفاتيح الخدمات؛ ضع القيمة نفسها في إعدادات عميل Spring أو متغير Postman `district_api_key`. هذا المفتاح مستقل عن مفاتيح المبيعات وإنشاء الطلبات. المقارنة تتم في [auth.py](../app/features/district_correction/auth.py)؛ غياب الهيدر يرجع `422`، والمفتاح الخاطئ `401`، وعدم ضبط مفتاح الخادم `500`. `GET /health` و`GET /v1/district-correction/ready` لا يحتاجان المفتاح.

## الإعداد والتشغيل

الميزة محمّلة في `app/main.py` وتعمل مع بقية الراوترات على المنفذ `8000`؛ لا توجد عملية أو Docker منفصل لها. شغّل الأمر المعتاد:

```powershell
.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

عند الإقلاع، يُستورد كتالوج Excel من `assets/address` تلقائياً إذا لم توجد قاعدة SQLite أو كان أي ملف Excel أحدث منها. لإعادة البناء يدوياً مع تقرير الاستيراد، ثم أعد تشغيل الباك اند لتحميل النسخة الجديدة في الذاكرة:

```powershell
.venv\Scripts\python.exe -m app.features.district_correction.import_excel_catalogs --report docs/district-correction-import-report.json
```

الحقول `district_source_dir` و`district_database_path` في `app/config.py` تقبل تجاوز المسار عبر `DISTRICT_SOURCE_DIR` و`DISTRICT_DATABASE_PATH`. الاستيراد يسجل الصفوف المكررة أو غير الصالحة في [تقرير الاستيراد](district-correction-import-report.json).
مسار LLM اختياري؛ اضبط `DISTRICT_LLM_BASE_URL` و`DISTRICT_LLM_MODEL` و`DISTRICT_LLM_API_KEY` عند الحاجة، و`DISTRICT_LLM_TIMEOUT_SECONDS` للمهلة، و`DISTRICT_LLM_MAX_CASES` (الافتراضي 100) لأقصى عدد حالات تُرسل للـLLM في الطلب الواحد. يمكن استخدام عنوان vLLM الحالي مثل `http://127.0.0.1:18001/v1` والموديل المحدد في `app/config.py`. بلا هذه المتغيرات تبقى المطابقة الحتمية فعالة وتعود الحالات الغامضة `UNRESOLVED`.

## شكل الطلب

```json
{
  "companyName": "FUHOOD",
  "cases": [
    {
      "excelSequence": 1,
      "stateName": "DHI_QAR",
      "stateCode": "DHI",
      "district": "شطره",
      "address": "الشطرة قرب السوق العام"
    },
    {
      "excelSequence": 2,
      "stateName": "BAGHDAD",
      "stateCode": "BGD",
      "district": "الكرادة شارع الرشيد، بناية 10",
      "address": ""
    },
    {
      "excelSequence": 3,
      "stateName": "BAGHDAD",
      "stateCode": "BGD",
      "district": "حي غير واضح",
      "address": "قرب الجامع"
    }
  ]
}
```

| الحقل | النوع | المطلوب | المعنى |
|---|---|---|---|
| `companyName` | string | نعم | اسم الشركة كما في الكتالوج: `ALZAEEM`, `FUHOOD`, `KHAYAL`, `RIYAM`, `TEST`. |
| `cases` | array | نعم | من 1 إلى 10,000 حالة في الطلب الواحد. تبقى بالترتيب نفسه في الرد. |
| `excelSequence` | integer | نعم | رقم فريد لكل صف داخل الطلب؛ يستخدمه Spring لربط الرد بصف Excel. |
| `stateCode` | string | نعم | رمز المحافظة الرسمي مثل `BGD` أو `DHI`، حتى 20 حرفاً. يقيّد البحث قبل المطابقة. |
| `stateName` | string | لا | اسم المحافظة العربي أو الإنجليزي للتحقق من اتساقه مع `stateCode`، حتى 100 حرف. |
| `district` | string | لا | نص المنطقة الخام، حتى 300 حرف؛ إن كان فارغاً تبقى الحالة غير محسومة. |
| `address` | string | لا | تفاصيل العنوان الخام، حتى 1000 حرف؛ يمكن أن يكون فارغاً. |

يمكن أن تضم الدفعة محافظات متعددة **للشركة نفسها**. لإرسال شركة ثانية، استخدم طلباً منفصلاً باسمها. لا تضع اسم المنطقة والتفاصيل في حقل واحد عن قصد، لكن الخدمة تفصلها إن وصلتها هكذا.

## شكل الاستجابة `200 OK`

للمثال أعلاه، ومع تعطيل LLM الاختياري:

```json
{
  "companyName": "FUHOOD",
  "cases": [
    {
      "excelSequence": 1,
      "originalDistrict": "شطره",
      "correctDistrict": "الشطرة",
      "addressDetails": "قرب السوق العام",
      "confidence": 0.95,
      "status": "NORMALIZED_MATCH",
      "reason": "Unique spelling-normalized catalog match.",
      "stateCode": "DHI",
      "errorCode": null
    },
    {
      "excelSequence": 2,
      "originalDistrict": "الكرادة شارع الرشيد، بناية 10",
      "correctDistrict": "الكرادة",
      "addressDetails": "شارع الرشيد، بناية 10",
      "confidence": 0.94,
      "status": "SPLIT_ADDRESS",
      "reason": "Catalog district extracted from start of district field.",
      "stateCode": "BGD",
      "errorCode": null
    },
    {
      "excelSequence": 3,
      "originalDistrict": "حي غير واضح",
      "correctDistrict": "حي غير واضح",
      "addressDetails": "قرب الجامع",
      "confidence": 0.2,
      "status": "UNRESOLVED",
      "reason": "No reliable catalog match; original district kept.",
      "stateCode": "BGD",
      "errorCode": null
    }
  ]
}
```

`correctDistrict` يساوي الاسم الرسمي **حرفياً** من كتالوج `companyName + stateCode` لكل حالة محلولة. `originalDistrict` يبقى كما أرسله Spring. `addressDetails` يحذف بادئة المنطقة المكررة فقط ويحفظ بقية تفاصيل العنوان. درجات الثقة محسوبة من مسار المطابقة؛ لا تؤخذ من رقم يعيده LLM.

| `status` | المعنى |
|---|---|
| `EXACT_MATCH` | اسم مطابق حرفياً لكتالوج الشركة والمحافظة. |
| `NORMALIZED_MATCH` | اختلاف كتابة واضح وفريد بعد التطبيع للبحث. |
| `SPLIT_ADDRESS` | بداية النص منطقة رسمية والباقي تفاصيل عنوان، أو تكررت المنطقة ببداية العنوان. |
| `FUZZY_MATCH` | تشابه مرتفع وفارق واضح عن المرشح التالي. |
| `AI_MATCH` | اختار LLM مرشحاً من القائمة المسموحة واجتاز فحص العضوية والتسلسل والمحافظة. |
| `UNRESOLVED` | لا يوجد اختيار موثوق؛ يُحفظ `district` و`address` الأصليان. |

## ما يفعله Spring بعد الرد

1. اربط كل عنصر بردّه عبر `excelSequence`، ولا تعتمد على ترتيب المصفوفة وحده.
2. إذا كان `status != UNRESOLVED`، ابحث عن `cdi_id` باستخدام **الشركة نفسها** و`stateCode` و`correctDistrict` الرسمي. الاستعلام `findByCdiNameAndCdiStCode(correctDistrict, stateCode)` صالح فقط إذا كان المستودع أصلاً مقيداً بكتالوج تلك الشركة.
3. خزّن `addressDetails` في حقل العنوان التفصيلي. عند `UNRESOLVED` اعرض الحالة للمراجعة ولا تُحوّل النص الخام إلى `cdi_id` بالتخمين.
4. إذا ظهر `errorCode` على صف، عالج ذلك الصف؛ لا تفترض فشل الدفعة كلها.

للتتبع يمكن إرسال `X-Request-ID` اختياري؛ يظهر في سجل الخدمة دون تسجيل عنوان العميل. نقطة التصحيح بلا حالة؛ يمكن تقسيم ملف كبير إلى دفعات حتى 10,000 حالة لكل طلب.

## الأخطاء

| HTTP | المثال | التصرف |
|---|---|---|
| `401` | `{"detail":"مفتاح API غير صحيح لخدمة تصحيح المناطق"}` | صحح `X-API-Key`. |
| `422` | `{"detail":[{"type":"missing","loc":["header","X-API-Key"],"msg":"Field required","input":null}]}` | الهيدر مفقود، أو جسم الطلب غير صالح، أو `excelSequence` مكرر. |
| `404` | `{"detail":{"code":"UNKNOWN_COMPANY"}}` | أرسل إحدى الشركات الموجودة. |
| `503` | `CATALOG_UNAVAILABLE` أو `NOT_READY` | افحص `/v1/district-correction/ready` و[تقرير الاستيراد](district-correction-import-report.json). |
| `500` | رسالة عدم ضبط المفتاح أو خطأ داخلي | راجع `district_api_key` في `app/config.py` وسجلات الخادم. |

المحافظة غير المعروفة، أو كتالوج الشركة الفارغ لتلك المحافظة، أو مهلة LLM، أو رد LLM غير صالح، أو تجاوز حد `DISTRICT_LLM_MAX_CASES`: تعود **على مستوى الصف** بحالة `UNRESOLVED` و`errorCode` من `UNKNOWN_STATE`, `EMPTY_DISTRICT_CATALOG`, `LLM_TIMEOUT`, `LLM_INVALID_RESPONSE`, `LLM_LIMIT_EXCEEDED`، مع بقاء بقية الصفوف قابلة للمعالجة.

## تجربة Postman

استورد [district-correction-postman.json](district-correction-postman.json)، ثم افتح المجموعة → **Variables**:

1. `baseUrl` مضبوط على رابط الباك اند المنشور نفسه في [postman_collection.json](postman_collection.json)؛ غيّره إلى `http://127.0.0.1:8000` للتجربة المحلية.
2. `district_api_key` مضبوط على قيمة `district_api_key` في `app/config.py`.
3. شغّل `Ready` للتأكد من تحميل الكتالوج، ثم `تصحيح دفعة — محافظتان وعنوان مفصول`. داخل المجموعة طلبان إضافيان للتحقق من رفض المفتاح الخاطئ والمفقود، وطلب لشركة غير معروفة.

للتجربة عبر curl:

```bash
curl -X POST http://127.0.0.1:8000/v1/district-correction \
  -H 'X-API-Key: YOUR_DISTRICT_SERVICE_KEY' \
  -H 'Content-Type: application/json' \
  -d '{"companyName":"FUHOOD","cases":[{"excelSequence":1,"stateCode":"DHI","district":"شطره","address":"الشطرة قرب السوق العام"}]}'
```
