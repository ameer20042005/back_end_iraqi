# المكالمات الصوتية — Pipecat فقط

تطبيق Rust يتصل عبر `/voice_followup/stream`، ويجري اتصال واتساب من جلسة المستخدم.
الصوت PCM s16le mono 16kHz؛ ينسخه openai/whisper-large-v3، ثم يرسَل النص إلى Qwen/Qwen3.5-9B للحوار والأدوات.
Pipecat يدير التوليد وتجميع العبارات والنطق والتشغيل بالتوازي. F5-TTS الفصحى (`SWivid/Habibi-TTS`، `Specialized/MSA`) يحوّل الرد النصي إلى صوت.
لا غرف RTC ولا SIP ولا عامل مستقل؛ عملية FastAPI واحدة (`--workers 1`).

التشغيل وميزانية GPU ونتائج المراجعة: [بنية Whisper وQwen وPipecat](../pipecat-native-audio.md).
المصادقة تبقى `X-API-Key`، وأسماء نقاط API وعقد PCM لم تتغير.

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

## تجهيز جلسة — `POST /voice_followup/calls`

الجسم: `company_name` و`goal` مطلوبان، و`questions` و`agent_name` و`context` و`contact` و`reference_id` و`language` اختيارية.
يرجع `room` و`transport: "pipecat"` و`stream_path: "/voice_followup/stream"` و`stream_id` و`expires_in_seconds: 120`.
يرسل العميل `stream_id` ضمن `start` لاستخدام نفس `room` للنتيجة. يُستهلك مرة واحدة، وتُرفض الجلسات المنتهية أو ذات الجسم المختلف.
يمكن للتطبيق الاستمرار بإرسال `start` دون تجهيز مسبق، وهو مسار `ai_call_dial` الحالي.
هذا الطلب يجهّز الجلسة ولا يتصل بالزبون وحده. الحقول القديمة `livekit_url/operator_token/dispatch_id` فارغة للتوافق فقط.

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
- التخزين: ملف JSON لكل غرفة تحت `CALL_OUTCOMES_DIR` (داخل عملية uvicorn الواحدة)، يبقى `CALL_OUTCOMES_TTL_HOURS` (24 ساعة) وبعدها يرجع `pending`.
- `room` لازم يطابق `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`، وإلا `422` (لا path traversal).

نجاح `save_outcome` ينهي الحوار تلقائياً: نتيجة محفوظة ثم وداع قصير ثم `end_call`، دون جولة نموذج إضافية. الملخص الفارغ وفشل الحفظ لا يفعّلان الإغلاق التلقائي. الإرسال الإضافي للنتيجة إلى الأنظمة الخارجية له مهلة ثانيتين بعد حفظها محلياً.

## سلوك الوكيل والتزامن

تعليمات الشركة والهدف والأسئلة والأدوات ونتائج المكالمات بقيت مشتركة من `prompts.py` و`call_tools.py`.
الافتتاحية تُجهّز أثناء الرنين ولا يُرسل أي صوت قبل `accepted`.
كل مكالمة تملك سياقها وقاطعها الصوتي؛ F5-TTS يملك نسخة واحدة افتراضياً لتوفير VRAM.
`VOICE_STREAM_MAX_CALLS` يحدد السعة، و`TTS_POOL_SIZE` يرفع توازي النطق على حساب الذاكرة.
Pipecat يلغي التوليد والنطق والتشغيل عند المقاطعة، ويسجّل النص المنطوق قبل دور العميل الجديد.

## WebSocket

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
| خادم ← تطبيق | نص | `{"type":"end_call","reason":"agent" / "idle_timeout" / "max_duration" / "tts_unavailable" / "stt_unavailable"}` — اقفل مكالمة الواتساب |
| خادم ← تطبيق | نص | `{"type":"error","code":"...","message":"...","reason":"..."}` — `reason` اختياري (مع `code: "tts"` و`code: "stt"`) |
| تطبيق ← خادم | نص | `{"type":"ping"}` ← `{"type":"pong"}` (مسموح حتى قبل `start`) |

**التسلسل:**

1. `start` (خلال 15 ثانية من الاتصال) ← الخادم يتحقق بـ `CallRequest` ويرد `ready` بـ `call_id` (نفس صيغة `room`: `^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$`). النتيجة لاحقاً على `GET /voice_followup/calls/{call_id}/outcome`.
2. التطبيق يتصل بالواتساب: `call_state: ringing` (اختياري)، ثم `accepted` أول ما يرد الزبون. **الـ agent ما يحچي ولا يسمع قبل `accepted`** (الصوت قبلها يتجاهل)، وبعدها مباشرة يبدأ بالافتتاحية.
3. الحوار: التطبيق يبث صوت الزبون باستمرار (حتى السكوت)، ويشغّل الصوت الواصل بالترتيب. الإرسال يسبق الوقت الحقيقي بحدود `VOICE_STREAM_TTS_LEAD_MS` (1.5 ثانية افتراضياً) فلازم التطبيق يخزّن (buffer).
4. النهاية:
   - الـ agent ينهي (`end_call`): الخادم يرسل الجملة الأخيرة كاملة، ثم `{"type":"end_call","reason":"agent"}`. التطبيق يكمّل تشغيل المخزّن، يقفل الواتساب، ويرسل `call_state: ended` (أو يسكّر الاتصال). الخادم ينتظر تشغيل الصوت المتبقي قبل إرسال end_call، ثم يغلق الاتصال بعد 10 ثوانٍ إن لم يصل تأكيد العميل.
   - الزبون قفل: `call_state: ended` ← الخادم يكتب النتيجة الاحتياطية إذا ما صار `save_outcome` ("الزبون لم يرد على المكالمة" إذا ما وصل `accepted`، وإلا "انتهت المكالمة بدون نتيجة مسجّلة"، و`follow_up_needed: true`)، يرسلها كحدث `outcome`، ثم يسكّر بالرمز 1000.
   - انقطاع الاتصال بلا `ended`: نفس النتيجة الاحتياطية تنكتب بالمخزن.
   - النطق معطّل: إذا فشل نطق جملتين **ورا بعض** بنفس المكالمة (الزبون ما يسمع شي)، الخادم يرسل `{"type":"end_call","reason":"tts_unavailable"}` ويكتب نتيجة احتياطية بـ `follow_up_needed: true` وملخّص "تعذّر توليد صوت الـ agent فانتهت المكالمة — تحتاج متابعة"، ثم `outcome` والإغلاق بـ 1000. جملة تنجح بين فشلين تصفّر العدّاد.
   - النسخ معطّل: فشل Whisper مرتين متتاليتين يرسل `end_call` بسبب `stt_unavailable`، وتُحفظ نتيجة احتياطية تحتاج متابعة.
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

أخطاء ما تسكّر المكالمة: `llm` (النموذج ما رد بهذا الدور)، `stt`، `tts`، `protocol` (رسالة غير معروفة). (استثناء: فشل `tts` أو `stt` مرتين ورا بعض ينهي المكالمة بـ `tts_unavailable` أو `stt_unavailable`، انظر فوق.)

حدث `error` بـ `code: "tts"` يحمل `reason` (التطبيق يگدر يتجاهله) — بلا تفاصيل داخلية؛ النص الكامل للاستثناء باللوق بس:

| `reason` | المعنى | وين تدوّر |
|---|---|---|
| `cuda_oom` | نفاد ذاكرة الـ GPU أثناء تحميل F5-TTS أو التوليد | `vram_free_gb` بـ `/health/voice`، وقسم ميزانية الذاكرة تحت |
| `not_installed` | `f5-tts` أو تبعية لها ما تنستورد (`tts.import_error` بـ `/health/voice`) | `python3 -c "import f5_tts.api"` |
| `load_failed` | المكتبة موجودة بس تحميل الأوزان/بصمة الصوت فشل لسبب غير الذاكرة (تنزيل، ملف مرجعي، توافق نسخة) | لوق uvicorn: "فشل تحميل نسخة النطق" مع traceback |
| `generation_failed` | الموديل محمَّل والتوليد نفسه فشل (أو رجع بلا صوت) | لوق uvicorn |

كل فشل نطق ينكتب بسطر لوق واحد: `فشل نطق جملة بالمكالمة <call_id> (<n> ورا بعض): reason=<...> error=<النوع>: <الرسالة> vram_free_gb=<...>`.

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
- **تقطيع الأدوار:** كلام متواصل ≥ `VOICE_STREAM_MIN_SPEECH_MS` (250) = بداية دور؛ سكوت ≥ `VOICE_STREAM_END_SILENCE_MS` (400) = نهاية الدور، والمقطع (مع ~300ms قبله) يروح لـ Whisper. مقطع أطول من `VOICE_STREAM_MAX_SEGMENT_SECONDS` (15) ينقطع وينسخ. مقاطع متتالية قبل رد الـ agent تندمج بدور واحد، والرد يبدأ بس لما يسكت الزبون.
- **المقاطعة:** أول ما تبدأ "بداية دور" والـ agent يولّد أو صوته بعده يشتغل عند التطبيق (حسب تقدير الخادم لمدة التشغيل): تنلغي مهمة الرد (النموذج + النطق) وينرسل `interrupt`. الجمل المكتملة تبقى بتاريخ المحادثة، والعبارة اللي بدأ إرسال صوتها وانقطعت تبقى مع ملاحظة أنها ما اكتملت؛ حتى ما يعيد المقدمة أو يفترض أن الزبون سمعها كاملة. بعد `end_call` المقاطعة تتعطل.
- **زمن الرد:** النموذج يتدفق (`stream: true`)، والرد يتقطع لعبارات تنطق وتنرسل أول ما تكمل. التحية القصيرة المتبوعة بفاصلة تنضم للتعريف، والكلمة ما تنقسم عند وصولها بعدة توكنات. يُحذف الصمت الزائد من بداية ونهاية صوت كل عبارة مع إبقاء هامش للحروف ووقفة قصيرة، بدون حذف الوقفات داخل الكلام. الجملة المكررة حرفياً داخل الرد الواحد ما تنطق مرتين؛ يظل ممكناً إعادتها بدور لاحق إذا طلب الزبون.
- **أسلوب الحوار:** يبدأ بعراقي مهذّب افتراضياً («هلا بيك، وياك…»)، ويصوغ الأسئلة حسب سياق الكلام. الأسئلة محاور وليست استبياناً ثابتاً: يتجاوز ما أجاب عنه الزبون، يتعامل مع الاعتراض أو الانشغال قبل السؤال التالي، ولا يعيد المقدمة عند «ألو» أو المقاطعة. `language=en` يبدأ بافتتاحية إنجليزية. هذه تعليمات للنموذج؛ جودة اللهجة والإلقاء وزمن التوليد الفعلي تحتاج تجربة على خادم الصوت بأوزانه الحقيقية.

### إعدادات الصوت والتشخيص

`VOICE_STREAM_END_SILENCE_MS=400`، و`VOICE_STREAM_FIRST_PHRASE_CHARS=56`، و`TTS_POOL_SIZE=1` افتراضياً.
Whisper هو مسار النسخ الوحيد، بنسخة مشتركة للمكالمات والرسائل المرفوعة؛ الرسائل المسجلة تُقسّم إلى قطع 28 ثانية متراكبة.
يفحص الخادم جاهزية Qwen وتبعيات النسخ والنطق قبل حدث `ready`. رمز الإغلاق 1013 يعني أن الصوت غير متوفر أو لم يجهز بعد.
الفشل والضغط لا يُسقطان البيانات بصمت: `audio_backpressure` و`stt` و`voice_worker_failed` وأخطاء SSE ظاهرة.

`GET /health/voice` يعرض `engine: pipecat` و`input_mode: whisper` و`stt` و`llm` و`memory_plan` و`gpu` و`last_call`.
حقول `gpu.process_allocated_gb/process_reserved_gb/process_cached_unused_gb` تخص عملية Python الحالية؛ `vram_free_gb` يخص الكرت كله.
إحصاءات العميل تشمل `segments_to_model/stt_empty/stt_errors`؛ لا يُحمّل الفحص نموذجاً.
تفاصيل قياس التأخير والذاكرة وخطوات التحقق في [تقرير المراجعة](../pipecat-native-audio.md).

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

### أدوات المكالمة الصوتية (Pipecat)

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
- الصوت المرجعي لـ F5-TTS: `app/features/voice_followup/reference/MSA.wav` ونصّه الحرفي بـ `tts_ref_text_ar` في `app/config.py`.
