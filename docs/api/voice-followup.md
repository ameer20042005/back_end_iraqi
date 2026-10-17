# المكالمات الصوتية الحية — `/voice_followup/*` (LiveKit)

مكالمات صوتية حية **عامة** مع الزبائن عبر [LiveKit Agents](https://github.com/livekit/agents). ماكو سيناريو ثابت: موظف تطبيق سطح المكتب يكتب هدف المكالمة (وأسئلة اختيارية ومعطيات)، والـ agent الصوتي موظف مبيعات/خدمة عملاء مهذّب باللهجة العراقية باسم الشركة اللي تستعمل التطبيق (`company_name` من إعدادات التطبيق بكل طلب). المساعد النصي يستعمل أدوات التطبيق ويرجع أمر `start_call` لكل شخص، والتطبيق يسحب نتيجة كل مكالمة بـ `GET /voice_followup/calls/{room}/outcome`.

## المعمارية

```text
تطبيق سطح المكتب ──(1) نص الموظف──► POST /voice_followup/operator/chat/completions
        ▲                                   │ tool_calls: أدوات التطبيق… ثم start_call
        │ (2) ينفّذ أدوات البيانات بنظامه    ▼
        └──(3) start_call ──────────────► POST /voice_followup/calls
                                            │ غرفة LiveKit + dispatch للـ agent
                                            ▼
   الزبون (WhatsApp/SIP) ◄──صوت──► LiveKit Server ◄──► Agent Worker (هذا الخادم، GPU)
                                                        Whisper → vLLM → Chatterbox
                                                        أدوات: save_outcome / end_call
                                                                    │
                                   ┌────────────────────────────────┴───────────────┐
                                   ▼                                                ▼
                  data/call_outcomes/{room}.json                     باك اند السستم (اختياري)
        GET /voice_followup/calls/{room}/outcome ◄──┘                POST /calls/outcome
```

| الطرف | دوره |
|---|---|
| **هذا الخادم (مزوّد الذكاء)** | مساعد الموظف النصي، بدء المكالمات، والـ agent worker الصوتي (Whisper + النموذج + Chatterbox) |
| **باك اند السستم** (اختياري) | يستقبل نسخة من نتيجة المكالمة: `POST {system_backend_base_url}/calls/outcome` (best effort) |
| **تطبيق سطح المكتب** | واجهة الموظف: يرسل الأوامر واسم الشركة، ينفّذ أدواته و`start_call`، يدخل الغرفة للمراقبة أو لربط صوت الواتساب، ويسحب نتيجة المكالمة |
| **خادم LiveKit** | ينقل الصوت (WebRTC/SIP). يحتاج IP عام ومنافذ UDP، فيعيش على LiveKit Cloud أو VPS، **مو على RunPod** |

## الإعداد

متغيرات بيئة (أسرار، فما تنحط بـ `app/config.py`):

| المتغيّر | إلزامي | الوصف |
|---|---|---|
| `LIVEKIT_URL` | ✅ | مثل `wss://<project>.livekit.cloud` |
| `LIVEKIT_API_KEY` / `LIVEKIT_API_SECRET` | ✅ | مفاتيح مشروع LiveKit |
| `LIVEKIT_SIP_TRUNK_ID` | ❌ | SIP trunk صادر (WhatsApp Business Calling أو Twilio عبر LiveKit SIP). بدونه تعمل المكالمة بوضع `bridge` |
| `VOICE_MAX_CALLS` | ❌ | أقصى مكالمات متزامنة لكل worker (افتراضياً `10`). بعدها LiveKit يوجّه المكالمة لـ worker ثاني أو تنتظر لحد ما تفرغ مكالمة |
| `CALL_OUTCOMES_DIR` | ❌ | مجلد ملفات نتائج المكالمات (افتراضياً `data/call_outcomes`). لازم يكون مشتركاً بين uvicorn والـ agent worker |
| `CALL_OUTCOMES_TTL_HOURS` / `CALL_OUTCOMES_MAX_FILES` | ❌ | مدة بقاء النتيجة (افتراضياً `24`) وأقصى عدد ملفات (افتراضياً `5000`) |
| `TTS_POOL_SIZE` | ❌ | عدد نسخ Chatterbox (افتراضياً `2`)، كل نسخة تنطق لمكالمة وحدة بالمرة. كل نسخة ~3GB VRAM تقريباً |

`start.sh` يشغّل الـ agent worker تلقائياً لو المتغيرات الثلاثة الأولى موجودة (اللوق: `/tmp/agent.log`). يدوياً:

```bash
python -m app.features.voice_followup.agent download-files   # مرة وحدة (أوزان VAD)
python -m app.features.voice_followup.agent start
```

بدون هذه المتغيرات، `POST /voice_followup/calls` يرجع `503` واضحة.

## مساعد الموظف — `POST /voice_followup/operator/chat/completions`

عقد OpenAI Chat Completions بلا حالة (نفس نمط `/sales/chat/completions`)، بمفتاح `voice_followup_api_key`. التطبيق يرسل التاريخ كاملاً بكل طلب مع **أدواته اللي ينفّذها هو** (مثل `search_contacts`، `send_whatsapp_message`، `create_campaign`، `create_ticket`، `list_tickets`، `get_call_outcome`)، وحقلاً إضافياً اختيارياً `company_name` يدخل رسالة system. الخادم ما يعتمد على أسماء أدوات التطبيق؛ يضيف بس أداته `start_call` تلقائياً، وإذا أرسل التطبيق أداة بنفس الاسم، تعريف الخادم يحل محلها.

| حقل إضافي | النوع | الوصف |
|---|---|---|
| `company_name` | string \| null | اسم الشركة من إعدادات التطبيق (≤ 120). بدونه يگول المساعد "الشركة" |

**الدورة:**

1. الموظف يكتب: "اتصل بزبائن اليوم واسألهم عن رأيهم بالمنتج".
2. الرد `tool_calls: search_contacts(...)`. التطبيق ينفّذه ويعيد النتيجة برسالة `role: "tool"`.
3. الرد `tool_calls: start_call(...)` (واحد لكل شخص). التطبيق **يضيف `company_name`** من إعداداته ويرسل الوسائط إلى `POST /voice_followup/calls`، ثم يعيد نتيجته برسالة `role: "tool"`.
4. الرد نص ملخّص للموظف (`finish_reason: "stop"`).
5. لاحقاً: "سجّل تذكرة بنتائج المكالمات" ← المساعد يستعمل أداة التطبيق للنتيجة (مثل `get_call_outcome`) ثم أداة التذاكر.

قواعد المساعد: سؤال توضيح واحد قصير إذا الأمر غامض؛ `start_call` مرة وحدة لكل شخص؛ جهة اتصال بلا رقم ما يتصل بيها ويذكرها للموظف؛ ما يخترع جهات اتصال أو أرقام؛ بعد نتائج الأدوات ملخّص قصير بالعربي.

```bash
curl -X POST "$BASE/voice_followup/operator/chat/completions" \
  -H "X-API-Key: $VOICE_KEY" -H "Content-Type: application/json" \
  -d '{
    "model": "operator",
    "company_name": "متجر النور",
    "messages": [{"role": "user", "content": "اتصل بأحمد واسأله عن رأيه بالغسالة"}],
    "tools": [{"type": "function", "function": {"name": "search_contacts",
      "description": "Search the app contacts by name or phone",
      "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}}]
  }'
```

`stream: true` يرجع نفس الرد بصيغة OpenAI SSE وينتهي بـ `data: [DONE]`.

### أداة `start_call` (يعرّفها الخادم)

وسائطها هي نفسها جسم `POST /voice_followup/calls` (الجدول أدناه). المطلوب بمخطط الأداة: `goal` و`contact` (مع `phone`) فقط — `company_name` **غير مطلوب بالأداة** لأن التطبيق يملأه (ويستبدله) قبل الإرسال.

## بدء مكالمة — `POST /voice_followup/calls`

```json
{
  "company_name": "متجر النور",
  "agent_name": "سارة",
  "goal": "اسأل الزبون عن رأيه بالمنتج وهل يحتاج شي ثاني",
  "questions": ["هل وصلك الطلب؟", "شنو تقييمك؟"],
  "context": "المنتج: غسالة اتوماتيك. الموعد السابق 2026-09-05",
  "contact": {"name": "أحمد", "phone": "07701234567"},
  "reference_id": "ORD-1042",
  "language": "ar"
}
```

| الحقل | النوع | إلزامي | الوصف |
|---|---|---|---|
| `company_name` | string (1–120) | ✅ | اسم الشركة من إعدادات التطبيق؛ الـ agent يتكلم باسمها |
| `agent_name` | string \| null (≤ 60) | ❌ | اسم أول يعرّف بيه الـ agent نفسه. بدونه: "وياك خدمة العملاء من {company_name}" |
| `goal` | string (1–1000) | ✅ | هدف المكالمة كما كتبه الموظف |
| `questions` | array of string (≤ 10، كل واحد ≤ 300) | ❌ | أسئلة محددة تنسأل بالترتيب، سؤال بكل دور |
| `context` | string \| null (≤ 2000) | ❌ | حقائق يقدر الـ agent يستشهد بيها. ماكو أي معلومة غيرها |
| `contact.phone` | string \| null (≤ 32) | ❌* | لازم لوضع `sip`. ما يدخل تعليمات النموذج |
| `contact.name` | string \| null (≤ 120) | ❌ | للتطبيق فقط. **ما يدخل سياق نموذج المكالمة أبداً**، ولو انكتب (أو الرقم) داخل `goal`/`questions`/`context` ينشال تلقائياً |
| `reference_id` | string \| null (≤ 120) | ❌ | معرّف يربط بيه التطبيق النتيجة (طلب/تذكرة/جهة اتصال). يرجع بالنتيجة ويدخل اسم الغرفة |
| `language` | string | ❌ | افتراضياً `"ar"`. `"en"` = يبدأ بإنكليزية بسيطة |

`contact` نفسه اختياري بالجسم (بدونه = وضع `bridge`).

**استجابة 200:**

```json
{
  "room": "call-ORD-1042-3f9a1c2b",
  "livekit_url": "wss://project.livekit.cloud",
  "operator_token": "eyJhbGciOi...",
  "dispatch_id": "AD_xxxxx",
  "dial_mode": "sip"
}
```

| `dial_mode` | المعنى |
|---|---|
| `sip` | الـ agent يتصل بالزبون بنفسه عبر `LIVEKIT_SIP_TRUNK_ID` وينتظر الرد قبل ما يتكلم. التوكن للمراقبة أو لتسلّم المكالمة |
| `bridge` | ماكو SIP أو ماكو رقم: التطبيق يدخل الغرفة بـ `operator_token` (LiveKit SDK) وينشر صوت مكالمة الواتساب كمسار صوت. الـ agent يبدأ الكلام أول ما يدخل أول مشارك |

الغرفة تنحذف تلقائياً إذا ما دخلها أحد خلال دقيقتين، وتنحذف عند انتهاء المكالمة (`end_call`).

**أخطاء:**

| كود | السبب |
|---|---|
| `401` | مفتاح `X-API-Key` غلط |
| `422` | `company_name`/`goal` ناقص أو فارغ، أو تجاوز الحدود (أكثر من 10 أسئلة، `context` > 2000...) |
| `502` | خادم LiveKit رفض الطلب أو ما يستجيب |
| `503` | `LIVEKIT_*` غير مضبوطة |

## نتيجة المكالمة — `GET /voice_followup/calls/{room}/outcome`

تطبيق سطح المكتب ما يستقبل webhooks، فيسحب النتيجة دورياً بنفس المفتاح. `room` من رد `POST /calls`.

```json
{
  "room": "call-ORD-1042-3f9a1c2b",
  "status": "done",
  "reference_id": "ORD-1042",
  "summary": "الطلب وصل والزبون راضي عن الغسالة وما يحتاج شي ثاني",
  "answers": [{"question": "هل وصلك الطلب؟", "answer": "اي وصل"}],
  "follow_up_needed": false,
  "customer_transcript": "اي وصل\nعاجبتني كلش",
  "ended_at": "2026-10-17T09:41:12+00:00"
}
```

- `status: "pending"` (HTTP 200، `answers: []` وباقي الحقول `null`/`false`) لحد ما تنحفظ النتيجة.
- `customer_transcript`: كلام الزبون وحده حرفياً كما نسخه Whisper.
- مكالمة انتهت بلا `save_outcome` (ما رد، رقم غلط، انقطع الخط) تنحفظ نتيجتها `done` بملخّص ثابت ("الزبون لم يرد على المكالمة" / "انتهت المكالمة بدون نتيجة مسجّلة") و`follow_up_needed: true`، حتى ما يبقى التطبيق ينتظر.
- التخزين: ملف JSON لكل غرفة تحت `CALL_OUTCOMES_DIR` (الـ agent worker عملية منفصلة عن uvicorn)، يبقى `CALL_OUTCOMES_TTL_HOURS` (24 ساعة) وبعدها يرجع `pending`.
- `room` لازم يطابق `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`، وإلا `422` (لا path traversal).

## سلوك الـ agent بالمكالمة

موظف مبيعات وخدمة عملاء مهذّب باللهجة العراقية، باسم `company_name`:

1. تحية قصيرة وتعريف مرة وحدة ("هلا بيك، وياك سارة من متجر النور")، ثم سبب الاتصال بجملة، ثم أول سؤال.
2. يسأل `questions` بالترتيب، سؤال وحد بكل دور، جمل قصيرة (٦–١٦ كلمة)، بلا رموز ولا قوائم (الكلام يتحوّل لصوت).
3. أول ما يتحقق `goal` أو يرفض الزبون: يستدعي `save_outcome(summary, answers, follow_up_needed)` مرة وحدة — الملخّص والأجوبة من كلام الزبون حصراً.
4. يشكر وينهي بـ `end_call` (يحذف الغرفة فيقطع كل المشاركين بما فيهم SIP).

قواعد ثابتة: ما يدّعي إنه AI ولا يذكر مصطلحات تقنية؛ ما ينطق اسم الزبون أو رقمه؛ ما يخترع معلومة مو موجودة بـ `context`؛ يبدّل لإنكليزية بسيطة إذا الزبون حچى إنكليزي؛ "رقم غلط / مو إلي" ← اعتذار و`end_call` بلا `save_outcome`.

`save_outcome` يحفظ النتيجة بملف (المصدر اللي يسحبه التطبيق)، ثم لو `system_backend_base_url` مضبوط يرسل نفس جسم النتيجة (best effort) إلى:

```text
POST {system_backend_base_url}/calls/outcome   (X-API-Key: voice_followup_api_key)
```

فشل هذا الإرسال ما يلغي النتيجة المحفوظة (ينسجّل باللوق بس). إذا فشل الحفظ نفسه، الـ agent يعتذر ويگول إن الفريق راح يتابع، وما يدّعي إن شي انحفظ.

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
