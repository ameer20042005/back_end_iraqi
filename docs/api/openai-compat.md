# واجهة OpenAI — `POST /v1/chat/completions`

واجهة Chat Completions متوافقة مع OpenAI/Spring AI وبلا حالة، يستدعيها jbot. تدعم native function calling، والتنفيذ الفعلي للأدوات يصير عند jbot مو بهذا الخادم. المفتاح: `openai_compat_api_key`.

## سلوك مهم

- **فحص قبل التوليد**: خادم النموذج غير متاح ← 503 `LM_STUDIO_UNAVAILABLE`؛ الموديل غير محمَّل ← 503 `AI_MODEL_NOT_LOADED`؛ المهلة ← 504 `AI_REQUEST_TIMEOUT`. كل رد خطأ يحوي `error.code` ثابتاً يقرأه jbot آلياً.
- **التفكير معطّل** (`reasoning_effort: "none"` في [llm_options.py](../../app/core/llm_options.py)): الرد ~1.5–3 ثوانٍ على LM Studio بدل 4–17.
- **استرداد نية الأداة**: بلا تفكير يكتب الموديل أحياناً «نستدعي الدالة searchShipments» نصاً بدل استدعائها. إذا ذكر الرد اسم أداة مرسلة، يُعاد التوليد مرة واحدة بـ `tool_choice="required"`. لا يحدث هذا بعد نتيجة أداة (لا حلقات).
- **رد فارغ**: إذا رجع الموديل رداً فارغاً يُعاد التوليد مرة واحدة (بـ `tool_choice="none"` بعد نتيجة أداة).
- **وسائط الأداة** يجب أن تكون كائن JSON؛ اسم أداة غير مرسل أو وسائط مشوّهة لا تُمرَّر للعميل.

## أدوات الشحن في واجهة OpenAI

مسار `POST /v1/chat/completions` لا ينفذ أدوات الشحن داخل هذا الخادم. تطبيق العميل
يرسل تعريفات الأدوات ضمن `tools`، ينفذ الاستدعاء الناتج بهوية الموظف المخوّلة، ثم يعيد
نتيجته برسالة `role: "tool"`. أسماء الأدوات المعتمدة هي:

| الاسم | الغرض | المعاملات المطلوبة |
|---|---|---|
| `searchShipments` | البحث عن شحنات مطابقة لمعيار مثل رقم الوصل أو الهاتف أو الحالة. | معيار بحث واحد على الأقل، مثل `receiptNumber` أو `phone` |
| `countShipments` | إرجاع العدد الكلي للشحنات المطابقة لنفس فلاتر البحث، من دون اعتبار صفحة النتائج عدداً كاملاً. | فلتر واحد على الأقل، مثل `status` أو `phone` أو نطاق تاريخ |
| `getShipmentHistory` | إرجاع تسلسل مراحل شحنة واحدة، مع مددها وأسباب التأخير المتاحة. | `receiptNumber` أو معرّف الشحنة الذي يتوقعه تطبيق العميل |

هذه أسماء `function.name` حرفياً بحالة الأحرف نفسها. لا يرسل العميل إلا الأدوات التي
يستطيع تنفيذها؛ إذا لم يرسل أداة مناسبة، يطلب الوكيل معياراً أو يوضح أن البيانات غير
متاحة بدلاً من التخمين.

مثال لتعريف الأدوات الثلاث:

```json
[
  {"type":"function","function":{"name":"searchShipments","description":"Search authorized shipments","parameters":{"type":"object","properties":{"receiptNumber":{"type":"string"},"phone":{"type":"string"},"status":{"type":"string"}},"minProperties":1}}},
  {"type":"function","function":{"name":"countShipments","description":"Count authorized shipments matching filters","parameters":{"type":"object","properties":{"phone":{"type":"string"},"status":{"type":"string"},"fromDate":{"type":"string"},"toDate":{"type":"string"}},"minProperties":1}}},
  {"type":"function","function":{"name":"getShipmentHistory","description":"Get stage history for one authorized shipment","parameters":{"type":"object","properties":{"receiptNumber":{"type":"string"}},"required":["receiptNumber"]}}}
]
```
