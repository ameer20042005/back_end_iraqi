# المرحلة 0 — مطابقة العقد مع باك اند السستم الفعلي (jbot)

> **الحالة:** مكتملة · **التاريخ:** 2026-09-14 · **تنفّذ:** [fix-plan.md § 4](fix-plan.md)
> **المصدر الذي قورن عليه:** `DATUM_WORK/jbot-prod` (Spring Boot) —
> `internal/InternalOrderController.java`، `internal/ShipmentToOrderMapper.java`،
> `internal/dto/SystemOrderDto.java`، `web/jenni/JenniClient.java`،
> `web/jenni/dto/SearchShipmentRequestDto.java`، `web/jenni/dto/PagedResponse.java`.

jbot يحقّق عقد الطلبات فوق بيانات Jenni تحت البادئة `/internal`
(`system_backend_base_url="http://127.0.0.1:8081/internal"` — انظر `app/config.py`).

---

## 1. جدول المطابقة

| المسار | العقد المكتوب (`system_backend_schema.py`) | الواقع بـ jbot | الفجوة |
|---|---|---|---|
| `GET /orders/{order_id}` | جسم `SystemOrder`، 404 = غير موجود | ✅ `InternalOrderController.getByOrderId` — يبحث بـ `receiptNumber`، 404 عند الغياب، 503 عند سقوط Jenni | لا فجوة |
| `GET /orders/search` | `?phone&status&date_from&date_to` → `{"orders": [...]}` | ✅ نفس الأسماء snake_case حرفياً؛ `status` يُطابَق على `stepName` بـ Jenni؛ التاريخ يُطبَّق بجهة Jenni فعلاً | ❌ لا `limit`/`offset`/`total`/`city`/`customer_name` |
| `GET /orders` | `{"orders": [...]}` كل الدفتر | ⚠️ `listAll` يرسل طلباً فارغاً لـ Jenni فيرجع **الصفحة الافتراضية فقط** (`PagedResponse.pageSize`) | 🔴 **دفتر جزئي صامت** (العطل A3 بنسخته للطلبات) — يُصلَح بجهة jbot |
| `GET /orders/count` | غير مكتوب بالعقد (تقترحه الخطة) | ❌ غير موجود بـ `/internal` — لكن `JenniClient.countShipments` **موجود** ويكفي تمريره | يحتاج مساراً جديداً بـ jbot |
| `GET /products/search` | `{"results": [SystemProduct]}` | ❌ **لا وجود لأي مسار منتجات بـ jbot إطلاقاً** | 🔴 ميزة المبيعات بلا باك اند حقيقي بعد |
| `GET /products/{id}` | جسم `SystemProduct` | ❌ غير موجود | نفس ما فوق |

### أسماء الحقول (الطلب)

`SystemOrderDto` بـ jbot مكتوب كنسخة Java حرفية من `SystemOrder` مع
`@JsonNaming(SnakeCase)` — **كل الأسماء متطابقة** (`order_id`, `status`,
`current_stage`, `current_step`, `step_entered_at`, `phone`, `customer_city`,
`customer_district`, `address`, `items`, `total`, `currency`,
`assigned_transporter`, `eta`, `created_at`). العطل A1 **غير قائم** للطلبات.

ما **لا** يملكه Jenni ويصل دائماً `null`/فارغاً (انظر `ShipmentToOrderMapper`):

| الحقل | القيمة الفعلية | الأثر على الكود عندنا |
|---|---|---|
| `customer_name` | `null` دائماً | البحث باسم الزبون **لن يطابق شيئاً** — فلتر `customer_name` بـ `OrderQuery` يبقى للمستقبل، ولا يُعرض للموظف كوعد |
| `items` | `[]` دائماً | `_format_order_reply`/`_format_order_line` تعرضان «طلبك» / رقم الطلب فقط — سلوك صحيح موجود أصلاً |
| `eta` | `null` دائماً | يُخفى شرطياً — موجود أصلاً |
| `assigned_transporter` | ✅ **يصل فعلاً** (`deliveryAgentName`) | فرع «مندوب فلان» بـ `_bulk_query_answer` يشتغل الآن — لم يعد TODO |
| `current_stage`/`current_step`/`step_entered_at` | ✅ تصل فعلاً | TODO بـ `system_backend_schema.py` منتهي عملياً |

### المصادقة

الكود يرسل **الاثنين معاً** (بعد تعديل `system_backend.auth_headers`):
`X-API-Key` (مفتاح الخدمة، يطابق `support.api-key` بـ jbot) و`Authorization: Bearer <JWT المستخدم الأصلي>`
(يمرّره jbot بحقل `auth_token` بـ `/support/chat`). jbot يرجع **401** لو غاب أحدهما —
وهذا بالضبط ما يجعل العطل A5 (4xx تتحوّل لـ500 عارية) **واقعاً لا افتراضاً**: يُعالَج بالمرحلة 7.

### قيم `status` الحقيقية

`SearchShipmentRequestDto.stepName` بـ jbot يوثّق 17 قيمة ثابتة مثل
«شحنات جديده قادمة في الطريق»، «داخل المخزن»، «قيد التوصيل»، «شحنات سلمت بنجاح»،
«راجع كلي»، «مؤجل»… — `_STATUS_SYNONYMS` عندنا يطابق بالاحتواء («توصيل»، «تسليم»…)
فيغطي أغلبها، لكن «سلمت بنجاح» لا تحتوي «تسليم» — **يحتاج مرادفاً جديداً** («سلمت» → «سلمت»)،
خارج نطاق هذه الخطة، مسجَّل هنا فقط.

---

## 2. أسئلة القدرات (تحسم المرحلة 3)

| السؤال | الجواب | القرار |
|---|---|---|
| يدعم `limit`/`offset`؟ | ❌ لا بـ `/internal`. Jenni نفسه **يدعم الترقيم** (`PagedResponse` فيه `pageNumber/pageSize/total/totalPages`) لكن `SearchShipmentRequestDto` لا يحمل حقلَي صفحة، وjbot لا يمرر شيئاً | **الخطة البديلة** (fix-plan § المرحلة 2): `search`/`count` تُنفَّذان بجلب-ثم-قصّ **داخل `HttpOrderStatusProvider` حصراً** |
| يوجد `total` بالرد؟ | ❌ jbot يرمي `PagedResponse.total` ويرسل `orders` فقط | نحسبه محلياً بعد الفلترة (صحيح فقط بحدود ما رجّعه Jenni — انظر A3 أعلاه) |
| يبحث بالاسم/المدينة بجهته؟ | المدينة: Jenni يدعم `stateName` لكن jbot **لا يمرره**. الاسم: غير موجود أصلاً | فلترة محلية على `customer_city`؛ `customer_name` يبقى فلتراً شكلياً |
| مسار «كل الكتالوج» حقيقي؟ | لا يوجد أي مسار منتجات | المبيعات تبقى على `list_all` + المطابقة المحلية (المرحلة 8) لحين بناء مسار منتجات بـ jbot |
| يطبّق فلاتر التاريخ فعلاً؟ | ✅ نعم (`shipmentIssueDateFrom/To`) | الفلترة المحلية المزدوجة تبقى كشبكة أمان رخيصة (لا تُزال) |

---

## 3. قرار كل فجوة

| الفجوة | الحل | أين |
|---|---|---|
| لا ترقيم/عدّ/فلاتر مركّبة | **عدّل عندك** — الخطة البديلة داخل طبقة المستودع | `app/order_gateway.py` (المرحلة 2) |
| `listAll` صفحة افتراضية فقط | **عدّل باك اند السستم** — jbot يمرر `pageSize` كبيراً أو يدور على الصفحات، أو (الأفضل) يعرض `limit`/`offset`/`total` مباشرة ويمرر `countShipments` كمسار `/internal/orders/count` | طلب لفريق jbot — **لا يغيّر شيئاً عندنا** غير جسم `search`/`count` بـ `HttpOrderStatusProvider` |
| لا مسارات منتجات | **عدّل باك اند السستم** — بناء `/internal/products/search` و`/internal/products/{id}` بعقد `SystemProduct` | طلب لفريق jbot |
| 401 عند غياب التوكن | **عدّل عندك** — `backend_request` يلتقط 4xx | المرحلة 7 |
| `customer_name` دائماً null | **لا شيء** — يبقى اختيارياً بالعقد | — |

> **مبدأ ثابت (من الخطة):** أي تحويل يعيش حصراً بـ `HttpOrderStatusProvider`/`HttpProductRepository`.
> لما يضيف jbot الترقيم الحقيقي، يتغيّر جسم `search`/`count` فقط — الأداة والبرومبت والراوتر لا يلمسها أحد.

---

## 4. القرار 6 — تشديد سياسة التسامح

الخادم صار معلوماً، فالحقول الجوهرية صارت إلزامية (مطبَّق بـ `system_backend_schema.py`):

- **طلب:** `order_id` · `status` — jbot يرسل `status` دائماً من `stepName`؛ شحنة بلا خطوة تُستبعَد **مع تحذير باللوق** (كان الاستبعاد موجوداً أصلاً بـ `_parse_orders`، الفرق أن حقلاً ناقصاً صار يُعدّ انحرافاً لا حالة عادية).
- **منتج:** `id` · `name` · `price` — لا يوجد خادم منتجات بعد، فالتشديد هنا **عقد مطلوب** من jbot لا مطابقة لواقع.

`extra="allow"` يبقى بالنموذجين.
