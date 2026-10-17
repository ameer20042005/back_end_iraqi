# المكالمات الصوتية الحية — `/voice_followup/*` (LiveKit أو WebSocket)

> طريقتين لتشغيل المكالمة بنفس الشخصية والتعليمات والأدوات: **LiveKit** (يحتاج خادم LiveKit + SIP trunk) أو **الوكيل المحلي عبر WebSocket** (`WS /voice_followup/stream`): التطبيق يتصل من رقم الواتساب مالته ويبث صوت المكالمة لهذا الخادم — بلا LiveKit وبلا UDP، فيشتغل من وراء بروكسي RunPod. التفاصيل بقسم [الوكيل المحلي عبر WebSocket](#الوكيل-المحلي-عبر-websocket).

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
| `SYSTEM_BACKEND_CALL_OUTCOME_PATH` | ❌ | مسار إرسال النتيجة لباك اند السستم (افتراضياً `/calls/outcome`). فارغ = ما ترسل |

متغيرات الوكيل المحلي (WebSocket) وباك اند البيانات Catalog بأقسامها تحت.

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

`save_outcome` يحفظ النتيجة بملف (المصدر اللي يسحبه التطبيق، دائماً)، ثم يرسل نفس جسم النتيجة (best effort) لكل وجهة مضبوطة:

```text
POST {system_backend_base_url}{SYSTEM_BACKEND_CALL_OUTCOME_PATH}   (X-API-Key: voice_followup_api_key؛ الافتراضي /calls/outcome، فارغ = معطّل)
POST {CATALOG_BASE_URL}{CATALOG_PATH_CALL_OUTCOME}                 (Bearer + X-Tenant-Id؛ فارغ افتراضياً = معطّل)
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

## الوكيل المحلي عبر WebSocket

**`WS /voice_followup/stream`** — مكالمة كاملة بلا LiveKit. تطبيق سطح المكتب يتصل بالزبون من رقم الواتساب مالته، ويبث صوت الزبون لهنا، وهذا الخادم يدير الحوار (Whisper ← النموذج ← Chatterbox) بنفس `build_call_instructions` ونفس الأدوات (`call_tools.py`) اللي يستعملها الـ agent بـ LiveKit. يشتغل بعملية uvicorn نفسها (FastAPI WebSocket عادي)، فيعبر بروكسي RunPod HTTPS (`wss://<POD_ID>-8000.proxy.runpod.net/voice_followup/stream`).

```text
الزبون ◄─WhatsApp─► تطبيق سطح المكتب ◄──WebSocket (PCM + JSON)──► هذا الخادم
                      (رقم الشركة)                               VAD → Whisper → vLLM → Chatterbox
                                                                 أدوات: save_outcome / end_call / بيانات Catalog
```

### المصادقة

هيدر `X-API-Key: <voice_followup_api_key>` (نفس مفتاح نقاط HTTP). للعملاء اللي ما يگدرون يضبطون هيدرات WebSocket: `?api_key=<key>`. المفتاح الغلط: `{"type":"error","code":"unauthorized",...}` ثم إغلاق بالرمز **4401**.

### البروتوكول

| الاتجاه | الإطار | الشكل |
|---|---|---|
| تطبيق ← خادم | نص (أول رسالة) | `{"type":"start","call":{...جسم POST /voice_followup/calls...},"sample_rate":16000}` |
| خادم ← تطبيق | نص | `{"type":"ready","call_id":"call-ORD-1042-3f9a1c2b"}` |
| تطبيق ← خادم | نص | `{"type":"call_state","state":"ringing" / "accepted" / "ended"}` |
| تطبيق ← خادم | ثنائي | صوت الزبون: PCM s16le أحادي بـ `sample_rate`، أي حجم قطعة (التطبيق يرسل 60ms = 960 عينة) |
| خادم ← تطبيق | ثنائي | صوت الـ agent: PCM s16le أحادي **16000Hz**، قطع 40ms (1280 بايت) |
| خادم ← تطبيق | نص | `{"type":"transcript","role":"user" / "agent","text":"..."}` — مقاطع نهائية بس (جملة الـ agent تنرسل بعد ما ينرسل صوتها كامل) |
| خادم ← تطبيق | نص | `{"type":"interrupt"}` — الزبون حچى فوق الـ agent: **فرّغ صوت التشغيل المخزّن فوراً** |
| خادم ← تطبيق | نص | `{"type":"outcome", ...نفس حقول CallOutcome...}` — بعد `save_outcome`، أو النتيجة الاحتياطية عند النهاية |
| خادم ← تطبيق | نص | `{"type":"end_call","reason":"agent" / "idle_timeout" / "max_duration"}` — اقفل مكالمة الواتساب |
| خادم ← تطبيق | نص | `{"type":"error","code":"...","message":"..."}` |
| تطبيق ← خادم | نص | `{"type":"ping"}` ← `{"type":"pong"}` (مسموح حتى قبل `start`) |

**التسلسل:**

1. `start` (خلال 15 ثانية من الاتصال) ← الخادم يتحقق بـ `CallRequest` ويرد `ready` بـ `call_id` (نفس صيغة `room`: `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`). النتيجة لاحقاً على `GET /voice_followup/calls/{call_id}/outcome`.
2. التطبيق يتصل بالواتساب: `call_state: ringing` (اختياري)، ثم `accepted` أول ما يرد الزبون. **الـ agent ما يحچي ولا يسمع قبل `accepted`** (الصوت قبلها يتجاهل)، وبعدها مباشرة يبدأ بالافتتاحية.
3. الحوار: التطبيق يبث صوت الزبون باستمرار (حتى السكوت)، ويشغّل الصوت الواصل بالترتيب. الإرسال يسبق الوقت الحقيقي بحدود `VOICE_STREAM_TTS_LEAD_MS` (1.5 ثانية افتراضياً) فلازم التطبيق يخزّن (buffer).
4. النهاية:
   - الـ agent ينهي (`end_call`): الخادم يرسل الجملة الأخيرة كاملة، ثم `{"type":"end_call","reason":"agent"}`. التطبيق يكمّل تشغيل المخزّن، يقفل الواتساب، ويرسل `call_state: ended` (أو يسكّر الاتصال). بعد 10 ثواني + مدة الصوت المتبقي الخادم يسكّر بنفسه.
   - الزبون قفل: `call_state: ended` ← الخادم يكتب النتيجة الاحتياطية إذا ما صار `save_outcome` ("الزبون لم يرد على المكالمة" إذا ما وصل `accepted`، وإلا "انتهت المكالمة بدون نتيجة مسجّلة"، و`follow_up_needed: true`)، يرسلها كحدث `outcome`، ثم يسكّر بالرمز 1000.
   - انقطاع الاتصال بلا `ended`: نفس النتيجة الاحتياطية تنكتب بالمخزن.
   - مهلة: بلا كلام من الزبون ولا من الـ agent لمدة `VOICE_STREAM_IDLE_TIMEOUT_SECONDS` (60) بعد الرد، أو تجاوز `VOICE_STREAM_MAX_CALL_SECONDS` (15 دقيقة) من `ready` ← `end_call` بسبب `idle_timeout`/`max_duration` ثم النتيجة الاحتياطية والإغلاق.

**رموز الإغلاق والأخطاء:**

| رمز الإغلاق | `code` بحدث error | السبب |
|---|---|---|
| `4401` | `unauthorized` | مفتاح غلط |
| `4408` | `timeout` | ما وصلت `start` خلال 15 ثانية |
| `4422` | `validation` | أول رسالة مو `start`، جسم `call` غير صالح (مع `details` من pydantic)، أو `sample_rate` مو عدد صحيح بين 8000 و48000 |
| `4429` | `busy` | وصل `VOICE_STREAM_MAX_CALLS` |
| `1011` | `internal` | خلل داخلي |
| `1000` | — | نهاية طبيعية |

أخطاء ما تسكّر المكالمة: `llm` (النموذج ما رد بهذا الدور)، `stt`، `tts`، `protocol` (رسالة غير معروفة).

**مثال جلسة:**

```text
→ {"type":"start","call":{"company_name":"متجر النور","agent_name":"سارة","goal":"اسأله عن رأيه بالغسالة","contact":{"name":"أحمد","phone":"07701234567"},"reference_id":"ORD-1042"},"sample_rate":16000}
← {"type":"ready","call_id":"call-ORD-1042-3f9a1c2b"}
→ {"type":"call_state","state":"accepted"}
← [PCM 40ms] [PCM 40ms] ...
← {"type":"transcript","role":"agent","text":"هلا بيك، وياك سارة من متجر النور."}
→ [PCM 60ms] [PCM 60ms] ...            (كلام الزبون)
← {"type":"transcript","role":"user","text":"اي وصلت وعاجبتني"}
← {"type":"outcome","room":"call-ORD-1042-3f9a1c2b","status":"done",...}
← [PCM ...] {"type":"transcript","role":"agent","text":"شكراً إلك، مع السلامة."}
← {"type":"end_call","reason":"agent"}
→ {"type":"call_state","state":"ended"}
```

### كشف الكلام والمقاطعة

- **VAD:** نفس أوزان Silero اللي يستعملها الـ agent بـ LiveKit، تشتغل مباشرة بـ onnxruntime (ملف `silero_vad.onnx` من حزمة `livekit-plugins-silero`، بلا استيراد `livekit.agents`) بحالة RNN لكل مكالمة ونوافذ 32ms. إذا مو متوفرة (`VOICE_STREAM_VAD=auto`) يرجع لكاشف طاقة (RMS بالـ dBFS، نوافذ 20ms).
- **تقطيع الأدوار:** كلام متواصل ≥ `VOICE_STREAM_MIN_SPEECH_MS` (250) = بداية دور؛ سكوت ≥ `VOICE_STREAM_END_SILENCE_MS` (700) = نهاية الدور، والمقطع (مع ~300ms قبله) يروح لـ Whisper. مقطع أطول من `VOICE_STREAM_MAX_SEGMENT_SECONDS` (15) ينقطع وينسخ. مقاطع متتالية قبل رد الـ agent تندمج بدور واحد، والرد يبدأ بس لما يسكت الزبون.
- **المقاطعة:** أول ما تبدأ "بداية دور" والـ agent يولّد أو صوته بعده يشتغل عند التطبيق (حسب تقدير الخادم لمدة التشغيل): تنلغي مهمة الرد (النموذج + النطق) وينرسل `interrupt`. الجمل اللي انگالت كاملة تبقى بتاريخ المحادثة. بعد `end_call` المقاطعة تتعطل.
- **زمن الرد:** النموذج يتدفق (`stream: true`)، والرد يتقطع لجمل وكل جملة تنطق وتنرسل أول ما تكمل، فأول صوت يطلع بعد أول جملة بدل الرد كامل.

### إعدادات الوكيل المحلي

| المتغيّر | الافتراضي | الوصف |
|---|---|---|
| `VOICE_STREAM_MAX_CALLS` | `VOICE_MAX_CALLS` أو `10` | أقصى مكالمات WebSocket متزامنة؛ بعدها `busy`/4429 |
| `VOICE_STREAM_VAD` | `auto` | `silero` / `energy` / `auto` |
| `VOICE_STREAM_SILERO_THRESHOLD` | `0.5` | عتبة احتمال الكلام لـ Silero |
| `VOICE_STREAM_ENERGY_DBFS` | `-40` | عتبة كاشف الطاقة |
| `VOICE_STREAM_MIN_SPEECH_MS` | `250` | أقل كلام يبدأ دوراً (ويقاطع) |
| `VOICE_STREAM_END_SILENCE_MS` | `700` | السكوت اللي ينهي الدور |
| `VOICE_STREAM_MAX_SEGMENT_SECONDS` | `15` | أطول مقطع ينسخ مرة وحدة |
| `VOICE_STREAM_IDLE_TIMEOUT_SECONDS` | `60` | سكوت الطرفين بعد الرد قبل الإنهاء |
| `VOICE_STREAM_MAX_CALL_SECONDS` | `900` | أقصى مدة للمكالمة |
| `VOICE_STREAM_TTS_LEAD_MS` | `1500` | كم يسبق إرسال الصوت وقت تشغيله (أقل = مقاطعة أنظف، أكثر = تحمّل أكبر لتذبذب الشبكة) |
| `VOICE_STREAM_LLM_MAX_TOKENS` | `256` | أقصى توكنات لكل رد |

**تشغيل:** Whisper (`stt.py`) وChatterbox (`tts.py`، مجمّع `TTS_POOL_SIZE`) يتحمّلون بعملية uvicorn عند أول مكالمة WebSocket (أول مكالمة أبطأ). إذا شغّلت الـ agent worker مال LiveKit بنفس الوقت، كل عملية عندها نسخها (VRAM مضاعف).

## باك اند البيانات Catalog (أدوات البيانات)

الخادم يتصل بباك اند البيانات **Catalog** (متاجر جيني) بنفس عقد `ai_agent` (Java): `Authorization: Bearer <CATALOG_ACCESS_TOKEN>` و`X-Tenant-Id`، والقوائم بصيغة PageResponse `{content,totalElements,totalPages,number,size}`. العميل: `app/core/catalog.py` (عميل httpx مشترك، خطأ موحّد `CatalogError`، ترميز `+` بالرقم كـ `%2B`، تقليص الردود لحقول يحتاجها النموذج بس).

**`CATALOG_BASE_URL` فارغ = الأدوات ما تنعرض للنموذج أصلاً**، وتعليمات المكالمة تگول للـ agent إنه ما يگدر يتأكد من الطلبات/الأسعار وإن الفريق راح يتابع.

| المتغيّر | الافتراضي | الوصف |
|---|---|---|
| `CATALOG_BASE_URL` | — | مثل `https://api.example.com` (بلا `/` بالنهاية) |
| `CATALOG_ACCESS_TOKEN` | — | JWT (Bearer). سر — بيئة فقط |
| `CATALOG_TENANT_ID` | — | يُرسل بـ `X-Tenant-Id` |
| `CATALOG_TIMEOUT_SECONDS` | `15` | مهلة كل طلب |
| `CATALOG_STORAGE_ID` | — | مخزن المتجر: لو مضبوط، السعر والتوفّر من stock infos هذا المخزن بس (مثل `ai_agent`) |
| `CATALOG_SEARCH_PAGE_SIZE` / `CATALOG_ORDERS_PAGE_SIZE` / `CATALOG_STOCK_PAGE_SIZE` | `5` / `10` / `20` | أحجام صفحات صغيرة لزمن رد قليل |
| `CATALOG_PATH_PRODUCT_SEARCH` | `/api/catalog/v1/products/search` | `?text=&page=&size=` |
| `CATALOG_PATH_PRODUCT` | `/api/catalog/v1/products/{id}` | |
| `CATALOG_PATH_CATEGORIES` | `/api/catalog/v1/categories/all` | |
| `CATALOG_PATH_PRODUCT_STOCK` | `/api/catalog/v1/stock-infos/product/{productId}` | `?page=&size=` (لحد 5 صفحات) |
| `CATALOG_PATH_CUSTOMER_ORDERS` | `/api/catalog/v1/online/sells/all` | `?customerPhoneNumber=&page=&size=` |
| `CATALOG_PATH_ORDER` | `/api/catalog/v1/online/sells/{id}` | |
| `CATALOG_PATH_ORDERS_SEARCH` | `/api/catalog/v1/online/sells/all` | بحث مساعد الموظف (الفلاتر تحت) |
| `CATALOG_PATH_CALL_OUTCOME` | فارغ | لو مضبوط: `POST` نتيجة المكالمة (جسم CallOutcome) لـ Catalog. فارغ = ما ترسل |

### أدوات المكالمة الصوتية (LiveKit وWebSocket)

| الأداة | الوسائط | السلوك |
|---|---|---|
| `get_my_orders` | — | طلبات الزبون. **الرقم من metadata المكالمة الموثوقة (`contact.phone`)، مو من النموذج**؛ فلتر Catalog جزئي فالنتائج تتفلتر ثانيةً بآخر 10 أرقام. بلا رقم = ما ينادي Catalog |
| `get_order_status` | `order_id` | الطلب بس إذا `customerPhoneNumber` مالته يطابق رقم المكالمة، وإلا "ماكو طلب بهذا الرقم لهذا الزبون" (ما ينكشف إنه موجود) |
| `search_products` | `text` | المنتجات (اسم، تصنيف) + السعر والتوفّر من stock infos (نداءات متوازية؛ فشل مخزون منتج ما يفشّل البحث) |

النتائج JSON مضغوط بلا اسم الزبون ورقمه وعنوانه. فشل Catalog يصير رسالة للنموذج: "گول إن الفريق راح يتابع".

### أدوات مساعد الموظف (ينفّذها الخادم)

`POST /voice_followup/operator/chat/completions` يضيف (لما Catalog مضبوط) أداتين ينفّذهن الخادم بحلقة داخل نفس الطلب (لحد 3 جولات): النموذج يشوف النتيجة ويكمّل لحد ما يرد نصاً أو بأدوات التطبيق/`start_call`. **رد التطبيق ما يحتوي استدعاءات هذه الأدوات أبداً**، وعقد OpenAI يبقى نفسه. أداة تطبيق بنفس الاسم يحل محلها تعريف الخادم.

| الأداة | الوسائط | ملاحظات |
|---|---|---|
| `search_orders` | `order_id`، أو `phone` / `receipt_number` / `created_at` / `flow_step_id` | فلاتر قائمة `/online/sells/all` المدعومة فعلاً بـ Catalog: `customerPhoneNumber` (جزئي، ونفلتر بآخر 10 أرقام)، `receiptNumber` (جزئي)، `createdAt` (**يوم واحد** `YYYY-MM-DD`)، `flowStepId` (الحالة بـ Catalog خطوة سير عمل برقم). **ماكو بـ Catalog فلتر حالة نصي ولا مدى تواريخ** (`date_from`/`date_to`) |
| `search_products` | `text` | نفس أداة المكالمة |

نتائج `search_orders` تشمل اسم الزبون ورقمه (الموظف يحتاجهم لـ `start_call`). ملاحظة: التطبيق يرسل التاريخ بلا نتائج أدوات الخادم، فاللي يحتاجه لاحقاً لازم يكون بالرد النصي أو بوسائط `start_call`.

## ملاحظات تشغيل

- النموذج هو نفس خادم vLLM (`LLM_BASE_URL`) بلا تفكير، لأن زمن الرد بالمكالمة حساس.
- الصوت المرجعي لـ Chatterbox: `app/features/voice_followup/reference/mazen.mp3` (أول ~10 ثوانٍ منه).
