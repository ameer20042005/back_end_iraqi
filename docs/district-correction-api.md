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
تستخدم الخدمة خادم vLLM والموديل المحددين في `app/config.py` افتراضياً لتحليل الحالات التي ليست مطابقة حرفياً. يمكن تجاوزهما عبر `DISTRICT_LLM_BASE_URL` و`DISTRICT_LLM_MODEL`، وضبط `DISTRICT_LLM_API_KEY` عند الحاجة، و`DISTRICT_LLM_TIMEOUT_SECONDS` للمهلة، و`DISTRICT_LLM_MAX_CASES` (الافتراضي 100) لأقصى عدد حالات تُرسل للـLLM في الطلب الواحد. عند تعذر الوصول إلى النموذج تعود الحالة `UNRESOLVED` مع رمز الخطأ المناسب.

تُرسل الحالات في دفعات من 20 صفاً، وتعمل دفعتان بالتوازي افتراضياً لكل طلب. يمكن ضبط `DISTRICT_LLM_CONCURRENCY`، واستخدام القيمة `1` لخادم لا يستفيد من التوازي. يُحجز حد الحالات قبل بدء الدفعات، وتبقى النتائج بترتيب الطلب. تُعاد الاستفادة من حسابات المطابقة المتكررة، وتراعي قائمة مرشحي النموذج الكلمات الملتصقة. رد النموذج المكرر لنفس `excelSequence` يُرفض، ولا يمحو حقل `addressDetails` الفارغ العنوان الأصلي. عند فشل التحقق، تُحفظ المطابقة المحلية الموثوقة إن وجدت؛ وإلا تبقى الحالة `UNRESOLVED`.

**مركز المحافظة سياق لا منطقة:** إذا بدأ النص بمركز محافظة (الناصرية، الديوانية، الحلة، الموصل، كربلاء، النجف...) وتلته منطقة موجودة في الكتالوج، تُختار المنطقة لا المركز: «الناصريه الشطره» ← `الشطرة`، «الحله المسيب» ← `المسيب`. وتُقبل البقية أيضاً بخطأ طباعي واحد ضمن الميزانية («كربلاء طوريج» ← `طويريج`) أو إذا كانت بداية اسم واحد فقط في الكتالوج («كربلاء حي الامن» ← `حي الامن الداخلي`)، بثقة 0.9. إذا كانت البقية تشبه منطقة شبهاً بعيداً فقط («بغداد الرضوانيه») يبقى المركز بثقة 0.8 ويُرسَل للنموذج، وموافقة النموذج عليه لا ترفع الثقة إلى 0.97. تفاصيل العنوان التي يعيدها النموذج تُنظَّف من اسم المحافظة أو المركز والكلمات الوصفية المنفردة («ميسان»، «البصره قضاء»). أما «الكرادة شارع الرشيد» فتبقى `الكرادة` لأنها منطقة وليست مركز محافظة.

**متى يعمل النموذج:** القواعد تحسم المؤكد فقط: الاسم مطابق حرفياً، أو مطابق بعد توحيد الكتابة، أو مع اسم المحافظة أو كلمة وصفية (حي، مجمع...) فقط. كل ما عدا ذلك يقرره النموذج مهما كان حجم الطلب: فصل العنوان، الأخطاء الطباعية، مركز المحافظة، والنص غير المحلول. نتيجة القواعد تُرسل له تلميحاً (`suggestedDistrict`)، وتُستخدم احتياطاً إذا تعذّر الوصول إليه. يُقبل اختيار النموذج فقط إذا كان من كتالوج الشركة والمحافظة، **ومكتوباً في النص** ولو بأخطاء إملائية؛ الاختيار الذي لا يذكره النص («البصره» ← الزبير) يُرفض. طلبات النموذج هنا بلا تفكير (`enable_thinking: false`) وبـ `temperature: 0`، والمهلة الافتراضية 60 ثانية.

لقياس الأداء محلياً دون الاتصال بالنموذج: `.venv\Scripts\python.exe -m scripts.benchmark_district_correction`. يستخدم القياس الكتالوج الحقيقي ونموذجاً محاكياً بمهلة ثابتة لقياس توازي الدفعات؛ زمن استدلال النموذج الحقيقي يعتمد على الخادم.

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

`correctDistrict` يساوي الاسم الرسمي **حرفياً** من كتالوج `companyName + stateCode` لكل حالة محلولة. `originalDistrict` يبقى كما أرسله Spring. في الحالات التي يحللها النموذج، يحمل `addressDetails` بقية تفاصيل الموقع والعنوان بعد فصل المنطقة والمحافظة؛ وفي المطابقة الحرفية تُحذف بادئة المنطقة المكررة من العنوان. درجات الثقة محسوبة من مسار المطابقة؛ لا تؤخذ من رقم يعيده LLM.

| `status` | المعنى |
|---|---|
| `EXACT_MATCH` | اسم مطابق حرفياً لكتالوج الشركة والمحافظة. |
| `NORMALIZED_MATCH` | اختلاف كتابة واضح وفريد بعد التطبيع للبحث. |
| `SPLIT_ADDRESS` | استُخرج اسم منطقة من النص الكامل ونُقلت تفاصيل الموقع الأخرى إلى `addressDetails`؛ يُقبل اختيار النموذج فقط إذا طابق كتالوج الشركة والمحافظة. |
| `FUZZY_MATCH` | تشابه مرتفع وفارق واضح عن المرشح التالي. |
| `AI_MATCH` | اختار LLM مرشحاً من القائمة المسموحة واجتاز فحص العضوية والتسلسل والمحافظة. |
| `UNRESOLVED` | لا يوجد اختيار موثوق؛ يُحفظ `district` و`address` الأصليان. |

## ما يفعله Spring بعد الرد

1. اربط كل عنصر بردّه عبر `excelSequence`، ولا تعتمد على ترتيب المصفوفة وحده.
2. إذا كان `status != UNRESOLVED`، ابحث عن `cdi_id` باستخدام **الشركة نفسها** و`stateCode` و`correctDistrict` الرسمي. الاستعلام `findByCdiNameAndCdiStCode(correctDistrict, stateCode)` صالح فقط إذا كان المستودع أصلاً مقيداً بكتالوج تلك الشركة.
3. خزّن `addressDetails` في حقل العنوان التفصيلي. عند `UNRESOLVED` اعرض الحالة للمراجعة ولا تُحوّل النص الخام إلى `cdi_id` بالتخمين.
4. إذا ظهر `errorCode` على صف، عالج ذلك الصف؛ لا تفترض فشل الدفعة كلها.

للتتبع يمكن إرسال `X-Request-ID` اختياري؛ يظهر في سجل الخدمة دون تسجيل عنوان العميل. يمكن تقسيم ملف كبير إلى دفعات حتى 10,000 حالة لكل طلب.

## ذاكرة التصحيحات

تحفظ الذاكرة نص الزبون مع منطقته لكل شركة ومحافظة، فيُحسم النص نفسه في الطلبات اللاحقة دون LLM. مصدران يغذّيانها دون أي تعديل على Spring:

1. **التعلّم التلقائي** (`DISTRICT_AUTO_LEARN`، مفعّل افتراضياً): عندما تتفق طريقتان مستقلتان على المنطقة في صف كان يحتاج الـLLM، أي LLM مع نتيجة القواعد، أو LLM مع أول اختيار للبحث الدلالي، أو القواعد مع البحث الدلالي. يُحسم التكرار بثقة `0.95` والسبب `Learned from an earlier answer two independent methods agreed on.`. اختيار LLM يخالف القواعد والبحث الدلالي معاً لا يُحفظ.
2. **تأكيد المراجع** من صفحة الاختبار `/test` ← تصحيح المناطق: لكل صف في النتيجة عمود «مراجعة» بقائمة أسماء كتالوج الشركة والمحافظة وزر «تأكيد». يُحسم التكرار بثقة `0.98`. تأكيد المراجع يستبدل أي إجابة سابقة، والتعلّم التلقائي لا يستبدل تأكيد مراجع أبداً. الصفوف المحسومة من الذاكرة تظهر بشارة «من الذاكرة · مؤكَّد» أو «من الذاكرة · تلقائي».

قائمة الأسماء في الصفحة تأتي من `GET /v1/district-correction/districts?companyName=FUHOOD&stateCode=BGD` (بنفس المفتاح). `/ready` يعرض `rememberedCorrections` بعدد كل مصدر و`autoLearn`.

### `POST /v1/district-correction/feedback`

تستخدمه صفحة الاختبار، ويمكن لأي عميل (Spring لاحقاً إن أُضيف) إرسال النص الأصلي والمنطقة المؤكدة بنفس الهيدر `X-API-Key`:

```json
{
  "companyName": "FUHOOD",
  "corrections": [
    {"stateCode": "BGD", "district": "بغداد الدورة ابو دشير شارع الزيتون", "correctDistrict": "ابو دشير"},
    {"stateCode": "ARB", "district": "اربيل حاكماوه", "correctDistrict": "حاجياوا"}
  ]
}
```

الرد: `{"companyName": "FUHOOD", "saved": 2, "removed": 0, "rejected": []}`. الصف المرفوض يظهر برقمه في المصفوفة ورمز `UNKNOWN_STATE` أو `UNKNOWN_DISTRICT` (الاسم ليس حرفياً في كتالوج الشركة والمحافظة) أو `EMPTY_TEXT` (النص اسم المحافظة فقط) أو `MISSING_DISTRICT`.

بعدها أي طلب تصحيح للشركة والمحافظة نفسيهما بنفس النص (بغض النظر عن ة/ه وى/ي والهمزات وال التعريف والترقيم واسم المحافظة) يُحسم مباشرة دون LLM بثقة `0.98` والسبب `Confirmed correction remembered for this company and governorate.`؛ ما تبقى من النص بعد المنطقة يذهب إلى `addressDetails`. إرسال منطقة مختلفة لنفس النص يستبدل القديمة. للتراجع عن تأكيد خاطئ أرسل الجسم نفسه بـ `DELETE /v1/district-correction/feedback` (الحقل `correctDistrict` اختياري هنا) والرد يحمل `removed`.

الذاكرة في `app/features/district_correction/data/aliases.sqlite3` (أو `DISTRICT_ALIAS_DATABASE_PATH`)، منفصلة عن الكتالوج فلا تُمسح عند إعادة استيراد Excel. إذا حُذف اسم من الكتالوج لاحقاً يُتجاهل تصحيحه المحفوظ. لإيقاف التعلّم التلقائي: `DISTRICT_AUTO_LEARN=false`.

## النموذج المدرَّب: القواعد ثم النموذج ثم الـLLM

نموذج Reranker يُدرَّب في [training/](../training/README.md) على بيانات مرجعية. له وضعان يحدّدهما `DISTRICT_RERANKER_MODE`:

**`decide` (الافتراضي): الطرق الثلاث تعمل معًا بالترتيب.** النموذج يقرأ كل صف لم تحسمه القواعد، ثم:

| حالة الصف | النتيجة | هل يُرسل للـLLM؟ |
|---|---|---|
| القواعد والنموذج اتفقا على منطقة | جواب القواعد، الثقة ≥ 0.95، والسبب ينتهي بـ "The trained model agrees." | لا |
| القواعد وجدت منطقة والنموذج اختار غيرها أو لم يجد | الـLLM يحكم بينهما (مع كل حواجزه) | نعم |
| القواعد لم تجد منطقة أو وجدت المركز فقط، والنموذج واثق | اختيار النموذج، الثقة 0.9 | لا |
| القواعد لم تجد منطقة والنموذج غير واثق | تبقى نتيجة القواعد (UNRESOLVED أو المركز) للمراجعة | لا |

على 554 صفًا مرجعيًا لم يرها النموذج في التدريب: الدقة 96.8% مقابل 95.3% حين يمرّ كل صف غير محسوم على الـLLM، والأخطاء الخطيرة (منطقة خاطئة أو منطقة لنص لا يذكر منطقة) 12 بدل 18، ووصل للـLLM نحو 24 صفًا بدل 373. تخمين الـLLM للصفوف التي لم تجدها القواعد ولا النموذج زاد المطابقات الكاذبة أكثر مما أصلح، لذلك تبقى للمراجعة. إن فشل النموذج أثناء الطلب يعود الطلب تلقائيًا للمسار القديم (كل الصفوف غير المحسومة للـLLM).

**`shadow`:** النموذج يعمل بالتوازي مع الـLLM ولا يغيّر الإجابة.

في الوضعين يظهر اختيار النموذج في الحقل `modelDistrict` (`null` إذا لم يجد منطقة واثقًا منها)، وصفحة `/test` تعرضه بشارة خضراء عند اتفاقه مع الإجابة وبرتقالية عند اختلافه. على Spring تجاهل هذا الحقل. كل صف يمرّ عليه النموذج يُسجَّل سطرًا في `reranker_shadow.jsonl`: النص، اختيار القواعد، الإجابة النهائية، اختيار النموذج ودرجته. سجل الطلب يضيف `model_cases` و`model_agreed` و`model_settled` (الصفوف التي حسمها دون الـLLM)، و`/ready` يعرض `reranker` و`rerankerMode`.

| المتغير | المعنى |
|---|---|
| `DISTRICT_RERANKER_PATH` | مجلد النموذج، مثل `training/models/reranker`؛ فارغ = معطّل والخدمة تعمل بالقواعد والـLLM فقط |
| `DISTRICT_RERANKER_MODE` | `decide` (الافتراضي) أو `shadow` |
| `DISTRICT_RERANKER_LOG_PATH` | ملف السجل؛ الافتراضي `app/features/district_correction/data/reranker_shadow.jsonl` |

يحتاج torch وtransformers، وهما مثبّتان على RunPod أصلًا. إن تعذّر تحميل النموذج يُسجَّل الخطأ وتعمل الخدمة بدونه.

## البحث الدلالي (اختياري)

طبقة داعمة تعمل فقط على الصفوف التي كانت ستذهب للـLLM: يحوّل موديل Embeddings أسماء الكتالوج ونص الزبون إلى متجهات ويرتّب الأسماء حسب المعنى.

- إذا كان أقرب اسم دلالياً هو نتيجة القواعد نفسها، بفارق لا يقل عن `DISTRICT_EMBEDDING_AGREE_MARGIN` (الافتراضي `0.03`) عن الاسم التالي، تُحسم الحالة دون LLM ويُضاف للسبب `Semantic search agrees.`.
- غير ذلك تُرسل الحالة للـLLM مع أقرب الأسماء ضمن قائمة المرشحين وفي الحقل `similarDistricts`، وتبقى شروط قبول اختياره كما هي.
- عند تعذر الوصول لخادم الـEmbeddings تعمل الخدمة كما لو كانت الطبقة معطلة.

التفعيل بأي خادم متوافق مع `/v1/embeddings` (vLLM أو Text Embeddings Inference):

| المتغير | المعنى |
|---|---|
| `DISTRICT_EMBEDDING_BASE_URL` | مثل `http://127.0.0.1:8001`؛ فارغ = معطلة |
| `DISTRICT_EMBEDDING_MODEL` | مثل `BAAI/bge-m3` أو `intfloat/multilingual-e5-base` |
| `DISTRICT_EMBEDDING_API_KEY` | عند الحاجة |
| `DISTRICT_EMBEDDING_QUERY_PREFIX` / `DISTRICT_EMBEDDING_PASSAGE_PREFIX` | موديلات e5 تحتاج `query: ` و`passage: `؛ bge-m3 بدونهما |
| `DISTRICT_EMBEDDING_TIMEOUT_SECONDS` | الافتراضي 30 |
| `DISTRICT_EMBEDDING_AGREE_MARGIN` | الافتراضي 0.03 |

متجهات الكتالوج تُحسب مرة لكل شركة ومحافظة وتبقى في الذاكرة حتى إعادة التشغيل. قبل التفعيل، وعند تغيير الموديل، قِس أثر الهامش على ملف شحنات حقيقي وراجع الصفوف التي تُحسم:

```powershell
$env:DISTRICT_EMBEDDING_BASE_URL="http://127.0.0.1:8001"; $env:DISTRICT_EMBEDDING_MODEL="BAAI/bge-m3"
.venv\Scripts\python.exe -m scripts.evaluate_district_semantic "JSON_to_Excel (1).xlsx" --show 40
```

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
