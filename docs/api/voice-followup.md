# المكالمات الصوتية الحية — `/voice_followup/*` (LiveKit)

مكالمات صوتية حية مع الزبون عبر [LiveKit Agents](https://github.com/livekit/agents). الموظف يكتب بتطبيق سطح المكتب "اتصل بهؤلاء"، ومساعد نصي يجلب بياناتهم ويرجع للتطبيق أمر `start_call` لكل زبون. التطبيق ينفّذه فتبدأ مكالمة يديرها agent صوتي (جيني أو صباح).

## المعمارية

```text
تطبيق سطح المكتب ──(1) نص الموظف──► POST /voice_followup/operator/chat/completions
        ▲                                   │ tool_calls: search_orders… ثم start_call
        │ (2) ينفّذ أدوات البيانات بنظامه    ▼
        └──(3) start_call ──────────────► POST /voice_followup/calls
                                            │ غرفة LiveKit + dispatch للـ agent
                                            ▼
   الزبون (WhatsApp/SIP) ◄──صوت──► LiveKit Server ◄──► Agent Worker (هذا الخادم، GPU)
                                                        Whisper → vLLM → Chatterbox
                                                        أدوات: save_reason / propose_postpone / confirm_postpone
                                                                    │
                                                                    ▼
                                                        باك اند البيانات (feedback / postpone)
```

| الطرف | دوره |
|---|---|
| **هذا الخادم (مزوّد الذكاء)** | مساعد الموظف النصي، بدء المكالمات، والـ agent worker الصوتي (Whisper + النموذج + Chatterbox) |
| **باك اند البيانات** | مصدر بيانات الطلبات، ويستقبل نتيجة المكالمة: `POST /orders/{id}/feedback` و`POST /orders/{id}/postpone` |
| **تطبيق سطح المكتب** | واجهة الموظف: يرسل الأوامر، ينفّذ أدوات البيانات و`start_call`، ويدخل الغرفة للمراقبة أو لربط صوت الواتساب |
| **خادم LiveKit** | ينقل الصوت (WebRTC/SIP). يحتاج IP عام ومنافذ UDP، فيعيش على LiveKit Cloud أو VPS، **مو على RunPod** |

## الإعداد

متغيرات بيئة (أسرار، فما تنحط بـ `app/config.py`):

| المتغيّر | إلزامي | الوصف |
|---|---|---|
| `LIVEKIT_URL` | ✅ | مثل `wss://<project>.livekit.cloud` |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | ✅ | مفاتيح مشروع LiveKit |
| `LIVEKIT_SIP_TRUNK_ID` | ❌ | SIP trunk صادر (WhatsApp Business Calling أو Twilio عبر LiveKit SIP). بدونه تعمل المكالمة بوضع `bridge` |
| `VOICE_MAX_CALLS` | ❌ | أقصى مكالمات متزامنة لكل worker (افتراضياً `10`). بعدها LiveKit يوجّه المكالمة لـ worker ثاني أو تنتظر لحد ما تفرغ مكالمة |
| `TTS_POOL_SIZE` | ❌ | عدد نسخ Chatterbox (افتراضياً `2`)، كل نسخة تنطق لمكالمة وحدة بالمرة. كل نسخة ~3GB VRAM تقريباً |

`start.sh` يشغّل الـ agent worker تلقائياً لو المتغيرات الثلاثة الأولى موجودة (اللوق: `/tmp/agent.log`). يدوياً:

```bash
python -m app.features.voice_followup.agent download-files   # مرة وحدة (أوزان VAD)
python -m app.features.voice_followup.agent start
```

بدون هذه المتغيرات، `POST /voice_followup/calls` يرجع `503` واضحة.

## مساعد الموظف — `POST /voice_followup/operator/chat/completions`

عقد OpenAI Chat Completions بلا حالة (نفس نمط `/sales/chat/completions`)، بمفتاح `voice_followup_api_key`. التطبيق يرسل التاريخ كاملاً بكل طلب مع **أدوات البيانات اللي ينفّذها هو** (مثل `search_orders`)، والخادم يضيف أداة `start_call` تلقائياً. إذا أرسل التطبيق أداة بنفس الاسم، تعريف الخادم يحل محلها.

**الدورة:**

1. الموظف يكتب: "اتصل بزبائن الطلبات الملغية اليوم".
2. الرد `tool_calls: search_orders(...)`. التطبيق ينفّذه ويعيد النتيجة برسالة `role: "tool"`.
3. الرد `tool_calls: start_call(...)` (واحد لكل طلب). التطبيق يرسل وسائط كل واحد **كما هي** إلى `POST /voice_followup/calls`، ثم يعيد نتيجته برسالة `role: "tool"`.
4. الرد نص ملخّص للموظف (`finish_reason: "stop"`).

```bash
curl -X POST "$BASE/voice_followup/operator/chat/completions" \
  -H "X-API-Key: $VOICE_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "operator",
    "messages": [{"role": "user", "content": "اتصل بصاحب الطلب ORD-1042 واسأله ليش انلغى"}],
    "tools": [{"type": "function", "function": {"name": "search_orders",
      "description": "Search orders by status, date or order id",
      "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}, "status": {"type": "string"}}}}}]
  }'
```

`stream: true` يرجع نفس الرد بصيغة OpenAI SSE وينتهي بـ `data: [DONE]`.

### أداة `start_call` (يعرّفها الخادم)

| الوسيط | النوع | الوصف |
|---|---|---|
| `call_type` | `"followup"` \| `"postpone"` | `followup`: جيني تسأل عن سبب حالة الطلب. `postpone`: صباح تعرض تأجيل التسليم |
| `order` | object | بيانات الطلب من نتيجة أداة البيانات (الجدول أدناه). `order_id` و`status` و`customer_phone` مطلوبة |

## بدء مكالمة — `POST /voice_followup/calls`

الجسم هو نفسه وسائط `start_call`:

```json
{
  "call_type": "postpone",
  "order": {
    "order_id": "ORD-000123456",
    "status": "قيد التوصيل",
    "customer_phone": "07701234567",
    "items": [{"product_name": "غسالة اتوماتيك", "quantity": 1}],
    "scheduled_date": "2026-09-05"
  }
}
```

| حقل `order` | النوع | إلزامي | الوصف |
|---|---|---|---|
| `order_id` | string | ✅ | معرّف الطلب بنظام البيانات |
| `status` | string | ✅ | حالة الطلب بالعربي كما بالنظام (ملغي، مرتجع، لم يتم التسليم، قيد التوصيل...) |
| `customer_phone` | string \| null | ❌* | رقم الزبون — لازم لوضع `sip` |
| `customer_name` / `customer_city` / `customer_district` / `customer_address` | string \| null | ❌ | تُعاد كما هي مع نتيجة المتابعة. **لا تدخل سياق نموذج المكالمة** (الاسم والرقم ما يُنطقان أبداً) |
| `items` | array of `{product_name, quantity}` | ❌ | المنتجات؛ منها يذكر الـ agent الطلب ("الغسالة اللي طلبتها") |
| `reason_hint` | string \| null | ❌ | سبب أوّلي معروف بالنظام (لمكالمة `followup`) |
| `notes` | string \| null | ❌ | ملاحظات |
| `scheduled_date` | string \| null | ❌ | موعد التسليم الحالي `YYYY-MM-DD` (لمكالمة `postpone`) |

**استجابة 200:**

```json
{
  "room": "call-postpone-ORD-000123456-3f9a1c",
  "livekit_url": "wss://project.livekit.cloud",
  "operator_token": "eyJhbGciOi...",
  "dispatch_id": "AD_xxxxx",
  "dial_mode": "sip"
}
```

| `dial_mode` | المعنى |
|---|---|
| `sip` | الـ agent يتصل بالزبون بنفسه عبر `LIVEKIT_SIP_TRUNK_ID` وينتظر الرد قبل ما يتكلم. التوكن للمراقبة أو لتسلّم المكالمة |
| `bridge` | ماكو SIP: التطبيق يدخل الغرفة بـ `operator_token` (LiveKit SDK) وينشر صوت مكالمة الواتساب كمسار صوت. الـ agent يبدأ الكلام أول ما يدخل أول مشارك |

الغرفة تنحذف تلقائياً إذا ما دخلها أحد خلال دقيقتين، وتنحذف عند انتهاء المكالمة (`end_call`).

**أخطاء:**

| كود | السبب |
|---|---|
| `401` | مفتاح `X-API-Key` غلط |
| `422` | `call_type` غير `followup`/`postpone`، أو `order_id`/`status` ناقص |
| `502` | خادم LiveKit رفض الطلب أو ما يستجيب |
| `503` | `LIVEKIT_*` غير مضبوطة |

## سلوك الـ agent بالمكالمة

### جيني — `followup`

تسأل عن سبب حالة الطلب بجمل قصيرة، وأول ما يوضح الزبون السبب تستدعي `save_reason`، فيُرسل لباك اند البيانات:

```json
POST {system_backend_base_url}/orders/{order_id}/feedback
{
  "order_id": "ORD-1001", "status": "ملغي",
  "customer_name": "أحمد", "customer_phone": "07701234567",
  "customer_city": "بغداد", "customer_district": "الكرادة", "customer_address": "قرب مطعم كذا",
  "items": [{"product_name": "لابتوب لينوفو IdeaPad 15", "quantity": 1}],
  "reason_summary": "الزبون يقول التوصيل تأخر وقرر يلغي الطلب",
  "customer_transcript": "لا ماريده الحين، تأخر علي كثير..."
}
```

`customer_transcript` هو كلام الزبون حرفياً كما نسخه Whisper، و`reason_summary` من كلامه هو حصراً.

### صباح — `postpone`

تعرض "اليوم / باجر / بعد باجر"، وتقبل أي موعد يحدده الزبون خلال **١٤ يوماً**. التثبيت يمر بأداتين يتحقق منهما الكود:

1. `propose_postpone(choice)`: المفتاح من جدول مغلق (`today` / `plus_1..plus_14` / `weekday_0..6`). مفتاح خارجه يُرفض، ويرجع التسمية بصيغة الزبون ("باجر"، "يوم الخميس") والتاريخ.
2. `confirm_postpone()`: فقط بعد اقتراح مسبق وتأكيد صريح من الزبون. التاريخ يُحسب بالكود (`postpone.py`):

```json
POST {system_backend_base_url}/orders/{order_id}/postpone
{
  "order_id": "ORD-000123456",
  "new_scheduled_date": "2026-09-09",
  "postpone_choice": "plus_4",
  "reason": "تأجيل بطلب الزبون عبر مكالمة صباح"
}
```

| ما يقوله الزبون | المفتاح |
|---|---|
| اليوم · هسه · الحين | `today` |
| باجر · بكرة | `plus_1` |
| بعد باجر | `plus_2` |
| بعد N أيام | `plus_N` |
| بعد أسبوع / أسبوعين | `plus_7` / `plus_14` |
| اسم يوم ("خليها الخميس") | `weekday_3`، أقرب خميس قادم |

### مشترك

- "رقم غلط" أو "هذا مو طلبي": اعتذار وإنهاء المكالمة بلا إرسال أي شي.
- فشل الإرسال لباك اند البيانات: الـ agent يعتذر ويگول إن الفريق راح يتابع، وما يدّعي إن شي انحفظ.
- إنهاء المكالمة بأداة `end_call` يحذف الغرفة، فيقطع الاتصال عن كل المشاركين بما فيهم مكالمة SIP.

## التزامن (عدة مكالمات بنفس الوقت)

الـ worker الواحد يشغّل لحد `VOICE_MAX_CALLS` مكالمة متزامنة (افتراضياً 10)، كلها خيوط بنفس العملية فالأوزان تُحمَّل مرة وحدة:

| المرحلة | السلوك مع مكالمات متزامنة |
|---|---|
| النموذج (vLLM) | يخدمها كلها سوية (continuous batching)، ما تنتظر بعض |
| النسخ (Whisper) | نسخة وحدة، المقاطع تمر واحد واحد. المقطع قصير (~0.3 ثانية) فالانتظار قليل |
| النطق (Chatterbox) | مجمّع من `TTS_POOL_SIZE` نسخة. كل نسخة لمكالمة بالمرة، فإذا كل النسخ مشغولة الجملة تنتظر أول نسخة تفرغ |

**النطق هو عنق الزجاجة:** مكتبة Chatterbox ما تقبل توليدين متوازيين على نفس النسخة، فعدد المكالمات اللي تحچي **بنفس اللحظة** بلا انتظار = `TTS_POOL_SIZE`. المكالمات ما تحچي كل الوقت (الزبون يحچي نص الوقت)، فـ 10 مكالمات ما تحتاج 10 نسخ، بس كل ما قلّت النسخ زادت الوقفة قبل الرد وقت الضغط. القرار الصحيح يحتاج قياس على كرت RunPod الفعلي:

- **A40 مشترك مع vLLM** (~6GB متبقي): واقعياً `TTS_POOL_SIZE=1-2`.
- **كرت أكبر** (A100 80GB / H100) أو **كرت ثاني للـ worker**: `TTS_POOL_SIZE=4-5` يغطي 10 مكالمات براحة.
- عدة workers على عدة كروت: LiveKit يوزّع المكالمات بينها تلقائياً.

## ملاحظات تشغيل

- النموذج هو نفس خادم vLLM (`LLM_BASE_URL`) بلا تفكير، لأن زمن الرد بالمكالمة حساس.
- الصوت المرجعي لـ Chatterbox: `app/features/voice_followup/reference/mazen.mp3` (أول ~10 ثوانٍ منه).
