# واجهة المبيعات OpenAI-compatible

`sales` الآن AI API فقط، بنفس معمارية `app/features/openai_compat`:

1. يستقبل تاريخ المحادثة وتعريفات الأدوات من العميل.
2. يضيف system prompt الخاص بالمبيعات.
3. ينفذ توليد موديل واحداً فقط.
4. يعيد `assistant.content` أو native `assistant.tool_calls` بصيغة OpenAI.

الخادم لا يحفظ جلسة مبيعات، ولا يحمّل كتالوجاً، ولا ينفذ أداة، ولا يرسل طلب
شراء. العميل مسؤول عن التاريخ وتنفيذ الأدوات وإعادة نتائجها برسائل `role=tool`.

## نقطة النهاية

`POST /sales/chat/completions` مع:

```http
X-API-Key: <sales_api_key>
Content-Type: application/json
```

`POST /sales/chat` اسم قديم مخفي في Swagger لنفس الدالة. و
`POST /sales/chat/stream` يحافظ على اسم `sales_chat_stream` ويعيد OpenAI SSE.

```json
{
  "model": "sales",
  "messages": [
    {"role": "user", "content": "شنو عندكم غسالات؟"}
  ],
  "tools": [
    {
      "type": "function",
      "function": {
        "name": "search_products",
        "description": "Search products visible to the authenticated store",
        "parameters": {
          "type": "object",
          "properties": {"query": {"type": "string"}},
          "required": ["query"]
        }
      }
    }
  ],
  "tool_choice": "auto",
  "stream": false
}
```

`tools` لا تملك تعريفات افتراضية على الخادم، تماماً مثل `openai_compat`؛ العميل
يرسل فقط الأدوات التي يستطيع تنفيذها وينفذها بهوية المستخدم الموثقة. أدوات
المبيعات المعتمدة وأسماؤها الثابتة موثقة في القسم التالي. لا يرسل العميل أدوات
خارج هذه القائمة لمسار المبيعات.

## أدوات المبيعات المعتمدة

كل الأسماء أدناه هي قيمة `function.name` حرفياً بصيغة `snake_case`. الخادم يمرر
المخطط إلى النموذج ويتحقق أن أي `tool_call` يرجع باسم أرسله العميل، لكنه لا ينفذ
الأداة بنفسه. تطبيق العميل هو المسؤول عن التفويض بحسب `tenant` والمستخدم الحالي،
ثم يعيد النتيجة برسالة OpenAI من الدور `tool`.

| الاسم المعروض | `function.name` | الغرض | المعاملات المطلوبة |
|---|---|---|---|
| Search Products | `search_products` | البحث ضمن منتجات المتجر المرئية للمستخدم. | `query` نص البحث |
| Get Product Details | `get_product_details` | تحميل تفاصيل منتج محدد بعد اختيار المنتج أو طلب معلوماته. | `product_id` معرّف المنتج |
| Get Categories | `get_categories` | جلب فئات المنتجات المتاحة لعرضها أو تضييق البحث. | لا يوجد |
| Get Customer Orders | `get_customer_orders` | جلب طلبات عميل محدد من نظام الطلبات. | `customer_id` معرّف العميل |
| Get Order Details | `get_order_details` | جلب تفاصيل طلب واحد، بما فيها حالته وعناصره. | `order_id` معرّف الطلب |
| Create Order | `create_order` | إنشاء طلب جديد بعد أن يوافق العميل صراحة على الملخص النهائي. | `customer_id` و`items` و`delivery_address` |

المعاملات الاختيارية المقترحة: `search_products` يقبل `category` و`in_stock_only`؛
و`get_customer_orders` يقبل `status` و`limit`. ينبغي أن تعيد الأدوات كائن JSON
واضحاً. أداة `create_order` يجب أن تعيد على الأقل `success` و`order_id` عند النجاح؛
لا يعلن الوكيل تثبيت الطلب ما لم تكن `success` هي `true`.

هذا مثال كامل لتعريفات الأدوات الستة التي يرسلها العميل. يمكن توسيع خصائص النتيجة
التي يعيدها التنفيذ، لكن لا تغيّر أسماء الدوال أو المعاملات المطلوبة بلا تنسيق مع
عقد المبيعات:

```json
[
  {"type":"function","function":{"name":"search_products","description":"Search products visible to the authenticated store","parameters":{"type":"object","properties":{"query":{"type":"string"},"category":{"type":"string"},"in_stock_only":{"type":"boolean"}},"required":["query"]}}},
  {"type":"function","function":{"name":"get_product_details","description":"Get full details for one visible product","parameters":{"type":"object","properties":{"product_id":{"type":"string"}},"required":["product_id"]}}},
  {"type":"function","function":{"name":"get_categories","description":"Get categories visible to the authenticated store","parameters":{"type":"object","properties":{}}}},
  {"type":"function","function":{"name":"get_customer_orders","description":"Get orders for one authorized customer","parameters":{"type":"object","properties":{"customer_id":{"type":"string"},"status":{"type":"string"},"limit":{"type":"integer","minimum":1}},"required":["customer_id"]}}},
  {"type":"function","function":{"name":"get_order_details","description":"Get details for one authorized order","parameters":{"type":"object","properties":{"order_id":{"type":"string"}},"required":["order_id"]}}},
  {"type":"function","function":{"name":"create_order","description":"Create a confirmed customer order","parameters":{"type":"object","properties":{"customer_id":{"type":"string"},"items":{"type":"array","minItems":1,"items":{"type":"object"}},"delivery_address":{"type":"string"}},"required":["customer_id","items","delivery_address"]}}}
]
```

إذا أعاد الموديل tool call:

```json
{
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": null,
      "tool_calls": [{
        "id": "call_1",
        "type": "function",
        "function": {
          "name": "search_products",
          "arguments": "{\"query\":\"غسالة\"}"
        }
      }]
    },
    "finish_reason": "tool_calls"
  }]
}
```

يحفظ العميل رسالة `assistant`، ينفذ الأداة، ثم يرسل النتيجة في الطلب التالي:

```json
{
  "role": "tool",
  "tool_call_id": "call_1",
  "content": "{\"found\":true,\"results\":[{\"name\":\"غسالة\",\"price\":500000}]}"
}
```

## ما بقي محفوظاً

- النص الأصلي `SALES_SYSTEM_PROMPT` لم يُعدّل؛ أضيفت له تعليمات بروتوكول native
  tool calling عند بناء رسالة النظام النهائية.
- أسماء `sales_chat` و`sales_chat_stream` و`build_sales_prompt` محفوظة.
- أسماء الأدوات المذكورة في prompt محفوظة.
- الصور تبقى بصيغة OpenAI `image_url` داخل `messages[].content` وتمر للمحرك.
- `stream=true` يعيد `chat.completion.chunk` وينتهي بـ`data: [DONE]`.

خدمة `resolve_order` المستخدمة بواسطة `/orders/create` انتقلت إلى
`app/order_service.py` لأنها منطق طلبات وليست جزءاً من AI API للمبيعات.

ملف `assets/JENNI_STORES_SCHEMA_FOR_AI_QUERY_BUILDER (1).md` يصف قواعد البيانات
ولا يعرّف HTTP API للأدوات. لذلك لا ينفذ `sales` SQL أو يختلق endpoints؛ تطبيق
العميل يربط أدواته بخدمات Jenni Stores مع tenant والصلاحيات من هويته الموثقة.
