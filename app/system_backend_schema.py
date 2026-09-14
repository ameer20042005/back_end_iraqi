# -*- coding: utf-8 -*-
"""عقد استجابة ثابت وموثَّق لبيانات باك اند السستم (منتجات + طلبات) — يُستهلك
من الميزات الثلاث (sales، support، voice_followup) بنفس الشكل.

**لماذا هذا الملف موجود:** قبل هذا العقد، `app/products.py` وapp/order_gateway.py
كانا يمررون `resp.json()` كما هو بلا أي تحقق (`.get("results", [])` /
`.get("orders", [])` فقط) — أي شكل يرجعه باك اند السستم فعلياً يمر للموديل
حرفياً بلا فحص. الموديل "يقرأ" أي JSON كنص، لكن هذا لا يعني أنه يتحقق من
صحته أو اكتماله؛ الانضباط الوحيد كان تعليمات البرومبت ("انسخ حرفياً" —
انظر SALES_SYSTEM_PROMPT بـ app/features/sales/prompts.py)، بلا أي شبكة أمان
برمجية لو تغيّر شكل الحقول أو نقص بعضها.

**مصدر أسماء الحقول:** أعمدة جداول `catalog.products` / `catalog.stock_info`
و`catalog.sells` / `catalog.sell_items` الحقيقية كما موثّقة بـ
`assets/JENNI_STORES_SCHEMA_FOR_AI_QUERY_BUILDER (1).md` (وليست أسماء
مخترعة) — محوَّلة لصيغة snake_case مسطّحة مناسبة لعقد REST بسيط.

**سياسة التسامح (مشدَّدة — docs/fix-plan.md § 8 القرار 6):** الحقول
**الجوهرية** إلزامية — ما بدونه يصير الجواب خاطئاً لا ناقصاً: `id`/`name`/
`price` للمنتج (منتج بلا سعر لا يُعرض على زبون)، و`order_id`/`status` للطلب
(طلب بلا حالة عديم الفائدة بميزة تتبع). عنصر ينقصه أحدها يُستبعَد مع تحذير
باللوق (`_parse_orders`/`_parse_products`) بدل ما يمر صامتاً بحقل None.
الباقي (`barcode`, `eta`, `current_stage`, `assigned_transporter`…) اختياري
بحق: يتحوّل لـ None، والموديل مبرمج (بالبرومبت) يقول "غير متوفر" بدل ما
يخترع قيمة. التسامح الكامل القديم كان حكمة وقت ما كان الخادم مجهولاً، وصار
خطراً وقت ما صار معلوماً (jbot — انظر docs/contract-matching.md): يبتلع عدم
تطابق الأسماء صامتاً. `extra="allow"` يبقى: حقول جديدة مستقبلاً لا تكسر شيئاً.

**من يبني عليه:** فريق باك اند السستم — هذا هو العقد الرسمي المطلوب من
`GET /products/search`، `GET /products/{id}`، `GET /orders/{order_id}`،
`GET /orders/search`، و`GET /orders`. التوثيق المقروء البشري المقابل بـ
API.md § "عقد باك اند السستم"."""

from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class SystemProduct(BaseModel):
    """منتج واحد كما يجب أن يرجعه باك اند السستم — حقل بكل عنصر من
    `results` بـ `GET /products/search`، أو الجسم الكامل بـ
    `GET /products/{id}`.

    يقابل `catalog.products` (id/name/sku/barcode/prices/photos/deleted_at)
    مدموجاً مع `catalog.stock_info` (الكمية المتوفرة عبر المخازن) — باك اند
    السستم هو من يجمعهما، هذا الباك اند لا يفهرسهما محلياً.

    الحقول كلها اختيارية عدا `id`/`name`/`price` — انظر «سياسة التسامح»
    بأعلى الملف. `model_config.extra = "allow"`: حقول إضافية يرجعها باك اند
    السستم مستقبلاً تمر بلا رفض، فقط لا تُتحقق."""

    model_config = ConfigDict(extra="allow")

    id: str
    name: str
    sku: Optional[str] = None
    barcode: Optional[str] = None
    description: Optional[str] = None
    category: Optional[str] = None  # اسم الفئة (catalog.categories.pretty_name)
    price: float  # numeric(19,2) — سعر البيع؛ إلزامي: منتج بلا سعر لا يُعرض
    currency: Optional[str] = "IQD"
    in_stock: Optional[bool] = None  # مشتق من مجموع كميات stock_info > 0
    stock_quantity: Optional[int] = None  # مجموع الكمية المتوفرة عبر المخازن
    photos: List[str] = []  # روابط صور المنتج (products.photos JSON)
    deleted_at: Optional[str] = None  # ISO 8601 — منتج محذوف منطقياً إن وُجدت قيمة


class SystemProductSearchResponse(BaseModel):
    """جسم استجابة `GET /products/search` كاملاً."""

    model_config = ConfigDict(extra="allow")

    results: List[SystemProduct] = []


class SystemOrderItem(BaseModel):
    """سطر واحد بالطلب — يقابل `catalog.sell_items` (qty، أسعار الوحدة/الخصم/الصافي)."""

    model_config = ConfigDict(extra="allow")

    product_id: Optional[str] = None
    product_name: str
    quantity: int = 1
    unit_price: Optional[float] = None
    line_total: Optional[float] = None


class SystemOrder(BaseModel):
    """طلب واحد كما يجب أن يرجعه باك اند السستم — يقابل `catalog.sells`
    (id/receipt_number, customer_name/customer_phone_number, sell_status,
    total_price, delivery_info, estimated_delivery_date) مدموجاً بأسطره
    (`catalog.sell_items`).

    `status` نص عربي أو مرادفه — القيمة تُقرأ حرفياً كما هي من باك اند
    السستم، بلا قائمة ثابتة هنا (نفس مبدأ app/guards.py؛ المرادفات اللغوية
    بـ app/features/support/router.py::_STATUS_SYNONYMS تُطابَق ضدها وقت
    التشغيل، لا تُفترض هنا).

    `current_stage`/`current_step`/`step_entered_at`/`assigned_transporter`
    (**TODO — باك اند السستم لا يرجّعها بعد**): تقابل جدول سير عمل الطلب
    الحقيقي (`current_step_id → sell_flow_step.id → sell_flow_stage.id`،
    و`sell_flow_transition_log` لتاريخ آخر انتقال) و`assigned_transporter_id
    → transporters.id` — موثَّقة بـ
    `assets/JENNI_STORES_SCHEMA_FOR_AI_QUERY_BUILDER (1).md` §6.5/6.7.
    أُضيفت هنا **قبل** ربط باك اند السستم الفعلي (نفس نهج بقية الملف —
    عقد موثَّق سلفاً يشتغل فوراً بلا تعديل كود لما البيانات توصل) لتمكين
    ميزتين ناقصتين بدعم العملاء: «بأي مرحلة الطلب/ليش متأخر؟» (تحتاج
    current_stage + step_entered_at لمعرفة أين ومنذ متى) و«شنو الطلبات
    الموكلة لمندوب معيّن؟» (assigned_transporter). لحد ما تتوفر فعلياً،
    تصل None تلقائياً (نفس سياسة التسامح)، والموديل مبرمج لا يخترع قيمة
    لحقل فاضي.

    الحقول كلها اختيارية عدا `order_id` — انظر «سياسة التسامح» بأعلى الملف."""

    model_config = ConfigDict(extra="allow")

    order_id: str  # receipt_number أو id بصيغة نصية معروضة (مثل ORD-1001)
    status: str  # sell_status أو مرادفه العربي (stepName بـ jbot)؛ إلزامي: طلب بلا حالة عديم الفائدة
    current_stage: Optional[str] = None  # sell_flow_stage.name (عبر current_step_id) — TODO أعلاه
    current_step: Optional[str] = None  # sell_flow_step.name — الخطوة الدقيقة داخل المرحلة
    step_entered_at: Optional[str] = None  # ISO 8601 — آخر انتقال بـ sell_flow_transition_log لنفس الطلب
    customer_name: Optional[str] = None
    phone: Optional[str] = None  # customer_phone_number
    customer_city: Optional[str] = None  # اسم المحافظة (commondata.cities.name_arabic)
    customer_district: Optional[str] = None  # اسم المنطقة (delivery_info->>'districtId' محلولاً لاسم)
    address: Optional[str] = None
    items: List[SystemOrderItem] = []
    total: Optional[float] = None  # total_price
    currency: Optional[str] = "IQD"
    assigned_transporter: Optional[str] = None  # transporters.name (عبر assigned_transporter_id) — TODO أعلاه
    eta: Optional[str] = None  # estimated_delivery_date، نص جاهز للعرض
    created_at: Optional[str] = None  # ISO 8601


class SystemOrderListResponse(BaseModel):
    """جسم استجابة `GET /orders/search` و`GET /orders` كاملاً."""

    model_config = ConfigDict(extra="allow")

    orders: List[SystemOrder] = []


class SystemOrderCountResponse(BaseModel):
    """جسم استجابة `GET /orders/count` — عدّ الطلبات بمعايير.

    **ليش نقطة عدّ منفصلة مو len() على نتيجة /orders/search؟** لأن /search
    يرجّع **صفحة وحدة**، فطولها مو العدد الكلي. سؤال «كم طلب عدنا هالشهر؟»
    جوابه لازم يجي من عدّ حقيقي بقاعدة البيانات، لا من حجم صفحة — وهذا بالضبط
    الخطأ اللي يخلي الموديل يعطي رقماً واثقاً وغلط.

    كائن بحقل واحد مو رقم عارٍ: يبقى العقد كله كائنات JSON قابلة للتحقق
    بـPydantic، ويسمح بإضافة تفاصيل لاحقاً (عدّ حسب الحالة مثلاً) بلا كسر."""

    model_config = ConfigDict(extra="allow")

    count: int = 0


class SystemOrderEvent(BaseModel):
    """حدث واحد بسجل مراحل الطلب — مرحلة، ومتى دخلها، وكم بقى بيها.

    `duration_hours` هو مفتاح الجواب على «ليش متأخر؟»: رقم عشري بالساعات
    (18.5) مو صيغة ISO مدة ("PT18H30M") — الموديل يقرأ الرقم ويقارنه مباشرة.

    `duration_hours = None` معناها **لسا بهذي المرحلة** (ماكو مدة نهائية
    بعد)، مو «مرّ بيها بصفر وقت»."""

    model_config = ConfigDict(extra="allow")

    stage: Optional[str] = None
    entered_at: Optional[str] = None  # ISO 8601
    duration_hours: Optional[float] = None


class SystemOrderHistoryResponse(BaseModel):
    """جسم استجابة `GET /orders/{order_id}/history`.

    الأحداث مرتّبة زمنياً (الأقدم أول) حتى يوصف الموديل الرحلة بترتيبها
    الطبيعي. يقابل جدول سير عمل الطلب الحقيقي (`sell_flow_transition_log`
    بمنظومة JENNI_STORES، أو سجل مراحل الشحنة بباك اند Jenni)."""

    model_config = ConfigDict(extra="allow")

    order_id: str
    events: List[SystemOrderEvent] = []
